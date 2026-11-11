"""Automatic and manual alignment of the sources of a case onto one case clock (M5).

Clocks: all wall-clock sources of one original file share one clock (a bugreport's buffers);
every segment of a source with only a monotonic clock (kernel time since boot) is a clock of its
own. One clock is the reference (the case clock): the one a user chose with ``align``, else the
wall clock with the most records.

Anchors that relate two clocks, best first (see :mod:`core.clock` for the confidence names):

1. **Manual**: an offset (``align mode='offset'``) or pairs of records that happened at the same
   moment (``mode='anchor'``; two or more pairs also fit a drift).
2. **Paired timestamps**: the same line in two sources (``logcat -b kernel`` and ``dmesg``, two
   captures of one log), and wall-clock times printed in kernel lines (``PM: suspend exit
   2026-01-01 08:00:01.123456789 UTC``, converted with the device's time zone). The offsets of
   all pairs must agree (within 500 ms; a monotonic clock that stops in suspend gives one piece
   per stretch between suspends); ``paired`` when they agree within 10 ms, else ``marker``.
3. **Marker rules**: YAML files in the rule directories (``marker: <name>``) that pair events of
   two sources by a key, e.g. a request id logged by the head unit and by an ECU (see
   ``docs/connectors.md``).
4. **Boot time estimate** of a bugreport's kernel log: dumpstate time minus uptime (±1 min).

A clock without anchors keeps its times as written when it is a wall clock (``assumed``, shifted
by the difference of the time zones when both are known); a monotonic clock without anchors is
not placed on the case clock (``timeline`` names it).

The same lines on two different devices can agree by chance (two units running the same software
print the same boot lines a fixed time after boot): the pairs must make up at least half of the
candidates, and ``align`` prints how many lines a mapping rests on.
"""

from __future__ import annotations

import fnmatch
import hashlib
import heapq
import re
import sqlite3
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import ConfigError, read_yaml_mapping
from ..model import SEQ_BITS
from ..store import query
from . import clock as ck
from .clock import CONF_RANK, Piece, SegClock
from .templates import mask
from .timeparse import NS, wall_ns

# An unknown or malformed zone name (from a log), or no tz database on the machine.
_ZONE_ERRORS = (ZoneInfoNotFoundError, ValueError, OSError)

MS = 1_000_000
PAIR_TOL_NS = 500 * MS  # offsets of one piece agree within this
PAIRED_NS = 10 * MS  # ... and within this for 'paired'
MIN_PAIRS = 3
MAX_UNIQUE = 200_000  # unique messages per clock considered for same-line pairs
MAX_PAIRS = 20_000
BOOT_ESTIMATE_ERR = 60 * NS
DRIFT_MIN_SPAN = 60 * NS
DRIFT_MAX_PPM = 500.0

UTC_IN_MSG = re.compile(r"(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d):(\d\d)(?:\.(\d{1,9}))? UTC")


class AlignError(ValueError):
    """Invalid manual alignment (unknown source, record without time, conflicting reference)."""


# -- marker rules -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class MarkerSide:
    match: re.Pattern[str]
    tags: tuple[str, ...] | None = None
    sources: tuple[str, ...] | None = None  # globs on source name or format


@dataclass(frozen=True)
class MarkerRule:
    name: str
    a: MarkerSide
    b: MarkerSide
    delay_ns: int = 0  # b is logged this long after a
    tolerance_ns: int = PAIR_TOL_NS
    description: str = ""
    origin: str = ""
    version: str = "1"


_MARKER_KEYS = {"marker", "description", "a", "b", "delay", "tolerance", "version"}
_SIDE_KEYS = {"match", "tags", "sources"}
_DUR = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*(ns|us|ms|s|m|h)\s*$")
_DUR_NS = {"ns": 1, "us": 1000, "ms": MS, "s": NS, "m": 60 * NS, "h": 3600 * NS}


def parse_duration(text: Any, what: str) -> int:
    m = _DUR.match(str(text))
    if m is None:
        raise ValueError(f"{what} must be a duration like '250ms', '-1.5s' or '2m', got {text!r}")
    return round(float(m[1]) * _DUR_NS[m[2]])


def _side(spec: Any, path: Path, where: str) -> MarkerSide:
    if not isinstance(spec, dict) or set(spec) - _SIDE_KEYS:
        raise ConfigError(f"marker rule {path}: '{where}' must be a mapping with keys {sorted(_SIDE_KEYS)}")
    match = spec.get("match")
    if not isinstance(match, str) or not match:
        raise ConfigError(f"marker rule {path}: '{where}.match' (a regex) is required")
    try:
        rx = re.compile(match)
    except re.error as e:
        raise ConfigError(f"marker rule {path}: '{where}.match' is not a valid regex: {e}") from e
    out: dict[str, tuple[str, ...] | None] = {}
    for key in ("tags", "sources"):
        v = spec.get(key)
        if isinstance(v, str):
            v = [v]
        if v is not None and (not isinstance(v, list) or not all(isinstance(x, str) and x for x in v)):
            raise ConfigError(f"marker rule {path}: '{where}.{key}' must be a list of globs")
        out[key] = tuple(v) if v else None
    return MarkerSide(rx, out["tags"], out["sources"])


