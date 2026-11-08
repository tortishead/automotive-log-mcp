"""Segments: stretches of a source with one continuous clock.

A new segment starts at a boot marker, when a monotonic clock goes backwards (reboot or counter
reset), or when wall-clock time jumps backwards by more than 60 s (clock set back, or a log from
another boot appended). A forward jump is a gap, not a new segment.

New Year in logs without a year is handled when timestamps are parsed (see
:class:`automotive_log_mcp.core.timeparse.YearTracker`): December to January increments the year,
it does not start a segment.
"""

from __future__ import annotations

from dataclasses import dataclass

BACKWARD_JUMP_NS = 60 * 1_000_000_000


@dataclass(slots=True)
class Segment:
    seg: int
    reason: str
    first_seq: int
    last_seq: int
    count: int = 0
    ts_first: int | None = None
    ts_last: int | None = None
    ts_src_first: int | None = None
    ts_src_last: int | None = None


class SegmentTracker:
    def __init__(self) -> None:
        self.segments: list[Segment] = []
        self._last_ts: int | None = None
        self._last_src: int | None = None
        self._content = False  # current segment has a record that is not a boot marker

    def observe(self, seq: int, ts: int | None, ts_src: int | None, monotonic: bool, boot: bool) -> int:
        """Account one record; return its segment number (1-based)."""
        reason: str | None = None
        if not self.segments:
            reason = "boot" if boot else "start"
        elif boot and self._content:
            reason = "boot"
        elif monotonic and ts_src is not None and self._last_src is not None and ts_src < self._last_src:
            reason = "monotonic_reset"
        elif (
            not monotonic
            and ts is not None
            and self._last_ts is not None
            and ts < self._last_ts - BACKWARD_JUMP_NS
        ):
            reason = "wall_jump_back"
        if reason is not None:
            self.segments.append(Segment(len(self.segments) + 1, reason, seq, seq))
            self._last_ts = None
            self._last_src = None
            self._content = False
        cur = self.segments[-1]
        if not boot:
            self._content = True
        cur.last_seq = seq
        cur.count += 1
        if ts is not None:
            if cur.ts_first is None:
                cur.ts_first = ts
            cur.ts_last = ts
            if self._last_ts is None or ts > self._last_ts:
                self._last_ts = ts
        if ts_src is not None:
            if cur.ts_src_first is None:
                cur.ts_src_first = ts_src
            cur.ts_src_last = ts_src
            self._last_src = ts_src
        return cur.seg
