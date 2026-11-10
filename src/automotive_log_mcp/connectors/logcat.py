"""Android logcat text output, all ``-v`` formats.

Formats: ``threadtime`` (default), ``time``, ``brief``, ``process``, ``tag``, ``thread``,
``long`` and ``raw`` (raw lines become records without header fields). Modifiers: ``year``,
``epoch``, ``monotonic``, ``usec``/``nsec`` (any fraction length), ``zone``/``UTC`` (zone kept in
the source metadata), ``uid``, ``printable``, ``color`` (ANSI escapes removed), ``descriptive``.

Buffer markers (``--------- beginning of main``, ``--------- switch to crash``) name the logcat
buffer. Logcat prints ``beginning of <buffer>`` only before the first record of each buffer;
``switch to <buffer>`` lines appear only with ``-D``/``--dividers``. Without dividers (the
bugreport ``SYSTEM LOG``), the buffers interleave after the second marker with no further marker,
so the log stays one source and the source is renamed after the buffers it holds
(``main+system+crash``). With dividers, each buffer becomes its own source. A logcat command known
from the bugreport section header decides; otherwise records after a second ``beginning of`` are
held back until a ``switch to`` line shows dividers (or ``HOLD_RECORDS`` records pass without one).

A message logged with embedded newlines is printed by logcat as several lines with the same
header; such lines are merged into one record when the header is identical and the continuation
looks like one (stack frames, ``Caused by:``, indented text, ...). Lines that match no format
become records with ``level=None`` and the raw text.

An ``AndroidRuntime`` record starting ``FATAL EXCEPTION`` (normally in the crash buffer) carries a
``java_crash`` artifact; the record itself is the artifact's line on the timeline.
"""

from __future__ import annotations

import re
from collections.abc import Generator, Iterator
from typing import Any

from ..core.timeparse import NS, YearTracker, day_ns, frac_ns
from ..model import RawRecord, StreamMarker
from .artifacts import java_crash
from .base import ParseItem, SourceContext, TextConnector, head_lines

_LVL = "VDIWEFAS"
_LEVEL_MAP = {"V": "V", "D": "D", "I": "I", "W": "W", "E": "E", "F": "F", "A": "F", "S": None}

# Timestamp: [YYYY-]MM-DD HH:MM:SS[.frac] [zone]  |  seconds[.frac] (epoch or monotonic)
_TS = (
    r"(?:((?:\d{4}-)?\d\d-\d\d) (\d\d:\d\d:\d\d(?:\.\d+)?)|(\d+(?:\.\d+)?))"
    r"(?: ([+-]\d{4}|[A-Z]{3,4}))?"
)
# threadtime (+uid): <ts> [uid] pid tid L tag: msg
THREADTIME = re.compile(
    r"\s*" + _TS + r"\s+(?:([\w]+)\s+)?(\d+)\s+(\d+)\s+([" + _LVL + r"]) (.*?)\s*:(?: |$)"
)
# time: <ts> L/tag(pid): msg
TIME = re.compile(r"\s*" + _TS + r"\s+([" + _LVL + r"])/(.*?)\(\s*(\d+)\):(?: |$)")
# long header: [ <ts> [uid:] pid: tid L/tag ]
LONG = re.compile(r"\[ " + _TS + r"\s+(?:([\w]+):\s+)?(\d+):\s*(\d+) ([" + _LVL + r"])/(.*?)\s*\]$")
BRIEF = re.compile(r"([" + _LVL + r"])/(.*?)\(\s*(\d+)\):(?: |$)")
PROCESS = re.compile(r"([" + _LVL + r"])\(\s*(\d+)\) (.*?)  \((.*)\)$")
THREAD = re.compile(r"([" + _LVL + r"])\(\s*(\d+):\s*(\d+)\) ")
TAG = re.compile(r"([" + _LVL + r"])/([^\s:(][^:(]*?):(?: |$)")
MARKER = re.compile(r"--------- (beginning of|switch to) (\w+)")
DIVIDERS = re.compile(r"(?:^|\s)(?:-D|--dividers)(?:\s|$)")
HOLD_RECORDS = 2000  # records after an ambiguous marker before deciding "no dividers"
ANSI = re.compile(r"\x1b\[[0-9;]*m")

CONTINUATION = re.compile(
    r"\s|at |Caused by: |Suppressed: |\.\.\. \d+ more|Process: |backtrace:|#\d+ pc "
    r"|[\w$]+(?:\.[\w$]+)+(?:Exception|Error|Throwable)\b|\*\*\* "
)

BOOT_TAGS = frozenset({"boot_progress_start"})
FATAL_PREFIXES = ("FATAL EXCEPTION", "*** FATAL EXCEPTION")