def parse_marker(spec: dict[str, Any], path: Path) -> MarkerRule:
    unknown = set(spec) - _MARKER_KEYS
    if unknown:
        raise ConfigError(f"marker rule {path}: unknown keys: {', '.join(sorted(unknown))}")
    name = spec.get("marker")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", name):
        raise ConfigError(f"marker rule {path}: 'marker' must be a lowercase name")
    a, b = _side(spec.get("a"), path, "a"), _side(spec.get("b"), path, "b")
    ga, gb = set(a.match.groupindex), set(b.match.groupindex)
    if not ga or ga != gb:
        raise ConfigError(
            f"marker rule {path}: 'a.match' and 'b.match' need the same named groups (the key that "
            f"pairs two events); got {sorted(ga)} and {sorted(gb)}"
        )
    try:
        delay = parse_duration(spec.get("delay", "0s"), "'delay'")
        tol = parse_duration(spec.get("tolerance", "500ms"), "'tolerance'")
    except ValueError as e:
        raise ConfigError(f"marker rule {path}: {e}") from None
    if tol <= 0:
        raise ConfigError(f"marker rule {path}: 'tolerance' must be positive")
    return MarkerRule(
        name, a, b, delay, tol, str(spec.get("description", "")), str(path), str(spec.get("version", "1"))
    )


def load_markers(rule_dirs: Iterable[Path] = ()) -> list[MarkerRule]:
    """Marker rules from the rule directories (files with ``marker:`` or ``markers: [...]``);
    a later rule with the same name replaces an earlier one."""
    out: dict[str, MarkerRule] = {}
    for d in rule_dirs:
        if not d.is_dir():
            continue  # load_rules reports missing directories
        for path in sorted([*d.rglob("*.yaml"), *d.rglob("*.yml")]):
            data = read_yaml_mapping(path, "rule file")
            if "markers" in data:
                if set(data) != {"markers"} or not isinstance(data["markers"], list):
                    raise ConfigError(f"marker rule {path}: 'markers' must be the only key, with a list")
                for spec in data["markers"]:
                    if not isinstance(spec, dict):
                        raise ConfigError(f"marker rule {path}: each entry of 'markers' must be a mapping")
                    r = parse_marker(spec, path)
                    out[r.name] = r
            elif "marker" in data:
                r = parse_marker(data, path)
                out[r.name] = r
    return list(out.values())


def markers_hash(rules: Sequence[MarkerRule]) -> str:
    h = hashlib.sha256()
    for r in sorted(rules, key=lambda r: r.name):
        h.update(f"{r.name}|{r.version}|{r.a.match.pattern}|{r.b.match.pattern}|{r.delay_ns}|".encode())
        h.update(f"{r.a.tags}|{r.b.tags}|{r.a.sources}|{r.b.sources}|{r.tolerance_ns}\n".encode())
    return h.hexdigest()[:16]


# -- clocks and anchors -------------------------------------------------------------------------------


@dataclass
class Unit:
    sid: int
    seg: int
    native: str  # wall | mono
    first_seq: int
    last_seq: int
    count: int
    lo: int | None
    hi: int | None

    @property
    def id_range(self) -> tuple[int, int]:
        base = self.sid << SEQ_BITS
        return base | self.first_seq, base | self.last_seq


@dataclass
class Node:
    """One clock: the wall-clock segments of one file, or one monotonic segment."""

    key: str
    native: str
    sha256: str
    units: list[Unit] = field(default_factory=list)
    timezone: str | None = None
    boot_estimate: int | None = None  # wall time of native 0 (bugreport: dumpstate - uptime)
    uptime_ns: int | None = None
    names: list[str] = field(default_factory=list)

    @property
    def records(self) -> int:
        return sum(u.count for u in self.units)

    @property
    def sources(self) -> list[int]:
        return sorted({u.sid for u in self.units})

    def where(self) -> tuple[str, list[Any]]:
        parts: list[str] = []
        params: list[Any] = []
        for u in self.units:
            parts.append("r.id BETWEEN ? AND ?")
            params.extend(u.id_range)
        cond = "(" + " OR ".join(parts) + ")"
        cond += (
            " AND r.ts IS NOT NULL" if self.native == "wall" else " AND r.ts IS NULL AND r.ts_src IS NOT NULL"
        )
        return cond, params


@dataclass
class Edge:
    """``pieces`` map the native time of ``a`` to the native time of ``b``."""

    a: str
    b: str
    pieces: list[Piece]
    conf: str
    error_ns: int | None
    anchors: int
    method: str
    via: int | None  # a source of b
    show_via: bool = True


