"""The clock model: one case clock over all sources (M5).

Records keep their timestamps as written (``ts`` wall clock, ``ts_src`` the source's own clock),
so citations, printed times and the ``from``/``to`` of the other tools do not change when the
alignment changes. The case clock is the clock of a reference source (by default the wall clock of
the largest source); every other segment maps to it piecewise::

    case = native + offset_ns + round(drift_ppm * 1e-6 * (native - start))

for ``native >= start`` (the first piece of a segment also covers earlier times). ``native`` is a
record's ``ts`` in a wall-clock segment and its ``ts_src`` in a monotonic segment (kernel time
since boot). A monotonic clock stops during suspend, so a kernel segment can have several pieces.

Confidence (``conf``), best first:

- ``exact``: the reference clock itself (all wall-clock sources of the reference file);
- ``manual``: set by a user with ``align`` (offset or anchors);
- ``paired``: the same event with both clocks, agreeing within 10 ms (the same lines in two
  sources, a wall-clock time printed in a kernel line);
- ``marker``: matched events (same lines, marker rules) with a larger spread;
- ``estimate``: boot time from the bugreport header (dumpstate time minus uptime), about ±1 min;
- ``assumed``: another device's wall clock taken as written (shifted by the time-zone difference
  when both zones are known); nothing confirms it.

Times that are not ``exact`` print with ``~`` in ``timeline`` and ``context(scope='all')``.
"""

from __future__ import annotations

import bisect
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field

from ..model import SEQ_BITS

CONF_RANK = {"exact": 0, "manual": 1, "paired": 2, "marker": 3, "estimate": 4, "assumed": 5}
CONFS = tuple(CONF_RANK)