class _Clock:
    """Turns logcat timestamp groups into (ts, ts_src, monotonic) with year inference."""

    def __init__(self, ctx: SourceContext) -> None:
        self.years = YearTracker(ctx.ref_year, ctx.ref_month)
        self.ctx = ctx
        self._last_date: str | None = None
        self._last_base = 0
        self.boot_ns: int | None = ctx.meta.get("boot_time_ns")

    def wall(self, date: str, time: str) -> int:
        if date != self._last_date:
            if len(date) == 10:  # YYYY-MM-DD
                base = day_ns(int(date[:4]), int(date[5:7]), int(date[8:10]))
            else:
                month = int(date[:2])
                base = day_ns(self.years.year_for(month), month, int(date[3:5]))
            self._last_date = date
            self._last_base = base
        t = time
        secs = int(t[0:2]) * 3600 + int(t[3:5]) * 60 + int(t[6:8])
        return self._last_base + secs * NS + (frac_ns(t[9:]) if len(t) > 9 else 0)

    def convert(self, date: str | None, time: str | None, secs: str | None) -> tuple[int, int | None, bool]:
        """Return (ts_src, ts, monotonic)."""
        if date is not None and time is not None:
            ts = self.wall(date, time)
            return ts, ts, False
        assert secs is not None
        whole, _, frac = secs.partition(".")
        value = int(whole) * NS + frac_ns(frac)
        if len(whole) >= 9:  # -v epoch
            return value, value, False
        # -v monotonic: seconds since boot
        return value, (self.boot_ns + value if self.boot_ns is not None else None), True


def _int(s: str | None) -> int | None:
    return int(s) if s else None


def _dividers(ctx: SourceContext) -> bool | None:
    """True/False when the logcat command is known (bugreport section header), else None."""
    cmd = ctx.meta.get("section_command")
    if not isinstance(cmd, str) or not cmd.startswith("logcat"):
        return None
    return DIVIDERS.search(cmd) is not None


def _resolve_buffers(items: Iterator[ParseItem], ctx: SourceContext) -> Iterator[ParseItem]:
    """Turn buffer markers into stream switches or joins (see the module docstring)."""
    dividers = _dividers(ctx)
    begun = 0
    held: list[ParseItem] | None = None  # from the ambiguous marker on, while undecided
    held_records = 0

    def release(split: bool) -> Iterator[ParseItem]:
        assert held is not None
        for it in held:
            if type(it) is StreamMarker and not it.switch and not split:
                yield StreamMarker(it.stream, join=True)
            else:
                yield it

    for item in items:
        if type(item) is StreamMarker:
            if item.switch:
                if held is not None:
                    yield from release(True)
                    held = None
                dividers = True
                yield item
                continue
            begun += 1
            if begun == 1 or dividers:
                yield item
            elif dividers is False:
                yield StreamMarker(item.stream, join=True)
            elif held is not None:
                held.append(item)
            else:
                held, held_records = [item], 0
            continue
        if held is None:
            yield item
            continue
        held.append(item)
        held_records += 1
        if held_records >= HOLD_RECORDS:
            yield from release(False)
            held, dividers = None, False
    if held is not None:
        yield from release(False)