@dataclass
class Manual:
    id: int
    kind: str
    source_id: int
    seg: int | None
    offset_ns: int | None
    at_id: int | None
    ref_id: int | None


def manual_entries(conn: sqlite3.Connection) -> list[Manual]:
    return [
        Manual(*row)
        for row in conn.execute(
            "SELECT id, kind, source_id, seg, offset_ns, at_id, ref_id FROM clock_manual ORDER BY id"
        )
    ]


def signature(sources: Sequence[query.SourceRow], manual: Sequence[Manual], marker_hash: str) -> str:
    h = hashlib.sha256()
    for s in sources:
        h.update(f"{s.id}|{s.status}|{s.records}|{s.sha256}|{s.connector_version}\n".encode())
    for m in manual:
        h.update(f"m{m.id}|{m.kind}|{m.source_id}|{m.seg}|{m.offset_ns}|{m.at_id}|{m.ref_id}\n".encode())
    h.update(marker_hash.encode())
    return h.hexdigest()[:24]


def build_nodes(conn: sqlite3.Connection, sources: Sequence[query.SourceRow]) -> dict[str, Node]:
    done = {s.id: s for s in sources if s.status == "done" and s.records}
    nodes: dict[str, Node] = {}
    rows = conn.execute(
        "SELECT source_id, seg, first_seq, last_seq, count, ts_first, ts_last, ts_src_first, ts_src_last "
        "FROM segments ORDER BY source_id, seg"
    ).fetchall()
    last_seg: dict[int, int] = {}
    for sid, seg, *_ in rows:
        last_seg[sid] = max(seg, last_seg.get(sid, 0))
    for sid, seg, first, last, count, t0, t1, m0, m1 in rows:
        s = done.get(sid)
        if s is None or not count:
            continue
        if t0 is not None:
            native, lo, hi = "wall", t0, t1
            key = f"wall:{s.sha256}"
        elif m0 is not None:
            native, lo, hi = "mono", m0, m1
            key = f"mono:{sid}:{seg}"
        else:
            continue
        node = nodes.get(key)
        if node is None:
            node = nodes[key] = Node(key, native, s.sha256)
        node.units.append(Unit(sid, seg, native, first, last, count, lo, hi))
        if s.name not in node.names:
            node.names.append(s.name)
        tz = s.meta.get("timezone")
        if isinstance(tz, str) and tz:
            node.timezone = node.timezone or tz
        if native == "mono" and seg == last_seg.get(sid) and not _previous_boot(s):
            est = s.meta.get("boot_time_estimate")
            up = s.meta.get("uptime_s")
            if isinstance(est, int):
                node.boot_estimate = est
                node.uptime_ns = int(up) * NS if isinstance(up, int | float) else None
    # Time zones are per file: a kernel log of a bugreport has the zone of its buffers.
    zones = {n.sha256: n.timezone for n in nodes.values() if n.timezone}
    for n in nodes.values():
        n.timezone = n.timezone or zones.get(n.sha256)
    return nodes


def _previous_boot(s: query.SourceRow) -> bool:
    name = (s.name + " " + (s.member or "")).lower()
    return "last_kmsg" in name or "last kmsg" in name or "pstore" in name or "console-ramoops" in name


# -- fitting --------------------------------------------------------------------------------------------


@dataclass
class Fit:
    pieces: list[Piece]
    error_ns: int
    inliers: int
    candidates: int