@dataclass(frozen=True, slots=True)
class Piece:
    start: int
    offset_ns: int
    drift_ppm: float = 0.0

    def map(self, native: int) -> int:
        if self.drift_ppm:
            return native + self.offset_ns + round(self.drift_ppm * 1e-6 * (native - self.start))
        return native + self.offset_ns

    def inverse(self, case: int) -> int:
        """The native time that maps to ``case`` (rounded down)."""
        if self.drift_ppm:
            k = 1 + self.drift_ppm * 1e-6
            return self.start + int((case - self.offset_ns - self.start) // k)
        return case - self.offset_ns


@dataclass(slots=True)
class SegClock:
    """The mapping of one segment of one source."""

    source_id: int
    seg: int
    native: str  # wall | mono
    first_seq: int
    last_seq: int
    pieces: list[Piece]
    conf: str
    method: str
    error_ns: int | None = None
    anchors: int = 0
    via: int | None = None
    _starts: list[int] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self.pieces.sort(key=lambda p: p.start)
        self._starts = [p.start for p in self.pieces]

    @property
    def exact(self) -> bool:
        return self.conf == "exact"

    def piece_at(self, native: int) -> Piece:
        i = bisect.bisect_right(self._starts, native) - 1
        return self.pieces[max(i, 0)]

    def map(self, native: int) -> int:
        return self.piece_at(native).map(native)

    def native_windows(self, lo: int | None, hi: int | None) -> list[tuple[int | None, int | None]]:
        """Native ranges (inclusive, None = open) whose case times can fall in [lo, hi]."""
        out: list[tuple[int | None, int | None]] = []
        for i, p in enumerate(self.pieces):
            p_lo = None if i == 0 else p.start
            p_hi = None if i + 1 == len(self.pieces) else self.pieces[i + 1].start - 1
            n_lo = p.inverse(lo) - 1 if lo is not None else None
            n_hi = p.inverse(hi) + 1 if hi is not None else None
            a = n_lo if p_lo is None else (p_lo if n_lo is None else max(p_lo, n_lo))
            b = n_hi if p_hi is None else (p_hi if n_hi is None else min(p_hi, n_hi))
            if a is not None and b is not None and a > b:
                continue
            out.append((a, b))
        # Adjacent ranges merge (one query instead of several).
        merged: list[tuple[int | None, int | None]] = []
        for a, b in out:
            if merged:
                pa, pb = merged[-1]
                if pb is None or (a is not None and pb + 1 >= a):
                    merged[-1] = (pa, None if pb is None or b is None else max(pb, b))
                    continue
            merged.append((a, b))
        return merged

    @property
    def id_range(self) -> tuple[int, int]:
        base = self.source_id << SEQ_BITS
        return base | self.first_seq, base | self.last_seq


@dataclass
class CaseClock:
    """All segment clocks of a case. ``kind`` is the case clock: 'wall' or 'mono'."""

    kind: str
    reference: int | None
    segs: dict[tuple[int, int], SegClock]

    def get(self, source_id: int, seg: int | None) -> SegClock | None:
        if seg is None:
            return None
        return self.segs.get((source_id, seg))

    def case_time(self, source_id: int, seg: int | None, ts: int | None, ts_src: int | None) -> int | None:
        """Case time of a record (``ts_src`` as stored: None when equal to ``ts``)."""
        sc = self.get(source_id, seg)
        if sc is None:
            return None
        if sc.native == "wall":
            return sc.map(ts) if ts is not None else None
        if ts is not None:
            return None
        return sc.map(ts_src) if ts_src is not None else None

    def of_source(self, source_id: int) -> list[SegClock]:
        return sorted((c for (sid, _), c in self.segs.items() if sid == source_id), key=lambda c: c.seg)

    def placed_sources(self) -> set[int]:
        return {sid for sid, _ in self.segs}


def load(conn: sqlite3.Connection) -> CaseClock:
    """The stored clock model (empty when alignment has not run)."""
    kind = _meta(conn, "clock_kind") or "wall"
    ref = _meta(conn, "clock_reference")
    segs: dict[tuple[int, int], SegClock] = {}
    bounds = {
        (sid, seg): (first, last)
        for sid, seg, first, last in conn.execute("SELECT source_id, seg, first_seq, last_seq FROM segments")
    }
    rows = conn.execute(
        "SELECT source_id, seg, start, offset_ns, drift_ppm, native, conf, method, error_ns, anchors, via "
        "FROM clock ORDER BY source_id, seg, start"
    ).fetchall()
    for sid, seg, start, off, drift, native, conf, method, err, anchors, via in rows:
        key = (sid, seg)
        if key not in bounds:
            continue
        sc = segs.get(key)
        piece = Piece(start, off, drift or 0.0)
        if sc is None:
            first, last = bounds[key]
            segs[key] = SegClock(sid, seg, native, first, last, [piece], conf, method, err, anchors, via)
        else:
            sc.pieces.append(piece)
            sc.anchors += anchors
            if err is not None and (sc.error_ns is None or err > sc.error_ns):
                sc.error_ns = err
    for sc in segs.values():
        sc.__post_init__()
    return CaseClock(kind, int(ref) if ref else None, segs)


def store(
    conn: sqlite3.Connection, clocks: Iterable[SegClock], kind: str, reference: int | None, sig: str
) -> None:
    """Replace the stored model (inside the caller's transaction)."""
    conn.execute("DELETE FROM clock")
    rows = []
    for sc in clocks:
        for p in sc.pieces:
            rows.append(
                (
                    sc.source_id,
                    sc.seg,
                    p.start,
                    p.offset_ns,
                    p.drift_ppm,
                    sc.native,
                    sc.conf,
                    sc.method,
                    sc.error_ns,
                    sc.anchors if p is sc.pieces[0] else 0,
                    sc.via,
                )
            )
    conn.executemany(
        "INSERT OR REPLACE INTO clock(source_id, seg, start, offset_ns, drift_ppm, native, conf, method, "
        "error_ns, anchors, via) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    for key, value in (("clock_kind", kind), ("clock_reference", str(reference or "")), ("clock_sig", sig)):
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row and row[0] != "" else None


def stored_sig(conn: sqlite3.Connection) -> str | None:
    return _meta(conn, "clock_sig")
