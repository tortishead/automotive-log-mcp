"""Native crash dumps (tombstones), text form.

Read from ``/data/tombstones/tombstone_NN`` (bugreport zip ``FS/``), ``TOMBSTONE`` sections of
older bugreports and native-crash dropbox entries. Layout (debuggerd)::

    *** *** *** *** *** *** *** *** *** *** *** *** *** *** *** ***
    Build fingerprint: '...'
    Timestamp: 2026-01-01 00:00:05.123456789+0100
    Cmdline: /vendor/bin/hw/vhal-service
    pid: 812, tid: 830, name: VehicleHal  >>> /vendor/bin/hw/vhal-service <<<
    signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault addr 0x10
    Abort message: '...'            (for aborts)
        x0  0000000000000000  x1 ...  (registers)
    backtrace:
          #00 pc 000000000004a2b0  /vendor/lib64/libvhal.so (VehicleHal::set(Prop const&)+48) (BuildId: ..)
    stack: / memory near ..: / code around ..:
    memory map (N entries):
    --- --- --- ...                  (other threads, each with registers and backtrace)
    open files:
    --------- tail end of log main   (log buffers)

Sections: ``header``, ``registers``, ``backtrace``, ``memory``, ``memory_map``, ``threads``,
``open_files``, ``log``. Protobuf tombstones (``.pb``) are M5.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from ...model import RawArtifact
from ..base import ParseItem, SourceContext, TextConnector, head_lines
from .common import (
    Parsed,
    Sections,
    basename,
    fmt_time,
    parse_wall,
    section_time,
    signature,
    strip_args,
    to_artifact,
)

BANNER = "*** *** *** *** *** *** *** *** *** *** *** *** *** *** *** ***"
PID_LINE = re.compile(r"pid: (\d+), tid: (\d+), name: (.*?)\s+>>> (.*) <<<")
SIGNAL = re.compile(r"signal (\d+) \((\w+)\), code (-?\d+) \(([^)]*)\)(?:, fault addr (\S+))?(?:\s+\(.*\))?")
FRAME = re.compile(
    r"\s*#(?P<n>\d+) pc (?P<pc>[0-9a-fA-F]+)\s+(?P<map>.*?)(?: \(offset 0x[0-9a-f]+\))?"
    r"(?: \((?P<fn>.+)\+(?P<off>\d+)\))?(?: \(BuildId: (?P<bid>[0-9a-f]+)\))?\s*$"
)
REGISTER = re.compile(r"\s{4}[a-z][a-z0-9]{0,4}\s+[0-9a-f]{8,16}\b")
LOG_START = re.compile(r"--------- (?:tail end of log|log) (\w+)")
THREAD_SEP = "--- --- --- --- --- --- --- --- --- --- --- --- --- --- --- ---"

# Frames skipped at the top of an abort when building the signature: the abort machinery.
ABORT_LIBS = frozenset({"libc.so", "libbase.so", "liblog.so", "libutils.so"})
SIGNATURE_FRAMES = 3


def parse_frame(line: str) -> dict[str, Any] | None:
    m = FRAME.match(line)
    if m is None:
        return None
    frame: dict[str, Any] = {"n": int(m["n"]), "pc": m["pc"], "map": m["map"].strip()}
    if m["fn"]:
        frame["func"] = m["fn"]
        frame["offset"] = int(m["off"])
    if m["bid"]:
        frame["build_id"] = m["bid"]
    return frame


def frame_key(frame: dict[str, Any]) -> str:
    """'libvhal.so!VehicleHal::set' or 'libvhal.so+0x4a2b0' (unsymbolized)."""
    lib = basename(frame["map"])
    if "func" in frame:
        return f"{lib}!{strip_args(frame['func'])}"
    return f"{lib}+0x{int(frame['pc'], 16):x}"


def frame_text(frame: dict[str, Any]) -> str:
    lib = basename(frame["map"])
    if "func" in frame:
        return f"#{frame['n']:02d} {lib} ({frame['func']}+{frame['offset']})"
    return f"#{frame['n']:02d} {lib} (pc {frame['pc']})"


def parse(lines: list[str], file_time: int | None = None) -> Parsed:
    sec = Sections()
    f: dict[str, Any] = {}
    frames: list[dict[str, Any]] = []
    threads = 0
    state = "header"
    for line in lines:
        stripped = line.strip()
        if state not in ("threads", "open_files", "log"):
            if state == "header" and REGISTER.match(line):
                state = "registers"
                sec.start("registers")
            elif stripped == "backtrace:":
                state = "backtrace"
                sec.start("backtrace")
            elif stripped == "stack:" or stripped.startswith(("memory near ", "code around ")):
                if state != "memory":
                    state = "memory"
                    sec.start("memory")
            elif stripped.startswith("memory map ("):
                state = "memory_map"
                sec.start("memory_map")
        if stripped == THREAD_SEP and state not in ("open_files", "log"):
            if state != "threads":
                state = "threads"
                sec.start("threads")
            threads += 1
        elif stripped == "open files:" and state != "log":
            state = "open_files"
            sec.start("open_files")
        elif LOG_START.match(stripped):
            if state != "log":
                state = "log"
                sec.start("log")
            f.setdefault("log_buffers", []).append(LOG_START.match(stripped)[1])  # type: ignore[index]
        sec.add(line)

        if state == "header":
            _header_line(stripped, f)
        elif state == "backtrace":
            fr = parse_frame(line)
            if fr is not None:
                frames.append(fr)
    p = Parsed("tombstone", sec.result(), consumed=len(lines))
    if frames:
        f["frames"] = frames
    f["other_threads"] = threads
    p.pid, p.tid = f.pop("pid", None), f.pop("tid", None)
    p.process = f.get("process") or f.get("cmdline")
    ts = f.pop("_ts", None)
    if ts is not None:
        p.ts, p.ts_raw = ts
    elif file_time is not None:
        p.ts = file_time
        f["ts_from"] = "file time"
    p.fields = f
    return p


def _header_line(s: str, f: dict[str, Any]) -> None:
    if s.startswith("Build fingerprint:"):
        f["fingerprint"] = s.split(":", 1)[1].strip().strip("'")
    elif s.startswith("ABI:"):
        f["abi"] = s.split(":", 1)[1].strip().strip("'")
    elif s.startswith("Timestamp:"):
        raw = s.split(":", 1)[1].strip()
        t = parse_wall(raw)
        if t is not None:
            f["_ts"] = (t[0], raw)
            if t[1]:
                f["utc_offset"] = t[1]
    elif s.startswith("Process uptime:"):
        f["process_uptime"] = s.split(":", 1)[1].strip()
    elif s.startswith("Cmdline:"):
        f["cmdline"] = s.split(":", 1)[1].strip()
    elif s.startswith("uid:"):
        f["uid"] = s.split(":", 1)[1].strip()
    elif s.startswith("Cause:"):
        f["cause"] = s.split(":", 1)[1].strip()
    elif s.startswith("Abort message:"):
        f["abort_message"] = s.split(":", 1)[1].strip().strip("'")
    elif "pid" not in f and (m := PID_LINE.match(s)):
        f["pid"], f["tid"] = int(m[1]), int(m[2])
        f["thread"] = m[3]
        f["process"] = m[4].strip()
    elif "signal" not in f and (m := SIGNAL.match(s)):
        f["signal"] = {"number": int(m[1]), "name": m[2], "code": int(m[3]), "code_name": m[4]}
        if m[5] and not m[5].startswith("-"):
            f["signal"]["fault_addr"] = m[5]


def signature_frames(f: dict[str, Any]) -> list[str]:
    frames: list[dict[str, Any]] = f.get("frames", [])
    if f.get("signal", {}).get("name") == "SIGABRT" or "abort_message" in f:
        rest = [fr for fr in frames if basename(fr["map"]) not in ABORT_LIBS]
        frames = rest or frames
    return [frame_key(fr) for fr in frames[:SIGNATURE_FRAMES]]


def describe(p: Parsed) -> tuple[str, str, str | None]:
    f = p.fields
    sig = f.get("signal", {})
    kind = sig.get("name") or "native crash"
    proc = p.process or "unknown process"
    short = basename(proc.split()[0]) if proc else "?"
    where = f"pid {p.pid}, tid {p.tid}" if p.pid is not None else "pid unknown"
    if f.get("thread"):
        where += f' "{f["thread"]}"'
    frames: list[dict[str, Any]] = f.get("frames", [])
    top = signature_frames(f)
    lines = [f"Native crash in {proc} ({where}) at {fmt_time(p.ts)}"]
    if sig:
        s = f"signal {sig['number']} ({sig['name']}), code {sig['code']} ({sig['code_name']})"
        if "fault_addr" in sig:
            s += f", fault addr {sig['fault_addr']}"
        lines.append(s)
    if "cause" in f:
        lines.append(f"Cause: {f['cause']}")
    if "abort_message" in f:
        lines.append(f"Abort message: '{f['abort_message']}'")
    if frames:
        lines.append("backtrace:")
        lines += [f"  {frame_text(fr)}" for fr in frames[:8]]
        if len(frames) > 8:
            lines.append(f"  … {len(frames) - 8} more frames")
    if f.get("other_threads"):
        lines.append(f"other threads: {f['other_threads']}")
    headline = f"Native crash {kind} in {short} ({where})"
    if "abort_message" in f:
        headline += f": Abort message '{f['abort_message']}'"
    elif "cause" in f:
        headline += f": {f['cause']}"
    if top:
        headline += f" at {top[0]}"
    return headline, "\n".join(lines), signature(kind, short, top)


def from_lines(lines: list[str], file_time: int | None = None) -> RawArtifact:
    return to_artifact(parse(lines, file_time), describe)


class TombstoneConnector(TextConnector):
    name = "tombstone"
    version = "1"

    def sniff(self, head: bytes, filename: str) -> float:
        lines = head_lines(head, 30)
        if not any(ln.startswith(BANNER) for ln in lines[:5]):
            return 0.0
        score = 0.8
        if any(PID_LINE.match(ln) for ln in lines):
            score += 0.15
        if filename.startswith("tombstone"):
            score += 0.04
        return score

    def parse_lines(self, lines: Iterator[str], ctx: SourceContext) -> Iterator[ParseItem]:
        text = list(lines)
        if any(ln.strip() for ln in text):
            yield from_lines(text, section_time(ctx.meta))