def fit_pairs(
    pairs: Sequence[tuple[int, int]],
    tol: int = PAIR_TOL_NS,
    min_pairs: int = MIN_PAIRS,
    min_share: float = 0.5,
    singles: bool = False,
) -> Fit | None:
    """Piecewise offsets from (native a, native b) pairs that happened at the same moment.

    Pairs are taken in order of ``a``; a piece ends where the offset changes and the next pair
    confirms the new offset (a monotonic clock that stopped during suspend). Pairs that agree
    with neither are outliers. ``singles``: a piece may rest on one pair (trusted pairs)."""
    if not pairs:
        return None
    pts = sorted(pairs)[:MAX_PAIRS]
    runs: list[list[tuple[int, int]]] = []
    cur: list[tuple[int, int]] = []
    offs: list[int] = []  # sorted offsets of cur
    n = len(pts)
    for i, (x, y) in enumerate(pts):
        o = y - x
        if not cur or abs(o - offs[len(offs) // 2]) <= tol:
            cur.append((x, o))
            _insort(offs, o)
            continue
        nxt = pts[i + 1][1] - pts[i + 1][0] if i + 1 < n else None
        if (nxt is not None and abs(nxt - o) <= tol) or (singles and nxt is None):
            runs.append(cur)
            cur, offs = [(x, o)], [o]
    if cur:
        runs.append(cur)
    need = 1 if singles else min_pairs
    kept = [r for r in runs if len(r) >= need]
    inliers = sum(len(r) for r in kept)
    if not kept or inliers < need or inliers < min_share * n:
        return None
    pieces: list[Piece] = []
    err = 0
    for r in kept:
        start = r[0][0]
        med = int(statistics.median(o for _, o in r))
        piece = Piece(start, med)
        resid = max(abs(o - med) for _, o in r)
        if len(r) >= 5 and r[-1][0] - r[0][0] >= DRIFT_MIN_SPAN:
            drifted = _drift_fit(r)
            if drifted is not None:
                d_resid = max(abs(o - (drifted.map(x) - x)) for x, o in r)
                if d_resid < resid * 0.8:
                    piece, resid = drifted, d_resid
        pieces.append(piece)
        err = max(err, resid)
    # Agreement within the pairs is no better than the times' resolution: logcat prints
    # milliseconds, so offsets that agree exactly still leave up to 1 ms (found by the M6 eval,
    # where 12 paired lines gave '±0 ns').
    res = max(_resolution([x for r in kept for x, _ in r]), _resolution([x + o for r in kept for x, o in r]))
    return Fit(pieces, max(err, res), inliers, n)


def _resolution(values: list[int]) -> int:
    """1 ms when every value is a whole millisecond, 1 µs for whole microseconds, else 1 ns."""
    for step in (1_000_000, 1000):
        if all(v % step == 0 for v in values):
            return step
    return 1


def _insort(a: list[int], v: int) -> None:
    import bisect

    bisect.insort(a, v)


def _drift_fit(r: list[tuple[int, int]]) -> Piece | None:
    x0 = r[0][0]
    xs = [(x - x0) / NS for x, _ in r]
    ys = [float(o) for _, o in r]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx  # ns per s
    ppm = slope / 1000.0  # ns/s -> ppm (1 ppm = 1000 ns per s)
    if not 0.5 <= abs(ppm) <= DRIFT_MAX_PPM:
        return None
    intercept = my - slope * mx
    return Piece(x0, round(intercept), ppm)


def invert(pieces: Sequence[Piece]) -> list[Piece]:
    """Pieces mapping b back to a."""
    out = []
    for p in pieces:
        start_b = p.map(p.start)
        k = 1 + p.drift_ppm * 1e-6
        out.append(Piece(start_b, -p.offset_ns, (1 / k - 1) * 1e6 if p.drift_ppm else 0.0))
    return out


def compose(first: Sequence[Piece], second: Sequence[Piece]) -> list[Piece]:
    """Pieces of ``second ∘ first`` (a -> b -> c), in a's native time."""
    if not second:
        return list(first)
    f = SegClock(0, 0, "wall", 0, 0, list(first), "exact", "")
    s = SegClock(0, 0, "wall", 0, 0, list(second), "exact", "")
    bounds = {p.start for p in f.pieces}
    for q in s.pieces[1:]:
        for i, p in enumerate(f.pieces):
            x = p.inverse(q.start)
            lo_ok = i == 0 or x >= p.start
            hi_ok = i + 1 == len(f.pieces) or x < f.pieces[i + 1].start
            if lo_ok and hi_ok:
                bounds.add(x)
    out = []
    for b in sorted(bounds):
        p = f.piece_at(b)
        y = p.map(b)
        q = s.piece_at(y)
        out.append(Piece(b, q.map(y) - b, p.drift_ppm + q.drift_ppm))
    # Drop pieces that change nothing.
    slim: list[Piece] = []
    for p in out:
        if slim and slim[-1].offset_ns == p.offset_ns and slim[-1].drift_ppm == p.drift_ppm == 0:
            continue
        slim.append(p)
    return slim


# -- anchor finders ---------------------------------------------------------------------------------------


def _native(native: str, ts: int | None, ts_src: int | None) -> int | None:
    return ts if native == "wall" else (ts_src if ts is None else None)


def _unique_templates(conn: sqlite3.Connection, node: Node) -> dict[int, int]:
    cond, params = node.where()
    rows = conn.execute(
        f"SELECT r.template_id, min(r.id) FROM records r WHERE {cond} AND r.template_id IS NOT NULL "
        "GROUP BY r.template_id HAVING count(*) = 1 LIMIT ?",
        [*params, MAX_UNIQUE],
    )
    return {int(t): int(i) for t, i in rows}


def _norm(tag: str | None, msg: str) -> str:
    """A line's text without a leading 'tag: ' (dmesg keeps 'binder: ...' in the message, logcat's
    kernel buffer splits it into tag and message)."""
    if tag and msg.startswith(tag + ": "):
        return msg[len(tag) + 2 :]
    return msg


def _aliases(conn: sqlite3.Connection, uniq: dict[int, int]) -> dict[int, int]:
    """For unique lines of a monotonic clock: the template of the line without its tag prefix."""
    recs = query.records_by_id(conn, list(uniq.values()))
    want: dict[str, int] = {}
    for r in recs.values():
        n = _norm(r.tag, r.msg)
        if n != r.msg:
            want.setdefault(mask(n), r.id)
    out: dict[int, int] = {}
    texts = list(want)
    for k in range(0, len(texts), 500):
        chunk = texts[k : k + 500]
        for tid, text in conn.execute(
            f"SELECT id, text FROM templates WHERE text IN ({','.join('?' * len(chunk))})", chunk
        ):
            out[int(tid)] = want[text]
    return out


def same_line_edges(conn: sqlite3.Connection, nodes: dict[str, Node]) -> list[Edge]:
    """Edges from lines that occur once in each of two clocks with the same text and tag."""
    keys = [k for k in nodes if nodes[k].records]
    if len(keys) < 2:
        return []
    uniq = {k: _unique_templates(conn, nodes[k]) for k in keys}
    for k in keys:
        if nodes[k].native == "mono":
            for t, rid in _aliases(conn, uniq[k]).items():
                uniq[k].setdefault(t, rid)
    edges: list[Edge] = []
    for i, ka in enumerate(keys):
        for kb in keys[i + 1 :]:
            na, nb = nodes[ka], nodes[kb]
            if na.native == "mono" and nb.native == "mono":
                continue  # two boots print the same lines at the same uptime
            common = uniq[ka].keys() & uniq[kb].keys()
            if len(common) < MIN_PAIRS:
                continue
            id_pairs = {(uniq[ka][t], uniq[kb][t]) for t in common}
            recs = query.records_by_id(conn, [x for p in id_pairs for x in p])
            pairs = []
            via: dict[int, int] = {}
            for ia, ib in id_pairs:
                ra, rb = recs.get(ia), recs.get(ib)
                if ra is None or rb is None or ra.msg.startswith("[art:"):
                    continue
                if _norm(ra.tag, ra.msg) != _norm(rb.tag, rb.msg):
                    continue
                if ra.tag and rb.tag and ra.tag != rb.tag:
                    continue
                xa, xb = _native(na.native, ra.ts, ra.ts_src), _native(nb.native, rb.ts, rb.ts_src)
                if xa is None or xb is None:
                    continue
                pairs.append((xa, xb))
                via[rb.source_id] = via.get(rb.source_id, 0) + 1
            fit = fit_pairs(pairs)
            if fit is None:
                continue
            conf = "paired" if fit.error_ns <= PAIRED_NS else "marker"
            vsrc = max(via, key=lambda s: via[s]) if via else None
            edges.append(
                Edge(
                    ka,
                    kb,
                    fit.pieces,
                    conf,
                    fit.error_ns,
                    fit.inliers,
                    f"{fit.inliers} same lines of {fit.candidates}",
                    vsrc,
                )
            )
    return edges


def utc_in_message_edges(conn: sqlite3.Connection, nodes: dict[str, Node]) -> list[Edge]:
    """Monotonic segments whose lines print the wall-clock time in UTC (kernel suspend/resume,
    RTC lines), paired with the wall clock of the same file (converted with its time zone)."""
    edges = []
    walls = {n.sha256: n for n in nodes.values() if n.native == "wall"}
    for n in nodes.values():
        if n.native != "mono":
            continue
        target = walls.get(n.sha256)
        if target is None or not target.timezone:
            continue
        try:
            zone = ZoneInfo(target.timezone)
        except _ZONE_ERRORS:
            continue
        cond, params = n.where()
        rows = conn.execute(
            f"SELECT r.ts_src, rmsg(r.id) FROM records r WHERE {cond} AND {query.FTS_MATCH} LIMIT 20000",
            [*params, '"UTC"'],
        )
        pairs = []
        for ts_src, msg in rows:
            m = UTC_IN_MSG.search(msg)
            if m is None:
                continue
            try:
                utc = wall_ns(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]), int(m[6]), m[7] or "")
            except ValueError:
                continue
            secs = utc // NS
            off = datetime.fromtimestamp(secs, UTC).astimezone(zone).utcoffset() or timedelta(0)
            pairs.append((ts_src, utc + int(off.total_seconds()) * NS))
        fit = fit_pairs(pairs, tol=50 * MS, singles=True)
        if fit is None:
            continue
        edges.append(
            Edge(
                n.key,
                target.key,
                fit.pieces,
                "paired" if fit.error_ns <= PAIRED_NS else "marker",
                fit.error_ns,
                fit.inliers,
                f"{fit.inliers} kernel lines with the UTC time ({target.timezone})",
                target.sources[0],
            )
        )
    return edges