class LogcatConnector(TextConnector):
    name = "logcat"
    version = "2"  # 2: buffers without dividers stay one source (main+system+crash)

    def sniff(self, head: bytes, filename: str) -> float:
        lines = [ANSI.sub("", ln) for ln in head_lines(head, 60) if ln.strip()]
        if not lines:
            return 0.0
        strong = weak = markers = 0
        for ln in lines:
            if MARKER.match(ln):
                markers += 1
            elif THREADTIME.match(ln) or TIME.match(ln) or LONG.match(ln):
                strong += 1
            elif BRIEF.match(ln) or PROCESS.match(ln) or THREAD.match(ln) or TAG.match(ln):
                weak += 1
        n = len(lines)
        frac = (strong + 0.6 * weak + markers) / n
        if strong + weak == 0:
            return 0.0
        score = 0.3 + 0.65 * frac
        if markers:
            score += 0.05
        if "logcat" in filename.lower():
            score += 0.05
        return min(score, 0.95)

    def parse_lines(self, lines: Iterator[str], ctx: SourceContext) -> Iterator[ParseItem]:
        """Records, with a ``java_crash`` artifact attached to each ``FATAL EXCEPTION`` record."""
        items = self._parse(lines, ctx)
        try:
            for item in _resolve_buffers(items, ctx):
                if (
                    type(item) is RawRecord
                    and item.tag == "AndroidRuntime"
                    and item.msg.startswith(FATAL_PREFIXES)
                ):
                    item.artifact = java_crash.from_record(item)
                yield item
        finally:
            items.close()

    def _parse(self, lines: Iterator[str], ctx: SourceContext) -> Generator[ParseItem, None, None]:
        clock = _Clock(ctx)
        pending: RawRecord | None = None
        pending_key = ""
        long_mode = False
        blank = 0
        tt_match = THREADTIME.match
        cont_match = CONTINUATION.match
        level_map = _LEVEL_MAP
        fmt_unset = "ts_format" not in ctx.meta

        for line in lines:
            if "\x1b" in line:
                line = ANSI.sub("", line)
            m = tt_match(line)
            if m is not None:
                date, time, secs, zone, _uid, pid, tid, lvl, tag = m.groups()
                msg = line[m.end() :]
                key = line[: m.end()]
                if pending is not None and key == pending_key and cont_match(msg):
                    pending.msg += "\n" + msg
                    pending.lines += 1
                    continue
                if pending is not None:
                    yield pending
                ts_src, ts, mono = clock.convert(date, time, secs)
                if zone is not None and "zone" not in ctx.meta:
                    ctx.meta["zone"] = zone
                if date is not None and len(date) == 5 and len(time) == 12:
                    ts_raw = None  # reproducible from ts (ts_format md_ms)
                    if fmt_unset:
                        ctx.meta["ts_format"] = "md_ms"
                        fmt_unset = False
                else:
                    ts_raw = f"{date} {time}" if date is not None else secs
                attrs: dict[str, Any] | None = {"uid": _uid} if _uid is not None else None
                pending = RawRecord(
                    msg=msg,
                    ts_raw=ts_raw,
                    ts=ts,
                    ts_src=ts_src,
                    level=level_map[lvl],
                    tag=tag,
                    pid=int(pid),
                    tid=int(tid),
                    attrs=attrs,
                    monotonic=mono,
                    boot=tag in BOOT_TAGS,
                )
                pending_key = key
                long_mode = False
                continue

            if line.startswith("--------- "):
                mk = MARKER.match(line)
                if mk is not None:
                    if pending is not None:
                        yield pending
                        pending = None
                    long_mode = False
                    yield StreamMarker(mk.group(2), switch=mk.group(1) == "switch to")
                    continue

            if long_mode and pending is not None and not line.startswith("[ "):
                # Body of a 'long' record: everything up to the next header. Blank lines are
                # kept only between body lines (the one after each record is a separator).
                if not line:
                    blank += 1
                    continue
                if pending.lines == 0:
                    pending.msg = line
                else:
                    pending.msg += "\n" * (blank + 1) + line
                pending.lines += 1 + blank
                blank = 0
                continue
            blank = 0

            rec, key = self._other_formats(line, clock, ctx)
            if rec is None:
                if pending is not None:
                    yield pending
                    pending = None
                long_mode = False
                yield RawRecord(msg=line, unparsed=True)
                continue
            if key == "long":
                if pending is not None:
                    yield pending
                pending = rec
                pending_key = ""
                long_mode = True
                continue
            if pending is not None and key == pending_key and cont_match(rec.msg):
                pending.msg += "\n" + rec.msg
                pending.lines += 1
                continue
            if pending is not None:
                yield pending
            pending = rec
            pending_key = key
            long_mode = False

        if pending is not None:
            yield pending

    def _other_formats(self, line: str, clock: _Clock, ctx: SourceContext) -> tuple[RawRecord | None, str]:
        m = TIME.match(line)
        if m is not None:
            date, time, secs, zone, lvl, tag, pid = m.groups()
            ts_src, ts, mono = clock.convert(date, time, secs)
            if zone is not None and "zone" not in ctx.meta:
                ctx.meta["zone"] = zone
            rec = RawRecord(
                msg=line[m.end() :],
                ts_raw=f"{date} {time}" if date is not None else secs,
                ts=ts,
                ts_src=ts_src,
                level=_LEVEL_MAP[lvl],
                tag=tag.rstrip(),
                pid=int(pid),
                monotonic=mono,
                boot=tag in BOOT_TAGS,
            )
            return rec, line[: m.end()]
        m = LONG.match(line)
        if m is not None:
            date, time, secs, zone, uid, pid, tid, lvl, tag = m.groups()
            ts_src, ts, mono = clock.convert(date, time, secs)
            if zone is not None and "zone" not in ctx.meta:
                ctx.meta["zone"] = zone
            rec = RawRecord(
                msg="",
                ts_raw=f"{date} {time}" if date is not None else secs,
                ts=ts,
                ts_src=ts_src,
                level=_LEVEL_MAP[lvl],
                tag=tag,
                pid=int(pid),
                tid=int(tid),
                attrs={"uid": uid} if uid is not None else None,
                monotonic=mono,
                boot=tag in BOOT_TAGS,
                lines=0,
            )
            return rec, "long"
        m = BRIEF.match(line)
        if m is not None:
            lvl, tag, pid = m.groups()
            return RawRecord(
                msg=line[m.end() :], level=_LEVEL_MAP[lvl], tag=tag.rstrip(), pid=int(pid)
            ), line[: m.end()]
        m = PROCESS.match(line)
        if m is not None:
            lvl, pid, msg, tag = m.groups()
            return RawRecord(msg=msg, level=_LEVEL_MAP[lvl], tag=tag, pid=int(pid)), f"{lvl}({pid}) ({tag})"
        m = THREAD.match(line)
        if m is not None:
            lvl, pid, tid = m.groups()
            return RawRecord(msg=line[m.end() :], level=_LEVEL_MAP[lvl], pid=int(pid), tid=int(tid)), line[
                : m.end()
            ]
        m = TAG.match(line)
        if m is not None:
            lvl, tag = m.groups()
            return RawRecord(msg=line[m.end() :], level=_LEVEL_MAP[lvl], tag=tag.rstrip()), line[: m.end()]
        return None, ""
