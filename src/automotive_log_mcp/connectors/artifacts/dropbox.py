"""Dropbox entries (``/data/system/dropbox``), by tag, delegating bodies to the other parsers.

Two inputs:

- Files ``<tag>@<ms since epoch>.txt`` (``.txt.gz`` is unpacked by the archive container), as in
  the bugreport zip ``FS/data/system/dropbox/``. The time in the name is UTC; it is converted to
  the device's wall clock with the bugreport's time zone (``persist.sys.timezone``) so it lines
  up with logcat. Without a known zone the UTC time is kept and ``fields.ts_zone`` says so.
- ``dumpsys dropbox -p <tag>`` output in bugreport sections (``DROPBOX SYSTEM APP CRASHES`` ...):
  entries separated by a line of ``=``, each starting ``YYYY-MM-DD hh:mm:ss <tag> (text, N bytes)``
  in device time.

Tags: ``*_crash`` -> ``java_crash``; ``*_anr`` -> ``anr``; ``*_native_crash`` and
``SYSTEM_TOMBSTONE`` -> ``tombstone``; anything else -> type ``dropbox`` (header fields, the
first body line and a Java stack if there is one, e.g. ``*_wtf``, ``*_strictmode``). Every entry
keeps ``fields.dropbox_tag``; the header (``Process:``, ``PID:``, ... up to the first blank
line) is the section ``dropbox_header``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from ...core.timeparse import fmt_wall
from ...model import ArtifactSection, RawArtifact
from ..base import ParseItem, SourceContext, TextConnector
from . import anr, java_crash, tombstone
from .common import Parsed, fmt_time, header_fields, parse_wall, signature, strip_args, to_artifact

FILENAME = re.compile(r"(?P<tag>[A-Za-z0-9_.-]+)@(?P<ms>\d{10,14})\.txt$")
ENTRY = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (\S+) \((.*)\)$")
SEPARATOR = re.compile(r"={20,}$")
HEADER_KEY = re.compile(r"[A-Z][\w-]*: ")


def body_type(tag: str) -> str:
    if tag.endswith("_native_crash") or tag.startswith("SYSTEM_TOMBSTONE"):
        return "tombstone"
    if tag.endswith("_crash"):
        return "java_crash"
    if tag.endswith("_anr"):
        return "anr"
    return "dropbox"


def _split_header(lines: list[str]) -> tuple[list[str], list[str]]:
    """Header lines ('Key: value' up to the first blank line) and the body."""
    n = 0
    while n < len(lines) and lines[n].strip() and HEADER_KEY.match(lines[n]):
        n += 1
    if n == 0:
        return [], lines
    if n < len(lines) and not lines[n].strip():
        n += 1  # the blank separator belongs to the header
    return lines[:n], lines[n:]


def describe_generic(p: Parsed) -> tuple[str, str, str | None]:
    f = p.fields
    tag = f.get("dropbox_tag", "dropbox")
    proc = p.process
    who = f" from {proc}" + (f" (pid {p.pid})" if p.pid is not None else "") if proc else ""
    lines = [f"dropbox {tag}{who} at {fmt_time(p.ts)}"]
    exc = f.get("exception")
    first = f.get("first_line")
    if exc:
        lines.append(exc["class"] + (f": {exc['message']}" if exc.get("message") else ""))
        lines += [f"  at {fr}" for fr in f.get("frames", [])[:5]]
    elif first:
        lines.append(first)
    head = f"dropbox {tag}{who}"
    if exc:
        head += f": {exc['class']}" + (f": {exc['message']}" if exc.get("message") else "")
    elif first:
        head += f": {first}"
    frames = [strip_args(fr) for fr in f.get("frames", [])[:3]]
    kind = f"{tag} {exc['class']}" if exc else tag
    return head, "\n".join(lines), signature(kind, proc, frames)


DESCRIBE = {
    "tombstone": tombstone.describe,
    "java_crash": java_crash.describe,
    "anr": anr.describe,
    "dropbox": describe_generic,
}


def parse_entry(tag: str, lines: list[str], ts: int | None, ts_raw: str | None) -> RawArtifact:
    head, body = _split_header(lines)
    header = header_fields(head)
    kind = body_type(tag)
    if kind == "tombstone":
        p = tombstone.parse(body)
    elif kind == "anr":
        p = anr.parse(body, header)
    elif kind == "java_crash":
        p = java_crash.parse_body(body, header)
    else:
        p = java_crash.parse_body(body, header)
        p.type, p.level = "dropbox", "W"
        p.sections = [ArtifactSection("body", s.lines) for s in p.sections]
        first = next((ln.strip() for ln in body if ln.strip()), None)
        if first:
            p.fields["first_line"] = first
    if head:
        p.prepend(ArtifactSection("dropbox_header", head))
    p.consumed = len(lines)
    p.process = p.process or header.get("Process")
    if p.pid is None and header.get("PID", "").isdigit():
        p.pid = int(header["PID"])
    p.fields["dropbox_tag"] = tag
    if header:
        p.fields["dropbox_header"] = header
    if p.ts is None and ts is not None:  # the entry time, unless the body has its own
        p.ts, p.ts_raw = ts, ts_raw
    return to_artifact(p, DESCRIBE[kind])


def local_time(ms: int, zone: str | None) -> tuple[int, str, str]:
    """(wall-clock ns as the device writes it, ts_raw, zone used) for an epoch-ms timestamp."""
    utc = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=ms)
    used = "UTC"
    if zone:
        try:
            from zoneinfo import ZoneInfo

            local = utc.astimezone(ZoneInfo(zone))
            off = local.utcoffset() or timedelta(0)
            ms += int(off.total_seconds() * 1000)
            used = zone
        except (ValueError, OSError, KeyError, ImportError):
            pass
    ns = ms * 1_000_000
    return ns, fmt_wall(ns), used


class DropboxConnector(TextConnector):
    name = "dropbox"
    version = "1"

    def sniff(self, head: bytes, filename: str) -> float:
        if FILENAME.match(filename):
            return 0.9 if b"\x00" not in head[:4096] else 0.0
        text = head[:8192].decode("utf-8", errors="replace")
        if "Drop box contents:" in text or ("====================" in text and ENTRY.search(text)):
            return 0.7
        return 0.0

    def parse_lines(self, lines: Iterator[str], ctx: SourceContext) -> Iterator[ParseItem]:
        m = FILENAME.match(ctx.filename)
        if m is not None:
            text = list(lines)
            if not any(ln.strip() for ln in text):
                return
            ns, raw, used = local_time(int(m["ms"]), ctx.meta.get("timezone"))
            art = parse_entry(m["tag"], text, ns, raw)
            art.fields["ts_zone"] = used
            art.fields["epoch_ms"] = int(m["ms"])
            yield art
            return
        yield from self._dumpsys(lines)

    @staticmethod
    def _dumpsys(lines: Iterator[str]) -> Iterator[RawArtifact]:
        """Entries of ``dumpsys dropbox -p``; lines before the first entry are skipped."""
        entry: tuple[str, str, str] | None = None
        buf: list[str] = []
        skipped = 0

        def flush() -> Iterator[RawArtifact]:
            nonlocal skipped
            if entry is None:
                return
            when, tag, kind = entry
            while buf and (not buf[-1].strip() or SEPARATOR.match(buf[-1].strip())):
                buf.pop()
            body = buf[1:]
            t = parse_wall(when)
            art = parse_entry(tag, body, t[0] if t else None, when) if body else None
            if art is None:
                return
            # The entry line is the artifact's first line, so line numbers match the section text.
            art.sections.insert(0, ArtifactSection("entry", [buf[0]]))
            art.fields["dropbox_entry"] = kind
            art.consumed += skipped + 1
            skipped = 0
            yield art

        for line in lines:
            m = ENTRY.match(line.strip())
            if m is not None and (entry is None or SEPARATOR.match(buf[-1].strip())):
                yield from flush()
                entry = (m[1], m[2], m[3])
                buf = [line]
                continue
            if entry is None:
                skipped += 1
                continue
            buf.append(line)
        yield from flush()