def boot_estimate_edges(nodes: dict[str, Node]) -> list[Edge]:
    walls = {n.sha256: n for n in nodes.values() if n.native == "wall"}
    out = []
    for n in nodes.values():
        if n.native != "mono" or n.boot_estimate is None:
            continue
        target = walls.get(n.sha256)
        if target is None:
            continue
        hi = max((u.hi or 0) for u in n.units)
        if n.uptime_ns is not None and hi > n.uptime_ns + 10 * 60 * NS:
            continue  # this segment runs past the uptime: not the current boot
        out.append(
            Edge(
                n.key,
                target.key,
                [Piece(0, n.boot_estimate)],
                "estimate",
                BOOT_ESTIMATE_ERR,
                1,
                "boot time from the bugreport header (dumpstate time - uptime)",
                target.sources[0],
            )
        )
    return out


def _side_sources(side: MarkerSide, sources: Sequence[query.SourceRow]) -> list[int]:
    if side.sources is None:
        return [s.id for s in sources]
    return [
        s.id
        for s in sources
        if any(fnmatch.fnmatch(s.name, g) or fnmatch.fnmatch(s.format, g) for g in side.sources)
    ]


def _side_events(
    conn: sqlite3.Connection, side: MarkerSide, sids: list[int], unit_of: dict[tuple[int, int], Node]
) -> dict[tuple[str, ...], tuple[str, int, int] | None]:
    """key -> (node key, native time, source id); None for a key seen twice (not usable)."""
    if not sids:
        return {}
    rf = query.RecordFilter(sources=sids)
    if side.tags:
        rf.tag_ids, _ = query.tag_ids(conn, list(side.tags))
    where, params = rf.where()
    pre_w, pre_p = query.regex_prefilter(side.match.pattern)
    where, params = where + pre_w, params + pre_p
    sql = (
        f"SELECT r.id, r.ts, r.ts_src, r.seg, rmsg(r.id), t.name FROM {query.REC_FROM} "
        f"WHERE {' AND '.join(where) or '1'} ORDER BY r.id LIMIT 200000"
    )
    out: dict[tuple[str, ...], tuple[str, int, int] | None] = {}
    names = sorted(side.match.groupindex)
    for rid, ts, ts_src, seg, msg, _tag in conn.execute(sql, params):
        m = side.match.search(msg)
        if m is None:
            continue
        sid = rid >> SEQ_BITS
        node = unit_of.get((sid, seg))
        if node is None:
            continue
        x = _native(node.native, ts, ts_src)
        if x is None:
            continue
        key = tuple(str(m.group(g)) for g in names)
        out[key] = None if key in out else (node.key, x, sid)
    return out


