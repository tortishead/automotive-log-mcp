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
- ``VM TRACES AT LAST ANR``, ``TOMBSTONE ...`` and ``DROPBOX ...`` (``dumpsys dropbox -p``)
  sections go to the artifact parsers (``anr``, ``tombstone``, ``dropbox``).
- ``BINDER TRANSACTIONS`` gives outgoing binder calls at capture time (``ContainerContext.facts``),
  used for the binder peer of an ANR.

Section headers that name a dumped file (``(path: mtime)``) give the source metadata
``section_file``/``section_time``. Other sections are not indexed; the import notes how many.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from ..core.timeparse import NS, wall_ns
from .artifacts import dumpstate_section as ds
from .base import ContainerContext, Member, head_lines, iter_text_lines

DUMPSTATE = re.compile(r"== dumpstate: (\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)")
SECTION = ds.SECTION
SECTION_END = ds.SECTION_END


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
                up = ds.uptime_seconds(line)
                if up is not None:
                    meta["uptime_s"] = up
            elif line.startswith("Bugreport format version:"):
                meta["bugreport_format"] = line.split(":", 1)[1].strip()
        self._boot_estimate(meta)

        skipped: list[str] = []
        while header_line is not None:
            h = ds.header(header_line)
            assert h is not None
            state: dict[str, Any] = {"next": None}
            body = self._section(lines, h.title, state)
            sub = self._member_for(member, h, body, ctx)
            if sub is not None:
                yield sub
            elif ds.is_processes(h):
                ctx.processes.extend(ds.parse_ps(body))
            elif ds.is_properties(h):
                ds.parse_props(body, meta)
            elif ds.is_binder_transactions(h):
                ctx.facts.setdefault("binder_outgoing", []).extend(ds.parse_binder_transactions(body))
            elif h.title == "UPTIME":
                for line in body:
                    up = ds.uptime_seconds(line)
                    if up is not None and "uptime_s" not in meta:
                        meta["uptime_s"] = up
                        self._boot_estimate(meta)
            else:
                skipped.append(h.title)
            for _ in body:  # drain whatever the consumer left
                pass
            header_line = state["next"]
            if header_line is None:
                header_line = self._next_header(lines)
        if skipped:
            ctx.notes.append(f"{member.name}: {len(skipped)} sections not indexed in this version")

    @staticmethod
    def _boot_estimate(meta: dict[str, Any]) -> None:
        if "dumpstate_ns" in meta and "uptime_s" in meta:
            # Minute resolution at best: metadata for the clock model (M5), not used for ts.
            meta["boot_time_estimate"] = meta["dumpstate_ns"] - meta["uptime_s"] * NS

    @staticmethod
    def _next_header(lines: Iterator[str]) -> str | None:
        """Skip text between sections (after a duration line) up to the next section header."""
        for line in lines:
            if line.startswith("------ ") and not SECTION_END.match(line) and SECTION.match(line):
                return line
        return None

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
        parent: Member, h: ds.Header, body: Iterator[str], ctx: ContainerContext
    ) -> Member | None:
        target = ds.format_for(h)
        if target is None:
            return None
        fmt, stream = target
        return Member(
            name=f"{parent.name}#{h.title}",
            lines=body,
            format=fmt,
            stream=stream,
            meta=h.meta(),
            ref_year=ctx.ref_year,
            ref_month=ctx.ref_month,
            optional=False,
        )
