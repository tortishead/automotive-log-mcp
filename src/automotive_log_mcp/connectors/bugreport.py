"""Android bugreport text (``bugreport-*.txt``, the main entry of a bugreport zip).

The file is read once, front to back. The dumpstate header gives the capture time (reference
year for logcat lines), build fingerprint, kernel version and uptime. Sections
(``------ NAME (command) ------`` up to ``------ 0.1s was the duration of 'NAME' ------`` or the
next section) are handled by command:

- ``logcat ...`` sections become logcat members; buffer markers inside split them into one source
  per buffer (main, system, crash, events, radio, ...).
- ``dmesg`` and ``LAST KMSG`` sections become kernel members.
- ``SYSTEM PROPERTIES`` gives the fingerprint (if the header had none) and the time zone.
- ``PROCESSES ...`` / ``ps`` sections give the process list.
- ``UPTIME`` gives the uptime when the header has none.

Other sections are skipped in M1. Tombstones, ANR traces and dropbox entries are handed to
artifact parsers in M2.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from ..core.timeparse import NS, wall_ns
from ..model import ProcessInfo
from .base import ContainerContext, Member, head_lines, iter_text_lines

DUMPSTATE = re.compile(r"== dumpstate: (\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)")
SECTION = re.compile(r"------ (.*?)(?: \((.*)\))? ------$")
SECTION_END = re.compile(r"------ [\d.]+s was the duration of '(.*)' ------$")
PROP = re.compile(r"\[([^\]]+)\]: \[(.*)\]$")
UPTIME_WORDS = re.compile(
    r"up (\d+) weeks?, (\d+) days?, (\d+) hours?, (\d+) minutes?(?:, (\d+) seconds?)?", re.IGNORECASE
)
UPTIME_TOYBOX = re.compile(r"up (?:(\d+) days?,\s+)?(?:(\d+):(\d\d)|(\d+) min)", re.IGNORECASE)
LOGCAT_BUFFER = re.compile(r"-b\s+(\w+)")

KEEP_PROPS = (
    "ro.build.fingerprint",
    "ro.product.model",
    "ro.product.device",
    "ro.build.version.release",
    "ro.build.version.sdk",
    "ro.build.type",
    "persist.sys.timezone",
)


def _uptime_seconds(line: str) -> int | None:
    m = UPTIME_WORDS.search(line)
    if m:
        w, dd, hh, mm, ss = (int(x) if x else 0 for x in m.groups())
        return (((w * 7 + dd) * 24 + hh) * 60 + mm) * 60 + ss
    m = UPTIME_TOYBOX.search(line)
    if m:
        d, h, mi, only_min = m.groups()
        days = int(d) if d else 0
        if only_min:
            return days * 86400 + int(only_min) * 60
        return days * 86400 + int(h) * 3600 + int(mi) * 60
    return None


def _parse_ps(lines: list[str]) -> list[ProcessInfo]:
    """Parse ``ps`` output by its header (``USER PID [TID] PPID ... NAME|CMD``)."""
    out: list[ProcessInfo] = []
    header: list[str] | None = None
    for line in lines:
        if not line.strip():
            continue
        cols = line.split()
        if header is None:
            if "PID" in cols:
                header = cols
            continue
        if len(cols) < len(header):
            continue
        # The last column (NAME/CMD) may contain spaces.
        row = line.split(None, len(header) - 1)
        rec = dict(zip(header, row, strict=False))
        try:
            pid = int(rec["PID"])
        except (KeyError, ValueError):
            continue
        name = row[-1].strip()

        def num(key: str, rec: dict[str, str] = rec) -> int | None:
            v = rec.get(key)
            return int(v) if v is not None and v.isdigit() else None

        out.append(ProcessInfo(pid=pid, name=name, tid=num("TID"), ppid=num("PPID"), user=rec.get("USER")))
    return out


class BugreportContainer:
    name = "bugreport"
    version = "1"

    def sniff(self, head: bytes, filename: str) -> float:
        lines = head_lines(head, 10)
        if any(DUMPSTATE.match(ln) for ln in lines):
            return 0.98
        if b"== dumpstate:" in head[:4096]:
            return 0.9
        return 0.0

    def expand(self, member: Member, ctx: ContainerContext) -> Iterator[Member]:
        assert member.open is not None
        with member.open() as f:
            yield from self._expand_lines(member, iter_text_lines(f), ctx)

    def _expand_lines(self, member: Member, lines: Iterator[str], ctx: ContainerContext) -> Iterator[Member]:
        meta = ctx.meta
        header_line: str | None = None
        # Header: everything before the first section.
        for line in lines:
            if line.startswith("------ ") and SECTION.match(line):
                header_line = line
                break
            m = DUMPSTATE.match(line)
            if m:
                y, mo, d, hh, mi, ss = (int(x) for x in m.groups())
                meta["dumpstate_time"] = f"{y:04d}-{mo:02d}-{d:02d} {hh:02d}:{mi:02d}:{ss:02d}"
                meta["dumpstate_ns"] = wall_ns(y, mo, d, hh, mi, ss)
                ctx.ref_year, ctx.ref_month = y, mo
            elif line.startswith("Build fingerprint:"):
                meta["fingerprint"] = line.split(":", 1)[1].strip().strip("'")
            elif line.startswith("Build:"):
                meta["build"] = line.split(":", 1)[1].strip()
            elif line.startswith("Kernel:"):
                meta["kernel"] = line.split(":", 1)[1].strip()
            elif line.startswith("Uptime:"):
                up = _uptime_seconds(line)
                if up is not None:
                    meta["uptime_s"] = up
            elif line.startswith("Bugreport format version:"):
                meta["bugreport_format"] = line.split(":", 1)[1].strip()
        self._boot_estimate(meta)

        skipped: list[str] = []
        while header_line is not None:
            m = SECTION.match(header_line)
            assert m is not None
            title, command = m.group(1), m.group(2) or ""
            state: dict[str, Any] = {"next": None}
            body = self._section(lines, title, state)
            sub = self._member_for(member, title, command, body, ctx)
            if sub is not None:
                yield sub
            elif title.startswith("PROCESSES") or command.startswith("ps "):
                ctx.processes.extend(_parse_ps(list(body)))
            elif title == "SYSTEM PROPERTIES" or command == "getprop":
                self._props(body, meta)
            elif title == "UPTIME":
                for line in body:
                    up = _uptime_seconds(line)
                    if up is not None and "uptime_s" not in meta:
                        meta["uptime_s"] = up
                        self._boot_estimate(meta)
            else:
                skipped.append(title)
            for _ in body:  # drain whatever the consumer left
                pass
            header_line = state["next"]
        if skipped:
            ctx.notes.append(f"{member.name}: {len(skipped)} sections not indexed in this version")

    @staticmethod
    def _boot_estimate(meta: dict[str, Any]) -> None:
        if "dumpstate_ns" in meta and "uptime_s" in meta:
            # Minute resolution at best: metadata for the clock model (M5), not used for ts.
            meta["boot_time_estimate"] = meta["dumpstate_ns"] - meta["uptime_s"] * NS

    @staticmethod
    def _section(lines: Iterator[str], title: str, state: dict[str, Any]) -> Iterator[str]:
        for line in lines:
            if line.startswith("------ "):
                if SECTION_END.match(line):
                    return
                if SECTION.match(line):
                    state["next"] = line
                    return
            yield line

    @staticmethod
    def _member_for(
        parent: Member, title: str, command: str, body: Iterator[str], ctx: ContainerContext
    ) -> Member | None:
        cmd = command.strip()
        fmt: str | None = None
        stream: str | None = None
        if cmd.startswith("logcat"):
            fmt = "logcat"
            buffers = LOGCAT_BUFFER.findall(cmd)
            if len(buffers) == 1 and buffers[0] != "all":
                stream = buffers[0]
            elif title == "SYSTEM LOG":
                stream = "main"
        elif cmd.startswith("dmesg") or title in ("KERNEL LOG", "LAST KMSG") or "kmsg" in cmd:
            fmt = "kernel"
            stream = "last_kmsg" if title == "LAST KMSG" else "kernel"
        if fmt is None:
            return None
        return Member(
            name=f"{parent.name}#{title}",
            lines=body,
            format=fmt,
            stream=stream,
            ref_year=ctx.ref_year,
            ref_month=ctx.ref_month,
            optional=False,
        )

    @staticmethod
    def _props(body: Iterator[str], meta: dict[str, Any]) -> None:
        props = meta.setdefault("props", {})
        for line in body:
            m = PROP.match(line)
            if m and m.group(1) in KEEP_PROPS:
                props[m.group(1)] = m.group(2)
        if "fingerprint" not in meta and "ro.build.fingerprint" in props:
            meta["fingerprint"] = props["ro.build.fingerprint"]
        if "persist.sys.timezone" in props:
            meta["timezone"] = props["persist.sys.timezone"]