def marker_edges(
    conn: sqlite3.Connection,
    nodes: dict[str, Node],
    sources: Sequence[query.SourceRow],
    rules: Sequence[MarkerRule],
) -> list[Edge]:
    unit_of = {(u.sid, u.seg): n for n in nodes.values() for u in n.units}
    edges = []
    for rule in rules:
        ea = _side_events(conn, rule.a, _side_sources(rule.a, sources), unit_of)
        eb = _side_events(conn, rule.b, _side_sources(rule.b, sources), unit_of)
        pairs: dict[tuple[str, str], list[tuple[int, int]]] = {}
        via: dict[tuple[str, str], int] = {}
        for key, va in ea.items():
            vb = eb.get(key)
            if va is None or vb is None or va[0] == vb[0]:
                continue
            pairs.setdefault((va[0], vb[0]), []).append((va[1], vb[1] - rule.delay_ns))
            via[(va[0], vb[0])] = vb[2]
        for (ka, kb), pts in pairs.items():
            fit = fit_pairs(pts, tol=rule.tolerance_ns, min_pairs=2 if len(pts) < 6 else MIN_PAIRS)
            if fit is None:
                continue
            edges.append(
                Edge(
                    ka,
                    kb,
                    fit.pieces,
                    "paired" if fit.error_ns <= PAIRED_NS and fit.inliers >= MIN_PAIRS else "marker",
                    fit.error_ns,
                    fit.inliers,
                    f"marker rule '{rule.name}': {fit.inliers} events of {fit.candidates}",
                    via[(ka, kb)],
                )
            )
    return edges


