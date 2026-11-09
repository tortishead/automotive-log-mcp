"""Java crashes: an uncaught exception with its stack trace and causes.

Two origins: a ``FATAL EXCEPTION`` record from logcat (normally the crash buffer; the logcat
connector merges the stack lines into one record and attaches the artifact to it), and dropbox
entries ``system_app_crash``, ``data_app_crash`` and ``system_server_crash`` (header lines, a blank
line, then the stack trace). Sections: ``header`` (dropbox header or the logcat ``FATAL EXCEPTION``
and ``Process:`` lines) and ``exception``.
"""

from __future__ import annotations

import re
from typing import Any

from ...model import RawArtifact, RawRecord
from .common import Parsed, Sections, fmt_time, signature, strip_args, to_artifact, to_int

EXCEPTION = re.compile(r"(?:(Caused by|Suppressed): )?([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+)(?:: (.*))?$")
FRAME = re.compile(r"\s*at (.+)$")
MORE = re.compile(r"\s*\.\.\. (\d+) more$")
FATAL = re.compile(r"(?:\*\*\* )?FATAL EXCEPTION(?: IN SYSTEM PROCESS)?: (.*)$")
PROCESS = re.compile(r"Process: ([^,]+)(?:, PID: (\d+))?")
SIGNATURE_FRAMES = 3


def parse_stack(lines: list[str]) -> list[dict[str, Any]]:
    """Exceptions in order (outer first): class, message, kind ('Caused by'/'Suppressed'), frames."""
    chain: list[dict[str, Any]] = []
    for line in lines:
        fm = FRAME.match(line)
        if fm is not None:
            if chain:
                chain[-1]["frames"].append(fm[1].strip())
            continue
        mm = MORE.match(line)
        if mm is not None:
            if chain:
                chain[-1]["more"] = int(mm[1])
            continue
        if not line.strip():
            continue
        em = EXCEPTION.match(line.strip())
        if em is not None and (em[1] or not chain):
            exc: dict[str, Any] = {"class": em[2], "frames": []}
            if em[3] is not None:
                exc["message"] = em[3]
            if em[1]:
                exc["kind"] = em[1]
            chain.append(exc)
        elif chain and len(chain) == 1 and not chain[0]["frames"]:
            # A multi-line exception message.
            chain[0]["message"] = (chain[0].get("message", "") + "\n" + line).lstrip("\n")
    return chain


def root_cause(chain: list[dict[str, Any]]) -> dict[str, Any] | None:
    causes = [e for e in chain if e.get("kind") != "Suppressed"]
    return causes[-1] if causes else None


def exception_fields(chain: list[dict[str, Any]], f: dict[str, Any]) -> None:
    if not chain:
        return
    outer, root = chain[0], root_cause(chain) or chain[0]
    f["exception"] = {k: outer[k] for k in ("class", "message") if k in outer}
    f["frames"] = outer["frames"][:10]
    if root is not outer:
        f["root_cause"] = {k: root[k] for k in ("class", "message") if k in root}
        f["root_frames"] = root["frames"][:10]
    f["causes"] = [e["class"] for e in chain[1:] if e.get("kind") == "Caused by"]


def _exc_line(e: dict[str, Any]) -> str:
    return str(e["class"]) + (f": {e['message']}" if e.get("message") else "")


def describe(p: Parsed) -> tuple[str, str, str | None]:
    f = p.fields
    proc = p.process or "unknown process"
    where = f"pid {p.pid}" if p.pid is not None else "pid unknown"
    if f.get("thread"):
        where += f", thread {f['thread']}"
    lines = [f"Java crash in {proc} ({where}) at {fmt_time(p.ts)}"]
    exc, root = f.get("exception"), f.get("root_cause")
    if exc:
        lines.append(_exc_line(exc))
        lines += [f"  at {fr}" for fr in f.get("frames", [])[: 3 if root else 6]]
    if root:
        lines.append(f"Caused by: {_exc_line(root)}" + (" (root cause)" if len(f["causes"]) > 1 else ""))
        lines += [f"  at {fr}" for fr in f.get("root_frames", [])[:6]]
    if not exc:
        lines.append("(no stack trace found)")
    key = root or exc
    headline = f"Java crash in {proc} ({where})"
    if key:
        headline += f": {_exc_line(key)}"
        top = f.get("root_frames" if root else "frames") or []
        if top:
            headline += f" at {top[0]}"
    frames = signature_frames_from_fields(f)
    kind = (key or {}).get("class", "java_crash")
    return headline, "\n".join(lines), signature(kind, p.process, frames)


def signature_frames_from_fields(f: dict[str, Any]) -> list[str]:
    frames = f.get("root_frames") or f.get("frames") or []
    return [strip_args(fr) for fr in frames[:SIGNATURE_FRAMES]]


def parse_body(lines: list[str], header: dict[str, str] | None = None) -> Parsed:
    """A dropbox crash body (header already split off) or any text holding a Java stack trace."""
    sec = Sections("exception")
    for line in lines:
        sec.add(line)
    p = Parsed("java_crash", sec.result(), consumed=len(lines))
    exception_fields(parse_stack(lines), p.fields)
    if header:
        p.process = header.get("Process")
        p.pid = to_int(header.get("PID"))
    return p


def from_record(rec: RawRecord) -> RawArtifact | None:
    """The artifact for a logcat ``FATAL EXCEPTION`` record (its message holds the stack)."""
    lines = rec.msg.split("\n")
    m = FATAL.match(lines[0].strip())
    if m is None:
        return None
    sec = Sections()
    head = 1
    p = Parsed("java_crash", [], pid=rec.pid, tid=rec.tid, ts=rec.ts, ts_raw=rec.ts_raw)
    p.fields["thread"] = m[1].strip()
    if "IN SYSTEM PROCESS" in lines[0]:
        p.process = "system_server"
    if len(lines) > 1 and (pm := PROCESS.match(lines[1].strip())):
        p.process = pm[1].strip()
        if pm[2]:
            p.pid = int(pm[2])
        head = 2
    for i, line in enumerate(lines):
        if i == head:
            sec.start("exception")
        sec.add(line)
    p.sections = sec.result()
    p.consumed = rec.lines
    exception_fields(parse_stack(lines[head:]), p.fields)
    p.fields["origin"] = "logcat"
    return to_artifact(p, describe)
