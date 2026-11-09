"""ANR traces: thread dumps written when an app or the system did not respond.

Read from ``/data/anr/anr_*`` (bugreport zip ``FS/``), the ``VM TRACES AT LAST ANR`` section of
the bugreport text and ``*_anr`` dropbox entries. A trace holds one or more process blocks; the
first is the process that did not respond::

    Subject: Input dispatching timed out (...)          (optional, before the first block)
    ----- pid 1234 at 2026-01-01 00:00:03.456789012+0100 -----
    Cmd line: com.example.hvac
    DALVIK THREADS (12):
    "main" prio=5 tid=1 Blocked
      | group="main" sCount=1 ...
      | sysTid=1234 nice=-10 ...
      at com.example.hvac.Controller.update(Controller.java:42)
      - waiting to lock <0x0e3f5a21> (a java.lang.Object) held by thread 12
    ...
    ----- end 1234 -----

Native processes (dumped by debuggerd) have threads ``"name" sysTid=830`` with ``#00 pc`` frames.

Parsed: the main thread's state and frames, the chain of lock holders from the main thread
(with deadlock detection), and whether the last thread in that chain is blocked in a binder call
(interface and method from the frames). The binder peer (which process the call went to) is not
in the trace; :func:`complete` fills it from the bugreport's ``BINDER TRANSACTIONS`` section.

Sections: ``header`` (lines before the first block), ``p<pid>`` (process block header) and
``p<pid>.t<tid>.<thread-name>`` per thread.
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
    fmt_time,
    parse_wall,
    section_time,
    signature,
    slug,
    strip_args,
    to_artifact,
    to_int,
)
from .tombstone import frame_key, parse_frame

PROC_START = re.compile(r"----- pid (\d+) at (.*?) -----$")
PROC_END = re.compile(r"----- end (\d+) -----$")
JAVA_THREAD = re.compile(r'"(.*)"( daemon)? prio=(\d+) tid=(\d+) (\S+)(.*)$')
NATIVE_THREAD = re.compile(r'"(.*)" sysTid=(\d+)\s*$')
SYSTID = re.compile(r"\|.*\bsysTid=(\d+)")
JAVA_FRAME = re.compile(r"\s+at (.+)$")
NATIVE_FRAME = re.compile(r"\s+(?:native: )?#\d+ pc ")
WAIT_LOCK = re.compile(
    r"\s+- waiting to lock (?:<(0x[0-9a-f]+)> \(a (.+?)\)|an unknown object)(?: held by thread (\d+))?"
)
WAIT_ON = re.compile(r"\s+- (waiting on|sleeping on|parking to wait for) <(0x[0-9a-f]+)> \(a (.+?)\)")
LOCKED = re.compile(r"\s+- locked <(0x[0-9a-f]+)> \(a (.+?)\)")
STUB_PROXY = re.compile(r"([\w$.]+?)\$Stub\$Proxy\.(\w+)")
NATIVE_PROXY = re.compile(r"(?:^|[:!])(Bp\w+)::(\w+)")
BINDER_FRAMES = (
    "android.os.BinderProxy.transact",
    "IPCThreadState::transact",
    "IPCThreadState::waitForResponse",
    "IPCThreadState::talkWithDriver",
)
SIGNATURE_FRAMES = 3
CHAIN_MAX = 8


def _new_thread(name: str, tid: int, state: str, sys_tid: int | None = None) -> dict[str, Any]:
    return {"name": name, "tid": tid, "state": state, "sys_tid": sys_tid, "frames": [], "locked": []}


def parse(lines: list[str], header: dict[str, str] | None = None, file_time: int | None = None) -> Parsed:
    sec = Sections()
    procs: list[dict[str, Any]] = []
    proc: dict[str, Any] | None = None
    thread: dict[str, Any] | None = None
    subject = (header or {}).get("Subject")
    for line in lines:
        m = PROC_START.match(line.strip())
        if m is not None:
            pid = int(m[1])
            proc = {"pid": pid, "time": m[2], "threads": []}
            procs.append(proc)
            thread = None
            sec.start(f"p{pid}")
            sec.add(line)
            continue
        if proc is None:
            if subject is None and line.startswith("Subject:"):
                subject = line.split(":", 1)[1].strip()
            sec.add(line)
            continue
        jm = JAVA_THREAD.match(line)
        nm = None if jm else NATIVE_THREAD.match(line)
        if jm is not None:
            thread = _new_thread(jm[1], int(jm[4]), jm[5])
            thread["prio"] = int(jm[3])
            if jm[2]:
                thread["daemon"] = True
        elif nm is not None:
            thread = _new_thread(nm[1], int(nm[2]), "Native", int(nm[2]))
        if thread is not None and (jm or nm):
            proc["threads"].append(thread)
            sec.start(f"p{proc['pid']}.t{thread['tid']}.{slug(thread['name'], 24)}")
            sec.add(line)
            continue
        sec.add(line)
        if PROC_END.match(line.strip()):
            thread = None
            continue
        if thread is None:
            if line.startswith("Cmd line:"):
                proc["cmdline"] = line.split(":", 1)[1].strip()
            elif line.startswith("Build fingerprint:") and "fingerprint" not in proc:
                proc["fingerprint"] = line.split(":", 1)[1].strip().strip("'")
            continue
        _thread_line(line, thread)
    p = Parsed("anr", sec.result(), level="E", consumed=len(lines))
    f = p.fields
    if subject:
        f["subject"] = subject
    if procs:
        first = procs[0]
        p.pid = first["pid"]
        p.process = first.get("cmdline")
        t = parse_wall(first["time"])
        if t is not None:
            p.ts, p.ts_raw = t[0], first["time"]
            if t[1]:
                f["utc_offset"] = t[1]
        if "fingerprint" in first:
            f["fingerprint"] = first["fingerprint"]
        _analyse(first, f)
        others = [{"pid": q["pid"], "process": q.get("cmdline")} for q in procs[1:]]
        if others:
            f["other_processes"] = others
        f["pids"] = {str(q["pid"]): q.get("cmdline") for q in procs if q.get("cmdline")}
    if header:
        p.process = p.process or header.get("Process")
        p.pid = p.pid if p.pid is not None else to_int(header.get("PID"))
    if p.ts is None and file_time is not None:
        p.ts = file_time
        f["ts_from"] = "file time"
    return p


def _thread_line(line: str, t: dict[str, Any]) -> None:
    if line.lstrip().startswith("|"):
        m = SYSTID.search(line)
        if m is not None:
            t["sys_tid"] = int(m[1])
        return
    m = JAVA_FRAME.match(line)
    if m is not None:
        t["frames"].append(m[1].strip())
        return
    if NATIVE_FRAME.match(line):
        fr = parse_frame(line.replace("native: ", "", 1))
        if fr is not None:
            t.setdefault("native", []).append(frame_key(fr))
        return
    m = WAIT_LOCK.match(line)
    if m is not None and "waiting" not in t:
        w: dict[str, Any] = {"lock": m[1], "class": m[2]} if m[1] else {"lock": None}
        if m[3]:
            w["holder_tid"] = int(m[3])
        t["waiting"] = w
        return
    m = WAIT_ON.match(line)
    if m is not None and "waiting_on" not in t:
        t["waiting_on"] = {"how": m[1], "lock": m[2], "class": m[3]}
        return
    m = LOCKED.match(line)
    if m is not None:
        t["locked"].append(m[1])


def _brief(t: dict[str, Any], frames: int) -> dict[str, Any]:
    out = {k: t[k] for k in ("name", "tid", "sys_tid", "state") if t.get(k) is not None}
    if t["frames"]:
        out["frames"] = t["frames"][:frames]
    if t.get("native"):
        out["native"] = t["native"][:frames]
    if "waiting" in t:
        out["waiting"] = t["waiting"]
    return out


def _binder(t: dict[str, Any]) -> dict[str, Any] | None:
    frames = t["frames"] + t.get("native", [])
    if not any(b in fr for fr in frames for b in BINDER_FRAMES):
        return None
    out: dict[str, Any] = {"tid": t["tid"], "sys_tid": t.get("sys_tid"), "thread": t["name"]}
    for fr in frames:
        m = STUB_PROXY.search(fr) or NATIVE_PROXY.search(fr)
        if m is not None:
            out["interface"], out["method"] = m[1], m[2]
            break
    return out


def _analyse(proc: dict[str, Any], f: dict[str, Any]) -> None:
    threads: list[dict[str, Any]] = proc["threads"]
    f["threads"] = len(threads)
    main = next((t for t in threads if t["tid"] == 1 or t["name"] == "main"), threads[0] if threads else None)
    if main is None:
        return
    f["main"] = _brief(main, 10)
    by_tid = {t["tid"]: t for t in threads}
    holders_by_lock = {lock: t for t in threads for lock in t["locked"]}
    chain: list[dict[str, Any]] = []
    seen = {main["tid"]}
    cur = main
    while "waiting" in cur and len(chain) < CHAIN_MAX:
        w = cur["waiting"]
        holder = by_tid.get(w.get("holder_tid", -1)) or holders_by_lock.get(w.get("lock") or "")
        if holder is None:
            if "holder_tid" in w:
                chain.append({"tid": w["holder_tid"], "lock": w.get("lock"), "missing": True})
            break
        entry = _brief(holder, 6)
        entry["lock"] = w.get("lock")
        if w.get("class"):
            entry["lock_class"] = w["class"]
        chain.append(entry)
        if holder["tid"] in seen:
            f["deadlock"] = True
            break
        seen.add(holder["tid"])
        cur = holder
    if chain:
        f["lock_chain"] = chain
    binder = _binder(cur)
    if binder is not None:
        f["binder"] = binder


def _thread_label(t: dict[str, Any]) -> str:
    label = f"tid {t['tid']}"
    if t.get("name"):
        label += f' "{t["name"]}"'
    if t.get("state"):
        label += f" ({t['state']})"
    return label


def _binder_text(b: dict[str, Any]) -> str:
    call = f"{b['interface'].rsplit('.', 1)[-1]}.{b['method']}" if "interface" in b else "a binder call"
    text = f"in binder call {call}"
    peer = b.get("peer")
    if peer:
        text += f" -> pid {peer['pid']}" + (f" ({peer['process']})" if peer.get("process") else "")
    return text


def describe(p: Parsed) -> tuple[str, str, str | None]:
    f = p.fields
    proc = p.process or "unknown process"
    where = f"pid {p.pid}" if p.pid is not None else "pid unknown"
    lines = [f"ANR in {proc} ({where}) at {fmt_time(p.ts)}"]
    if f.get("subject"):
        lines.append(f"Subject: {f['subject']}")
    main = f.get("main")
    head = f"ANR in {proc} ({where})"
    if f.get("subject"):
        head += f": {f['subject']}"
    sig_frames: list[str] = []
    if main:
        lines.append(f"main thread ({_thread_label(main)}):")
        lines += [f"  at {fr}" for fr in main.get("frames", [])[:5]]
        if not main.get("frames"):
            lines += [f"  {fr}" for fr in main.get("native", [])[:5]]
        top = (main.get("frames") or main.get("native") or [""])[0]
        head += ("; " if f.get("subject") else ": ") + f"main {main.get('state', '?')}"
        if top:
            head += f" at {strip_args(top)}"
        frames = main.get("frames") or main.get("native") or []
        sig_frames = [strip_args(fr) for fr in frames[:SIGNATURE_FRAMES]]
    for i, h in enumerate(f.get("lock_chain", [])):
        lock = f"<{h['lock']}>" if h.get("lock") else "a lock"
        if h.get("lock_class"):
            lock += f" (a {h['lock_class']})"
        lines.append(f"{'  ' * i}waiting to lock {lock} held by {_thread_label(h)}")
        lines += [f"{'  ' * i}  at {fr}" for fr in h.get("frames", [])[:4]]
        if i == 0:
            head += f", lock held by {_thread_label(h)}"
    if f.get("deadlock"):
        lines.append("deadlock: the lock chain returns to a thread already in it")
        head += ", deadlock"
    if "binder" in f:
        b = f["binder"]
        lines.append(f"{_thread_label({'tid': b['tid'], 'name': b['thread']})} is {_binder_text(b)}")
        head += f", {_binder_text(b)}"
    if f.get("threads"):
        extra = f"threads in {proc}: {f['threads']}"
        if f.get("other_processes"):
            extra += "; also dumped: " + ", ".join(
                f"{o['process'] or '?'} ({o['pid']})" for o in f["other_processes"][:5]
            )
        lines.append(extra)
    state = main.get("state", "?") if main else "?"
    return head, "\n".join(lines), signature(f"ANR {state}", p.process, sig_frames)


def complete(art: RawArtifact, outgoing: list[dict[str, int]], names: dict[int, str]) -> bool:
    """Fill the binder peer of a blocked binder call from ``BINDER TRANSACTIONS`` (outgoing
    transactions at bugreport time) and pid -> process names. Returns True if fields changed."""
    b = art.fields.get("binder")
    if not b or "peer" in b or art.pid is None or b.get("sys_tid") is None:
        return False
    for t in outgoing:
        if t["from_pid"] == art.pid and t["from_tid"] == b["sys_tid"]:
            peer: dict[str, Any] = {"pid": t["to_pid"], "tid": t["to_tid"], "from": "BINDER TRANSACTIONS"}
            if t["to_pid"] in names:
                peer["process"] = names[t["to_pid"]]
            b["peer"] = peer
            return True
    return False


def from_lines(
    lines: list[str], header: dict[str, str] | None = None, file_time: int | None = None
) -> RawArtifact:
    return to_artifact(parse(lines, header, file_time), describe)


class AnrConnector(TextConnector):
    name = "anr"
    version = "1"

    def sniff(self, head: bytes, filename: str) -> float:
        lines = head_lines(head, 40)
        if not any(PROC_START.match(ln.strip()) for ln in lines):
            return 0.0
        score = 0.75
        if any(ln.startswith("Cmd line:") for ln in lines):
            score += 0.15
        if filename.startswith(("anr_", "traces")):
            score += 0.05
        return score

    def parse_lines(self, lines: Iterator[str], ctx: SourceContext) -> Iterator[ParseItem]:
        text = list(lines)
        if any(ln.strip() for ln in text):
            yield from_lines(text, None, section_time(ctx.meta))