def manual_edges(
    conn: sqlite3.Connection, nodes: dict[str, Node], manual: Sequence[Manual], ref: str
) -> tuple[list[Edge], list[str]]:
    """Edges from manual offsets (to the reference) and anchors; and notes on entries that no
    longer apply (their source was removed or reindexed)."""
    unit_of = {(u.sid, u.seg): n for n in nodes.values() for u in n.units}
    by_source: dict[int, list[Node]] = {}
    for n in nodes.values():
        for u in n.units:
            by_source.setdefault(u.sid, [])
            if n not in by_source[u.sid]:
                by_source[u.sid].append(n)
    edges: list[Edge] = []
    notes: list[str] = []
    anchor_pairs: dict[tuple[str, str], list[tuple[int, int]]] = {}
    anchor_via: dict[tuple[str, str], int] = {}
    for m in manual:
        if m.kind == "offset":
            targets = [
                n
                for n in by_source.get(m.source_id, [])
                if m.seg is None or any(u.sid == m.source_id and u.seg == m.seg for u in n.units)
            ]
            if not targets:
                notes.append(f"manual offset for source {m.source_id} no longer applies (source not found)")
            for n in targets:
                if n.key == ref:
                    continue
                edges.append(
                    Edge(
                        n.key,
                        ref,
                        [Piece(0, int(m.offset_ns or 0))],
                        "manual",
                        None,
                        1,
                        "offset set with align",
                        None,
                        show_via=False,
                    )
                )
        elif m.kind == "anchor" and m.at_id is not None and m.ref_id is not None:
            recs = query.records_by_id(conn, [m.at_id, m.ref_id])
            ra, rb = recs.get(m.at_id), recs.get(m.ref_id)
            if ra is None or rb is None:
                notes.append("a manual anchor no longer applies (record not found)")
                continue
            na, nb = unit_of.get((ra.source_id, ra.seg or 0)), unit_of.get((rb.source_id, rb.seg or 0))
            if na is None or nb is None or na is nb:
                continue
            xa, xb = _native(na.native, ra.ts, ra.ts_src), _native(nb.native, rb.ts, rb.ts_src)
            if xa is None or xb is None:
                continue
            anchor_pairs.setdefault((na.key, nb.key), []).append((xa, xb + int(m.offset_ns or 0)))
            anchor_via[(na.key, nb.key)] = rb.source_id
    for (ka, kb), pts in anchor_pairs.items():
        fit = fit_pairs(pts, tol=365 * 86400 * NS, singles=True, min_share=0.0)
        if fit is None:
            continue
        pieces = fit.pieces
        if len(pts) >= 2 and len(pieces) == 1:
            pts_s = sorted(pts)
            (x0, y0), (x1, y1) = pts_s[0], pts_s[-1]
            if x1 > x0:
                ppm = ((y1 - x1) - (y0 - x0)) / (x1 - x0) * 1e6
                pieces = [Piece(x0, y0 - x0, ppm if abs(ppm) <= DRIFT_MAX_PPM else 0.0)]
        edges.append(
            Edge(
                ka,
                kb,
                pieces,
                "manual",
                fit.error_ns if len(pts) >= 3 else None,
                len(pts),
                f"{len(pts)} anchor(s) set with align",
                anchor_via[(ka, kb)],
            )
        )
    return edges, notes


# -- solving -----------------------------------------------------------------------------------------------


@dataclass
class Result:
    kind: str
    reference: int | None
    ref_key: str | None
    clocks: list[SegClock]
    unplaced: list[tuple[int, int]]
    notes: list[str]


def choose_reference(nodes: dict[str, Node], manual: Sequence[Manual]) -> Node | None:
    for m in reversed(manual):
        if m.kind == "reference":
            cands = [n for n in nodes.values() if m.source_id in n.sources]
            if cands:
                return max(cands, key=lambda n: (n.native == "wall", n.records))
    walls = [n for n in nodes.values() if n.native == "wall"]
    pool = walls or list(nodes.values())
    if not pool:
        return None
    return max(pool, key=lambda n: (n.records, -min(n.sources)))


def _zone_offset(zone_from: str | None, zone_to: str | None, at: int | None) -> int | None:
    if not zone_from or not zone_to or zone_from == zone_to or at is None:
        return 0 if zone_from and zone_from == zone_to else None
    try:
        t = datetime.fromtimestamp(at // NS, UTC)
        a = t.astimezone(ZoneInfo(zone_from)).utcoffset() or timedelta(0)
        b = t.astimezone(ZoneInfo(zone_to)).utcoffset() or timedelta(0)
    except (*_ZONE_ERRORS, OverflowError):
        return None
    return int((b - a).total_seconds()) * NS


def solve(
    conn: sqlite3.Connection,
    sources: Sequence[query.SourceRow],
    manual: Sequence[Manual],
    markers: Sequence[MarkerRule] = (),
) -> Result:
    nodes = build_nodes(conn, sources)
    ref = choose_reference(nodes, manual)
    if ref is None:
        return Result("wall", None, None, [], [], [])
    notes: list[str] = []
    m_edges, m_notes = manual_edges(conn, nodes, manual, ref.key)
    notes += m_notes
    edges = m_edges
    if len(nodes) > 1:
        edges += same_line_edges(conn, nodes)
        edges += utc_in_message_edges(conn, nodes)
        edges += boot_estimate_edges(nodes)
        if markers:
            edges += marker_edges(conn, nodes, sources, markers)
    # Dijkstra from the reference over edges in both directions; cost: worst confidence on the
    # path, then summed error (unknown error counts as large).
    adj: dict[str, list[tuple[str, list[Piece], Edge]]] = {}
    for edge in edges:
        adj.setdefault(edge.b, []).append((edge.a, edge.pieces, edge))  # pieces map a -> b
        adj.setdefault(edge.a, []).append((edge.b, invert(edge.pieces), edge))
    best: dict[str, tuple[int, int]] = {ref.key: (0, 0)}
    mapping: dict[str, tuple[list[Piece], str, int | None, Edge | None]] = {
        ref.key: ([Piece(0, 0)], "exact", 0, None)
    }
    heap: list[tuple[int, int, str]] = [(0, 0, ref.key)]
    while heap:
        rank, cost, k = heapq.heappop(heap)
        if best.get(k, (99, 0)) < (rank, cost):
            continue
        to_ref, _, prev_err, _ = mapping[k]
        for other, pieces, edge in adj.get(k, []):
            if other == ref.key:
                continue
            r2 = max(rank, CONF_RANK[edge.conf])
            c2 = cost + (edge.error_ns if edge.error_ns is not None else 3600 * NS)
            if (r2, c2) < best.get(other, (99, 0)):
                best[other] = (r2, c2)
                total = None if edge.error_ns is None or prev_err is None else prev_err + edge.error_ns
                mapping[other] = (compose(pieces, to_ref), ck.CONFS[r2], total, edge)
                heapq.heappush(heap, (r2, c2, other))
    clocks: list[SegClock] = []
    unplaced: list[tuple[int, int]] = []
    for n in nodes.values():
        found = mapping.get(n.key)
        via: int | None = None
        if found is None:
            if n.native != "wall" or ref.native != "wall":
                unplaced.extend((u.sid, u.seg) for u in n.units)
                continue
            start = min((u.lo for u in n.units if u.lo is not None), default=None)
            zoff = _zone_offset(n.timezone, ref.timezone, start)
            method = "wall clock as written, no anchor found"
            if zoff:
                method += f"; shifted by the time-zone difference {n.timezone} -> {ref.timezone}"
            found = ([Piece(0, zoff or 0)], "assumed", None, None)
        pieces, conf, err, used = found
        if used is None:
            if conf != "assumed":
                method = "reference" if n is ref else "same clock"
        else:
            method = used.method
            if used.show_via:
                via = (
                    (used.via or _first_source(nodes, used.b))
                    if used.a == n.key
                    else _first_source(nodes, used.a)
                )
        for u in n.units:
            clocks.append(
                SegClock(
                    u.sid,
                    u.seg,
                    n.native,
                    u.first_seq,
                    u.last_seq,
                    list(pieces),
                    conf,
                    method,
                    err,
                    used.anchors if used is not None else 0,
                    via,
                )
            )
    return Result(ref.native, min(ref.sources), ref.key, clocks, unplaced, notes)


def _first_source(nodes: dict[str, Node], key: str) -> int | None:
    n = nodes.get(key)
    return n.sources[0] if n and n.sources else None


def refresh(
    conn: sqlite3.Connection,
    sources: Sequence[query.SourceRow],
    markers: Sequence[MarkerRule] = (),
    force: bool = False,
) -> Result | None:
    """Recompute and store the clock model when the sources, manual entries or marker rules
    changed since it was stored (or with ``force``). Returns the result, or None if unchanged."""
    manual = manual_entries(conn)
    sig = signature(sources, manual, markers_hash(markers))
    if not force and ck.stored_sig(conn) == sig:
        return None
    res = solve(conn, sources, manual, markers)
    with conn:
        ck.store(conn, res.clocks, res.kind, res.reference, sig)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('clock_notes', ?)", ("\n".join(res.notes),)
        )
    return res


def add_manual(
    conn: sqlite3.Connection,
    kind: str,
    source_id: int,
    seg: int | None = None,
    offset_ns: int | None = None,
    at_id: int | None = None,
    ref_id: int | None = None,
) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with conn:
        if kind == "reference":
            conn.execute("DELETE FROM clock_manual WHERE kind = 'reference'")
        if kind == "anchor":
            conn.execute(
                "DELETE FROM clock_manual WHERE kind = 'anchor' AND at_id = ? AND ref_id = ?", (at_id, ref_id)
            )
        if kind == "offset":
            conn.execute(
                "DELETE FROM clock_manual WHERE kind = 'offset' AND source_id = ? "
                "AND (seg IS ? OR ? IS NULL)",
                (source_id, seg, seg),
            )
        conn.execute(
            "INSERT INTO clock_manual(kind, source_id, seg, offset_ns, at_id, ref_id, added_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (kind, source_id, seg, offset_ns, at_id, ref_id, now),
        )


def reset_manual(conn: sqlite3.Connection, source_id: int | None = None) -> int:
    with conn:
        if source_id is None:
            cur = conn.execute("DELETE FROM clock_manual")
        else:
            cur = conn.execute(
                "DELETE FROM clock_manual WHERE source_id = ? OR at_id >> 40 = ? OR ref_id >> 40 = ?",
                (source_id, source_id, source_id),
            )
    return cur.rowcount
