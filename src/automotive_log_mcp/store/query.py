"""Read side of a case DB: sources, the record filter as SQL, FTS and regex search, collapse
groups, context neighbours, artifacts and citations, relink.

Everything here works on resolved values (ids, ns); parsing tool arguments, caps and formatting
are in ``tools.py`` and ``core/format.py``.
"""

from __future__ import annotations

import fnmatch
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.levels import LEVEL_RANK
from ..core.timeparse import NS
from ..core.words import query_alternative
from ..model import SEQ_BITS, SEQ_MASK, citation


@dataclass
class SourceRow:
    id: int
    name: str
    format: str
    path: str
    member: str | None
    stream: str | None
    status: str
    error: str | None
    records: int
    lines: int
    unparsed: int
    ts_min: int | None
    ts_max: int | None
    ts_src_min: int | None
    ts_src_max: int | None
    fingerprint: str | None
    meta: dict[str, Any]
    connector_version: str
    redaction_hash: str
    sha256: str
    original_missing: bool = False
    stale: bool = False
    segments: int = 0

    @property
    def ts_format(self) -> str | None:
        fmt = self.meta.get("ts_format")
        return fmt if isinstance(fmt, str) else None


_SOURCE_COLS = (
    "id, name, format, path, member, stream, status, error, records, lines, unparsed, ts_min, ts_max, "
    "ts_src_min, ts_src_max, fingerprint, meta, connector_version, redaction_hash, sha256"
)


def sources(
    conn: sqlite3.Connection,
    versions: dict[str, str] | None = None,
    redaction_hash: str | None = None,
    blocked: Any = None,
) -> list[SourceRow]:
    """All sources, with ``original_missing`` and ``stale`` computed.

    ``versions`` maps connector names to their current version; ``blocked`` is a predicate on
    (name, path) that hides sources (policy ``blocked_sources``).
    """
    out: list[SourceRow] = []
    segs = dict(conn.execute("SELECT source_id, count(*) FROM segments GROUP BY source_id").fetchall())
    missing: dict[str, bool] = {}
    for r in conn.execute(f"SELECT {_SOURCE_COLS} FROM sources ORDER BY id").fetchall():
        (
            sid,
            name,
            fmt,
            path,
            member,
            stream,
            status,
            error,
            records,
            lines,
            unparsed,
            ts_min,
            ts_max,
            ts_src_min,
            ts_src_max,
            fingerprint,
            meta,
            cver,
            rhash,
            sha,
        ) = r
        if blocked is not None and blocked(name, path):
            continue
        if path not in missing:
            missing[path] = not Path(path).is_file()
        stale = (versions is not None and fmt in versions and versions[fmt] != cver) or (
            redaction_hash is not None and rhash != redaction_hash
        )
        out.append(
            SourceRow(
                sid,
                name,
                fmt,
                path,
                member,
                stream,
                status,
                error,
                records,
                lines,
                unparsed,
                ts_min,
                ts_max,
                ts_src_min,
                ts_src_max,
                fingerprint,
                json.loads(meta) if meta else {},
                cver,
                rhash,
                sha,
                missing[path],
                stale,
                segs.get(sid, 0),
            )
        )
    return out


def fmt_ns(ns: int | None, monotonic: bool = False) -> str | None:
    """Print a timestamp the way outputs show it (and from/to will accept it back in M3)."""
    if ns is None:
        return None
    if monotonic:
        secs, frac = divmod(ns, NS)
        return f"{secs}.{frac // 1000:06d}"
    from datetime import UTC, datetime

    secs, frac = divmod(ns, NS)
    dt = datetime.fromtimestamp(secs, UTC)
    return dt.strftime("%Y-%m-%d %H:%M:%S") + f".{frac // 1_000_000:03d}"


def time_range(s: SourceRow) -> tuple[str | None, str | None]:
    if s.ts_min is not None:
        return fmt_ns(s.ts_min), fmt_ns(s.ts_max)
    return fmt_ns(s.ts_src_min, True), fmt_ns(s.ts_src_max, True)


# -- records: filter, search, context --------------------------------------------------------

LOWER_BOUND = 0
UPPER_BOUND = (1 << 63) - 1


@dataclass
class Rec:
    """A stored record as the tools read it. ``ts_src`` is None when it equals ``ts``."""

    id: int
    ts: int | None
    ts_src: int | None
    ts_raw: str | None
    level: str | None
    tag: str | None
    pid: int | None
    tid: int | None
    msg: str
    template_id: int | None
    seg: int | None
    attrs_json: str | None

    @property
    def source_id(self) -> int:
        return self.id >> SEQ_BITS

    @property
    def seq(self) -> int:
        return self.id & SEQ_MASK

    @property
    def cite(self) -> str:
        return citation(self.source_id, self.seq)

    @property
    def attrs(self) -> dict[str, Any]:
        return json.loads(self.attrs_json) if self.attrs_json else {}

    @property
    def sort_key(self) -> tuple[int, int, int]:
        """Time order: wall-clock records by time, then records with only a monotonic clock."""
        return (0, self.ts, self.id) if self.ts is not None else (1, 0, self.id)


REC_COLS = (
    "r.id, r.ts, r.ts_src, r.ts_raw, r.level, t.name, r.pid, r.tid, r.msg, r.template_id, r.seg, r.attrs"
)
REC_FROM = "records r LEFT JOIN tags t ON t.id = r.tag_id"


def _rec(row: tuple[Any, ...]) -> Rec:
    return Rec(*row)


@dataclass
class RecordFilter:
    """A resolved record filter (all parts AND). ``sources`` None means every source in the DB."""

    sources: list[int] | None = None
    ts_from: int | None = None
    ts_to: int | None = None
    mono_from: int | None = None  # bounds on the monotonic clock (ts_src) of records without wall time
    mono_to: int | None = None
    min_level: str | None = None
    tag_ids: list[int] | None = None
    not_tag_ids: list[int] | None = None
    pid: int | None = None
    attrs: dict[str, Any] | None = None

    def where(self, skip: frozenset[str] = frozenset()) -> tuple[list[str], list[Any]]:
        """SQL conditions on ``records r``; ``skip`` names parts to leave out (empty-result reasons)."""
        where: list[str] = []
        params: list[Any] = []
        if self.sources is not None and "sources" not in skip:
            if not self.sources:
                where.append("0")
            else:
                where.append("(" + " OR ".join("r.id BETWEEN ? AND ?" for _ in self.sources) + ")")
                for sid in self.sources:
                    params.extend([sid << SEQ_BITS, ((sid + 1) << SEQ_BITS) - 1])
        if "time" not in skip:
            if self.ts_from is not None or self.ts_to is not None:
                where.append("r.ts BETWEEN ? AND ?")
                params.extend(
                    [
                        self.ts_from if self.ts_from is not None else -UPPER_BOUND,
                        self.ts_to if self.ts_to is not None else UPPER_BOUND,
                    ]
                )
            if self.mono_from is not None or self.mono_to is not None:
                where.append("r.ts IS NULL AND r.ts_src BETWEEN ? AND ?")
                params.extend(
                    [
                        self.mono_from if self.mono_from is not None else -UPPER_BOUND,
                        self.mono_to if self.mono_to is not None else UPPER_BOUND,
                    ]
                )
        if self.min_level and "level" not in skip:
            levels = [lvl for lvl, rank in LEVEL_RANK.items() if rank >= LEVEL_RANK[self.min_level]]
            where.append(f"r.level IN ({','.join('?' * len(levels))})")
            params.extend(levels)
        if self.tag_ids is not None and "tags" not in skip:
            if not self.tag_ids:
                where.append("0")
            else:
                where.append(f"r.tag_id IN ({','.join('?' * len(self.tag_ids))})")
                params.extend(self.tag_ids)
        if self.not_tag_ids and "not_tags" not in skip:
            where.append(f"(r.tag_id IS NULL OR r.tag_id NOT IN ({','.join('?' * len(self.not_tag_ids))}))")
            params.extend(self.not_tag_ids)
        if self.pid is not None and "pid" not in skip:
            where.append("r.pid = ?")
            params.append(self.pid)
        if self.attrs and "attrs" not in skip:
            for key, value in self.attrs.items():
                # Compare as text: attributes are stored as JSON (uid "1000", art 3, flags true).
                where.append("CAST(json_extract(r.attrs, ?) AS TEXT) = ?")
                params.extend([f'$."{key}"', _attr_text(value)])
        return where, params

    def parts(self) -> list[str]:
        """Names of the parts that are set, as :meth:`where` accepts them in ``skip``."""
        out = []
        if self.sources is not None:
            out.append("sources")
        if any(x is not None for x in (self.ts_from, self.ts_to, self.mono_from, self.mono_to)):
            out.append("time")
        if self.min_level:
            out.append("level")
        if self.tag_ids is not None:
            out.append("tags")
        if self.not_tag_ids:
            out.append("not_tags")
        if self.pid is not None:
            out.append("pid")
        if self.attrs:
            out.append("attrs")
        return out


def _attr_text(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def tag_ids(conn: sqlite3.Connection, globs: list[str]) -> tuple[list[int], list[str]]:
    """Tag ids matching any glob (case-sensitive), and the globs that matched no tag."""
    names = conn.execute("SELECT id, name FROM tags").fetchall()
    ids: list[int] = []
    unmatched: list[str] = []
    for g in globs:
        hit = [tid for tid, n in names if fnmatch.fnmatchcase(n, g)]
        if not hit:
            unmatched.append(g)
        ids.extend(hit)
    return sorted(set(ids)), unmatched


def similar_tags(conn: sqlite3.Connection, glob: str, n: int = 5) -> list[str]:
    """Tags close to a glob that matched nothing: case-insensitive matches first, then spelling."""
    import difflib

    names = [n for (n,) in conn.execute("SELECT name FROM tags")]
    low = glob.lower()
    ci = [x for x in names if fnmatch.fnmatchcase(x.lower(), low)]
    if ci:
        return ci[:n]
    return difflib.get_close_matches(glob.strip("*?"), names, n=n, cutoff=0.6)


_FTS_BARE = re.compile(r"^[\w\s]+$")
_HAS_UPPER = re.compile(r"[a-z][A-Z]|[A-Z]{2}[a-z]")
_FTS_OPS = ('"', " AND ", " OR ", " NOT ", "NEAR(")


def fts_query(text: str) -> str:
    """Pass simple word queries and queries with FTS5 operators through; otherwise quote each
    whitespace-separated token as a phrase (punctuation then only separates words). A trailing
    ``*`` keeps its meaning as a prefix query. A CamelCase word outside quotes also matches its
    parts in a row (``DisplayArea`` -> ``(DisplayArea OR "display area")``, core/words.py)."""
    if any(op in text for op in ('"', "NEAR(")):
        return text
    if _FTS_BARE.match(text) or any(op in text for op in _FTS_OPS):
        if not _HAS_UPPER.search(text):
            return text
        return _join_terms([tok if tok in _OPS else (query_alternative(tok) or tok) for tok in text.split()])
    out = []
    for tok in text.split():
        star = tok.endswith("*") and len(tok) > 1
        body = tok.rstrip("*") if star else tok
        alt = query_alternative(tok.strip(".,:;!?()[]{}'"))
        out.append(alt or ('"' + body.replace('"', '""') + '"' + ("*" if star else "")))
    return _join_terms(out)


_OPS = ("AND", "OR", "NOT")


def _join_terms(terms: list[str]) -> str:
    """Space-join; an explicit AND after a (group), which FTS5 does not AND implicitly."""
    if not any(t.startswith("(") for t in terms):
        return " ".join(terms)
    out: list[str] = []
    for t in terms:
        if out and out[-1] not in _OPS and t not in _OPS:
            out.append("AND")
        out.append(t)
    return " ".join(out)


FTS_MATCH = "r.id IN (SELECT rowid FROM records_fts WHERE records_fts MATCH ?)"
# Every k-th FTS hit by rowid: collapse groups of very broad queries are estimated from a sample.
FTS_SAMPLE = "r.id IN (SELECT rowid FROM records_fts WHERE records_fts MATCH ? AND rowid % ? = 0)"


def fts_count(conn: sqlite3.Connection, fts: str) -> int:
    return int(
        conn.execute("SELECT count(*) FROM records_fts WHERE records_fts MATCH ?", (fts,)).fetchone()[0]
    )


@dataclass
class Group:
    """Matches sharing source, template, tag and level: one line when collapsed."""

    source_id: int
    template_id: int | None
    tag_id: int | None
    level: str | None
    count: int
    first_id: int
    last_id: int
    first_ts: int | None  # earliest wall-clock time in the group (ordering)
    first_mono: int | None
    n_wall: int = 0  # matches with a wall-clock time

    @property
    def sort_key(self) -> tuple[int, int, int]:
        if self.first_ts is not None:
            return (0, self.first_ts, self.first_id)
        return (1, self.first_mono if self.first_mono is not None else 0, self.first_id)


def aggregate(conn: sqlite3.Connection, where: list[str], params: list[Any]) -> list[Group]:
    """Matches grouped for collapse, in time order of their first occurrence."""
    sql = (
        "SELECT r.id >> 40, r.template_id, r.tag_id, r.level, count(*), min(r.id), max(r.id), min(r.ts), "
        "min(r.ts_src), count(r.ts) FROM records r"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " GROUP BY r.id >> 40, r.template_id, r.tag_id, r.level"
    )
    groups = [Group(*row) for row in conn.execute(sql, params)]
    groups.sort(key=lambda g: g.sort_key)
    return groups


def select_ordered(
    conn: sqlite3.Connection,
    where: list[str],
    params: list[Any],
    offset: int,
    limit: int,
    n_wall: int,
    by_time_index: bool = False,
) -> list[Rec]:
    """Matching records in time order (``Rec.sort_key``), ``limit`` from ``offset``.

    ``n_wall`` is the number of matches with a wall-clock time (they come first). With
    ``by_time_index`` SQLite walks the ts index and checks each record (fast when matches are
    common); otherwise it collects the matches and sorts them (fast when they are rare)."""
    out: list[Rec] = []
    cond = " AND ".join(where) if where else "1"
    hint = " INDEXED BY records_ts" if by_time_index else ""
    if offset < n_wall:
        out = [
            _rec(row)
            for row in conn.execute(
                f"SELECT {REC_COLS} FROM records r{hint} LEFT JOIN tags t ON t.id = r.tag_id "
                f"WHERE {cond} AND r.ts IS NOT NULL "
                "ORDER BY r.ts, r.id LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
        ]
    if len(out) < limit:
        out += [
            _rec(row)
            for row in conn.execute(
                f"SELECT {REC_COLS} FROM {REC_FROM} WHERE {cond} AND r.ts IS NULL "
                "ORDER BY r.id LIMIT ? OFFSET ?",
                [*params, limit - len(out), max(0, offset - n_wall)],
            )
        ]
    return out


def records_by_id(conn: sqlite3.Connection, ids: list[int]) -> dict[int, Rec]:
    out: dict[int, Rec] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        for row in conn.execute(
            f"SELECT {REC_COLS} FROM {REC_FROM} WHERE r.id IN ({','.join('?' * len(chunk))})", chunk
        ):
            out[row[0]] = _rec(row)
    return out


def template_texts(conn: sqlite3.Connection, ids: list[int]) -> dict[int, str]:
    ids = [i for i in set(ids) if i is not None]
    out: dict[int, str] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        out.update(
            conn.execute(f"SELECT id, text FROM templates WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        )
    return out


def tag_names(conn: sqlite3.Connection, ids: list[int | None]) -> dict[int, str]:
    wanted = [i for i in set(ids) if i is not None]
    if not wanted:
        return {}
    return dict(
        conn.execute(f"SELECT id, name FROM tags WHERE id IN ({','.join('?' * len(wanted))})", wanted)
    )


# -- regex: SQL prefilter and capped scan -----------------------------------------------------

_TOKEN_CHARS = re.compile(r"[0-9A-Za-z\u0080-\U0010ffff]+")
_QUANT = re.compile(r"\{(\d*)(?:,(\d*))?\}")
_CLASS_ESCAPES = set("dDwWsSbBAZzG")
_CHAR_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "f": "\f", "v": "\v"}


def required_literals(pattern: str) -> list[str]:
    """Literal substrings every match of ``pattern`` must contain (conservative: [] when unsure).

    Only top-level text outside groups and classes counts; a top-level ``|`` or inline flags
    (``(?i)`` …) give no literals."""
    if re.search(r"\(\?[a-zA-Z-]*[aiLmsux]", pattern):
        return []
    runs: list[str] = []
    cur: list[str] = []
    depth = 0
    i, n = 0, len(pattern)

    def flush() -> None:
        if cur and depth == 0:
            runs.append("".join(cur))
        cur.clear()

    while i < n:
        c = pattern[i]
        if c == "\\" and i + 1 < n:
            e = pattern[i + 1]
            i += 2
            if e in _CLASS_ESCAPES or e.isdigit() or e in "xuUNp":
                flush()
                if e in "xuUNp":
                    return [r for r in runs if len(r) >= 3]  # rare escapes: stop collecting here
            elif depth == 0:
                cur.append(_CHAR_ESCAPES.get(e, e))
            continue
        if c == "[":
            flush()
            j = i + 1
            if j < n and pattern[j] == "^":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 2 if pattern[j] == "\\" else 1
            i = j + 1
            continue
        if c == "|":
            if depth == 0:
                return []
            i += 1
            continue
        if c == "(":
            flush()
            depth += 1
            i += 1
            continue
        if c == ")":
            depth = max(0, depth - 1)
            i += 1
            continue
        if c in ".^$":
            flush()
            i += 1
            continue
        if c in "*?":
            if cur:
                cur.pop()
            flush()
            i += 1
            continue
        if c == "+":
            flush()
            i += 1
            continue
        if c == "{":
            m = _QUANT.match(pattern, i)
            if m:
                if cur and (m.group(1) in ("", "0")):
                    cur.pop()
                flush()
                i = m.end()
                continue
        if depth == 0:
            cur.append(c)
        i += 1
    flush()
    return [r for r in runs if len(r) >= 3]


def literals_fts(literals: list[str]) -> str | None:
    """An FTS5 query that every record containing all ``literals`` matches (a superset), or None.

    Word pieces strictly inside a literal are whole tokens; a piece at its end can be a prefix."""
    terms: list[str] = []
    for lit in literals:
        for m in _TOKEN_CHARS.finditer(lit):
            if m.start() == 0:
                continue  # may be the end of a longer token
            tok = m.group().replace('"', "")
            if m.end() < len(lit):
                terms.append(f'"{tok}"')
            elif len(tok) >= 2:
                terms.append(f'"{tok}"*')
    return " ".join(dict.fromkeys(terms)) or None


@dataclass
class ScanResult:
    groups: list[Group]
    first: list[Rec]  # the first matches in time order, up to the requested window
    scanned: int
    complete: bool
    last_scanned: int | None  # record id where a capped scan stopped


def regex_prefilter(pattern: str) -> tuple[list[str], list[Any]]:
    """SQL conditions (on ``records r`` joined with ``tags t``) that every record matching the regex
    ``pattern`` over message or tag passes: its literal parts as ``instr`` and an FTS superset."""
    lits = required_literals(pattern)
    where: list[str] = []
    params: list[Any] = []
    lit_fts = literals_fts(lits)
    if lit_fts:
        where.append(FTS_MATCH)
        params.append(lit_fts)
    for lit in lits:
        where.append("(instr(r.msg, ?) > 0 OR instr(coalesce(t.name, ''), ?) > 0)")
        params.extend([lit, lit])
    return where, params


# A record test for regex_scan: (msg, tag, level, tag_id) -> match.
RecordTest = Callable[[str, str | None, str | None, int | None], bool]


def regex_scan(
    conn: sqlite3.Connection,
    where: list[str],
    params: list[Any],
    rx: re.Pattern[str] | RecordTest,
    max_rows: int,
    window: int,
) -> ScanResult:
    """Scan candidate records (``where``, in id order) with ``rx`` over message and tag (or with a
    record test, e.g. a detector rule's conditions).

    Stops after ``max_rows`` candidates. Returns groups like :func:`aggregate` and the first
    ``window`` matches in time order."""
    import heapq

    cond = " AND ".join(where) if where else "1"
    sql = f"SELECT {REC_COLS}, r.tag_id FROM {REC_FROM} WHERE {cond} ORDER BY r.id LIMIT ?"
    groups: dict[tuple[Any, ...], Group] = {}
    heap: list[tuple[tuple[int, int, int], int, Rec]] = []  # max-heap by negated key
    scanned = 0
    last: int | None = None
    search = rx.search if isinstance(rx, re.Pattern) else None
    test = None if isinstance(rx, re.Pattern) else rx
    for row in conn.execute(sql, [*params, max_rows + 1]):
        if scanned >= max_rows:
            return ScanResult(_sorted(groups), _heap_sorted(heap), scanned, False, last)
        scanned += 1
        last = row[0]
        msg, tag = row[8], row[5]
        if search is not None:
            if not (search(msg) or (tag and search(tag))):
                continue
        elif test is not None and not test(msg, tag, row[4], row[12]):
            continue
        rec = _rec(row[:12])
        key = (rec.source_id, rec.template_id, row[12], rec.level)
        g = groups.get(key)
        if g is None:
            groups[key] = Group(
                rec.source_id,
                rec.template_id,
                row[12],
                rec.level,
                1,
                rec.id,
                rec.id,
                rec.ts,
                rec.ts_src if rec.ts is None else None,
                1 if rec.ts is not None else 0,
            )
        else:
            g.count += 1
            g.last_id = rec.id
            g.n_wall += rec.ts is not None
            if rec.ts is not None and (g.first_ts is None or rec.ts < g.first_ts):
                g.first_ts = rec.ts
            if (
                rec.ts is None
                and rec.ts_src is not None
                and (g.first_mono is None or rec.ts_src < g.first_mono)
            ):
                g.first_mono = rec.ts_src
        sk = rec.sort_key
        neg = (-sk[0], -sk[1], -sk[2])
        if window <= 0:
            continue
        if len(heap) < window:
            heapq.heappush(heap, (neg, rec.id, rec))
        elif neg > heap[0][0]:
            heapq.heapreplace(heap, (neg, rec.id, rec))
    return ScanResult(_sorted(groups), _heap_sorted(heap), scanned, True, last)


def _sorted(groups: dict[tuple[Any, ...], Group]) -> list[Group]:
    return sorted(groups.values(), key=lambda g: g.sort_key)


def _heap_sorted(heap: list[tuple[tuple[int, int, int], int, Rec]]) -> list[Rec]:
    return sorted((r for _, _, r in heap), key=lambda r: r.sort_key)


def count_by_source(conn: sqlite3.Connection, where: list[str], params: list[Any]) -> dict[int, int]:
    """Matching records per source id."""
    cond = " AND ".join(where) if where else "1"
    sql = f"SELECT r.id >> {SEQ_BITS}, count(*) FROM records r WHERE {cond} GROUP BY 1"
    return {int(sid): int(n) for sid, n in conn.execute(sql, params)}


def count_candidates(conn: sqlite3.Connection, where: list[str], params: list[Any]) -> int:
    cond = " AND ".join(where) if where else "1"
    return int(conn.execute(f"SELECT count(*) FROM records r WHERE {cond}", params).fetchone()[0])


# -- context ----------------------------------------------------------------------------------


def source_neighbours(conn: sqlite3.Connection, rid: int, before: int, after: int) -> list[Rec]:
    """Records of the same source around ``rid`` (by seq), ``rid`` included."""
    sid = rid >> SEQ_BITS
    lo, hi = sid << SEQ_BITS, ((sid + 1) << SEQ_BITS) - 1
    prev = conn.execute(
        f"SELECT {REC_COLS} FROM {REC_FROM} WHERE r.id < ? AND r.id >= ? ORDER BY r.id DESC LIMIT ?",
        (rid, lo, before),
    ).fetchall()
    nxt = conn.execute(
        f"SELECT {REC_COLS} FROM {REC_FROM} WHERE r.id >= ? AND r.id <= ? ORDER BY r.id LIMIT ?",
        (rid, hi, after + 1),
    ).fetchall()
    return [_rec(r) for r in reversed(prev)] + [_rec(r) for r in nxt]


def _seq_at_time(conn: sqlite3.Connection, sid: int, ts: int, last_seq: int) -> int:
    """Binary search for the first seq of a source whose time is >= ``ts`` (seq is roughly time
    ordered within a source; records without a time are skipped by probing forward)."""
    lo, hi = 1, last_seq + 1
    base = sid << SEQ_BITS
    while lo < hi:
        mid = (lo + hi) // 2
        row = conn.execute(
            "SELECT id, ts FROM records WHERE id >= ? AND id < ? AND ts IS NOT NULL ORDER BY id LIMIT 1",
            (base | mid, base | hi),
        ).fetchone()
        if row is None:
            hi = mid
            continue
        if row[1] < ts:
            lo = (row[0] & SEQ_MASK) + 1
        else:
            hi = mid
    return lo


def pid_neighbours(
    conn: sqlite3.Connection,
    anchor: Rec,
    sources: list[tuple[int, int]],
    before: int,
    after: int,
    seg_range: tuple[int, int] | None,
) -> list[Rec]:
    """Records of ``anchor.pid`` around the anchor, merged by time over ``sources``
    ((source id, last seq) pairs; the anchor's own source included). ``seg_range`` limits the
    anchor's source to the seqs of its segment (pids are reused after a reboot)."""
    pid = anchor.pid
    found: list[Rec] = []
    for sid, last_seq in sources:
        base = sid << SEQ_BITS
        if sid == anchor.source_id:
            pivot = anchor.id
            lo_id, hi_id = (
                (base | seg_range[0], base | seg_range[1]) if seg_range else (base, base | SEQ_MASK)
            )
        else:
            if anchor.ts is None:
                continue
            pivot = base | _seq_at_time(conn, sid, anchor.ts, last_seq)
            lo_id, hi_id = base, base | SEQ_MASK
        prev = conn.execute(
            f"SELECT {REC_COLS} FROM {REC_FROM} WHERE r.pid = ? AND r.id < ? AND r.id >= ? "
            "ORDER BY r.id DESC LIMIT ?",
            (pid, pivot, lo_id, before + 1),
        ).fetchall()
        nxt = conn.execute(
            f"SELECT {REC_COLS} FROM {REC_FROM} WHERE r.pid = ? AND r.id >= ? AND r.id <= ? "
            "ORDER BY r.id LIMIT ?",
            (pid, pivot, hi_id, after + 1),
        ).fetchall()
        found += [_rec(r) for r in prev] + [_rec(r) for r in nxt]
    found = list({r.id: r for r in found}.values())
    if anchor.ts is None:
        found = [r for r in found if r.source_id == anchor.source_id]
        found.sort(key=lambda r: r.id)
    else:
        found.sort(key=lambda r: (r.ts if r.ts is not None else anchor.ts, r.id))
    idx = next(i for i, r in enumerate(found) if r.id == anchor.id)
    return found[max(0, idx - before) : idx + after + 1]


def segment_of(conn: sqlite3.Connection, sid: int, seq: int) -> tuple[int, int] | None:
    row = conn.execute(
        "SELECT first_seq, last_seq FROM segments WHERE source_id = ? AND first_seq <= ? AND last_seq >= ?",
        (sid, seq, seq),
    ).fetchone()
    return (row[0], row[1]) if row else None


def relink(conn: sqlite3.Connection, source_id: int, new_path: Path, sha256_of: Any) -> list[int]:
    """Point every source of the same original at ``new_path`` after checking its hash."""
    row = conn.execute("SELECT path, sha256 FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise ValueError(f"no source {source_id} in this case")
    old_path, sha = row
    if not new_path.is_file():
        raise ValueError(f"no such file: {new_path}")
    actual = sha256_of(new_path)
    if actual != sha:
        raise ValueError(
            f"{new_path} is not the file source {source_id} was indexed from (SHA-256 {actual[:12]}… "
            f"differs from {sha[:12]}…)"
        )
    with conn:
        ids = [
            sid
            for (sid,) in conn.execute(
                "SELECT id FROM sources WHERE sha256 = ? AND path = ?", (sha, old_path)
            )
        ]
        conn.execute(
            "UPDATE sources SET path = ? WHERE sha256 = ? AND path = ?", (str(new_path), sha, old_path)
        )
    return ids


# -- artifacts --------------------------------------------------------------------------------

# art:<id>[/<section>][:L<line>[-L<line>]] (D6); 'art:' may be left out on the command line.
ART_CITE = re.compile(r"(?:art:)?(\d+)(?:/([a-z0-9_.-]+))?(?::L(\d+)(?:-L?(\d+))?)?")


@dataclass
class SectionRow:
    name: str
    first_line: int
    lines: int

    @property
    def last_line(self) -> int:
        return self.first_line + self.lines - 1


@dataclass
class ArtifactRow:
    id: int
    source_id: int
    record_id: int
    type: str
    name: str
    ts: int | None
    ts_raw: str | None
    pid: int | None
    tid: int | None
    process: str | None
    signature: str | None
    headline: str
    summary: str
    fields: dict[str, Any]
    lines: int
    sections: list[SectionRow] = field(default_factory=list)

    @property
    def cite(self) -> str:
        return f"art:{self.id}"

    @property
    def record_cite(self) -> str:
        return citation(self.record_id >> SEQ_BITS, self.record_id & ((1 << SEQ_BITS) - 1))


@dataclass(frozen=True)
class ArtCitation:
    id: int
    section: str | None = None
    first: int | None = None
    last: int | None = None


def parse_art_citation(text: str) -> ArtCitation:
    m = ART_CITE.fullmatch(text.strip())
    if m is None:
        raise ValueError(f"invalid artifact citation {text!r}; expected art:<id>[/<section>][:L<line>]")
    first = int(m[3]) if m[3] else None
    last = int(m[4]) if m[4] else first
    if first is not None and (first < 1 or (last is not None and last < first)):
        raise ValueError(f"invalid line range in {text!r}")
    return ArtCitation(int(m[1]), m[2], first, last)


_ART_COLS = (
    "id, source_id, record_id, type, name, ts, ts_raw, pid, tid, process, signature, headline, summary, "
    "fields, lines"
)


def _artifact(row: tuple[Any, ...]) -> ArtifactRow:
    values = list(row)
    values[13] = json.loads(values[13]) if values[13] else {}
    return ArtifactRow(*values)


def artifacts(
    conn: sqlite3.Connection, type: str | None = None, sources: list[int] | None = None
) -> list[ArtifactRow]:
    """Artifacts in time order (unknown times last), without sections."""
    where: list[str] = []
    params: list[Any] = []
    if type:
        where.append("type = ?")
        params.append(type)
    if sources is not None:
        if not sources:
            return []
        where.append(f"source_id IN ({','.join('?' * len(sources))})")
        params.extend(sources)
    sql = f"SELECT {_ART_COLS} FROM artifacts"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts IS NULL, ts, id"
    return [_artifact(r) for r in conn.execute(sql, params)]


def artifacts_near(
    conn: sqlite3.Connection, a: ArtifactRow, window_ns: int, sources: list[int]
) -> list[ArtifactRow]:
    """Artifacts of the same type and pid within ``window_ns`` of ``a`` (uses the ts index)."""
    assert a.ts is not None
    rows = conn.execute(
        f"SELECT {_ART_COLS} FROM artifacts WHERE ts BETWEEN ? AND ? AND type = ? AND pid = ? "
        "ORDER BY ts, id",
        (a.ts - window_ns, a.ts + window_ns, a.type, a.pid),
    )
    allowed = set(sources)
    return [r for r in map(_artifact, rows) if r.source_id in allowed]


def artifact(conn: sqlite3.Connection, art_id: int) -> ArtifactRow | None:
    row = conn.execute(f"SELECT {_ART_COLS} FROM artifacts WHERE id = ?", (art_id,)).fetchone()
    if row is None:
        return None
    a = _artifact(row)
    a.sections = [
        SectionRow(*r)
        for r in conn.execute(
            "SELECT name, first_line, lines FROM artifact_sections WHERE artifact_id = ? ORDER BY idx",
            (art_id,),
        )
    ]
    return a


def artifact_lines(conn: sqlite3.Connection, a: ArtifactRow, cite: ArtCitation) -> list[tuple[int, str]]:
    """The lines a citation names, as (line number in the artifact, text).

    ``/section`` alone gives the section; ``:L<n>`` (optionally ``-L<m>``) the lines, which must lie
    inside the section when one is given. Raises ValueError for unknown sections or lines.
    """
    secs = a.sections
    if cite.section is not None:
        sec = next((s for s in secs if s.name == cite.section), None)
        if sec is None:
            names = ", ".join(s.name for s in secs[:20])
            raise ValueError(f"{a.cite} has no section '{cite.section}'; sections: {names}")
        lo, hi = sec.first_line, sec.last_line
    else:
        lo, hi = 1, a.lines
    if cite.first is not None:
        last = cite.last if cite.last is not None else cite.first
        if cite.first < lo or last > hi:
            where = f"section '{cite.section}' (L{lo}-L{hi})" if cite.section else f"{a.cite} (L1-L{hi})"
            raise ValueError(
                f"line L{cite.first}" + (f"-L{last}" if last != cite.first else "") + f" is outside {where}"
            )
        lo, hi = cite.first, last
    out: list[tuple[int, str]] = []
    for first, text in conn.execute(
        "SELECT first_line, text FROM artifact_sections WHERE artifact_id = ? AND first_line <= ? "
        "AND first_line + lines - 1 >= ? ORDER BY idx",
        (a.id, hi, lo),
    ):
        for i, line in enumerate(text.split("\n")):
            n = first + i
            if lo <= n <= hi:
                out.append((n, line))
    return out


# -- counts, buckets and gaps (count, overview) -----------------------------------------------

# Group keys for count(by=...): SQL over ``records r``.
COUNT_KEYS = {
    "level": "r.level",
    "tag": "r.tag_id",
    "pid": "r.pid",
    "source": "r.id >> 40",
    "template": "r.template_id",
}


def _where_sql(where: list[str]) -> str:
    return (" WHERE " + " AND ".join(where)) if where else ""


def group_counts(
    conn: sqlite3.Connection, key: str, where: list[str], params: list[Any]
) -> list[tuple[Any, int, int]]:
    """(key, count, smallest record id) per value of ``key`` (a COUNT_KEYS name), most frequent
    first. Tag conditions need ``tags t``: they join it."""
    join = " LEFT JOIN tags t ON t.id = r.tag_id" if any("t.name" in w for w in where) else ""
    sql = (
        f"SELECT {COUNT_KEYS[key]} AS k, count(*), min(r.id) FROM records r{join}{_where_sql(where)} "
        "GROUP BY k ORDER BY 2 DESC, 3"
    )
    return [(k, n, i) for k, n, i in conn.execute(sql, params)]


def bucket_counts(
    conn: sqlite3.Connection,
    step: int,
    where: list[str],
    params: list[Any],
    key: str | None = None,
    mono: bool = False,
) -> list[tuple[int, Any, int, int]]:
    """(bucket start ns, key value or None, count, smallest record id) per time bucket of ``step``
    ns; on the wall clock (``ts``), or with ``mono`` on the monotonic clock of records without one."""
    col = "r.ts_src" if mono else "r.ts"
    clock = "r.ts IS NULL AND r.ts_src IS NOT NULL" if mono else "r.ts IS NOT NULL"
    kexpr = COUNT_KEYS[key] if key else "NULL"
    join = " LEFT JOIN tags t ON t.id = r.tag_id" if any("t.name" in w for w in where) else ""
    sql = (
        f"SELECT {col} / ? AS b, {kexpr} AS k, count(*), min(r.id) FROM records r{join}"
        f"{_where_sql([clock, *where])} GROUP BY b, k"
    )
    return [(b * step, k, n, i) for b, k, n, i in conn.execute(sql, [step, *params])]


def level_bucket_counts(
    conn: sqlite3.Connection, step: int, where: list[str], params: list[Any]
) -> list[tuple[int, str | None, int]]:
    """(bucket start, level, count) on the wall clock: histogram and level counts in one pass."""
    sql = (
        f"SELECT r.ts / ? AS b, r.level, count(*) FROM records r{_where_sql(['r.ts IS NOT NULL', *where])} "
        "GROUP BY b, r.level"
    )
    return [(b * step, lvl, n) for b, lvl, n in conn.execute(sql, [step, *params])]


@dataclass
class Gap:
    before_id: int  # last record before the gap
    before_ts: int
    after_id: int  # first record after it
    after_ts: int


def gaps(
    conn: sqlite3.Connection,
    where: list[str],
    params: list[Any],
    lo: int,
    hi: int,
    min_gap: int,
    max_steps: int = 50_000,
) -> tuple[list[Gap], bool]:
    """Stretches of more than ``min_gap`` ns without a record (wall clock) between ``lo`` and
    ``hi``. Walks the ts index in jumps of ``min_gap``: about (hi - lo) / min_gap lookups. The
    flag is False when ``max_steps`` stopped the walk early."""
    cond = " AND ".join(where) if where else "1"
    last_in = (
        f"SELECT r.ts FROM records r INDEXED BY records_ts WHERE r.ts BETWEEN ? AND ? AND {cond} "
        "ORDER BY r.ts DESC LIMIT 1"
    )
    next_after = (
        f"SELECT r.ts FROM records r INDEXED BY records_ts WHERE r.ts > ? AND r.ts <= ? AND {cond} "
        "ORDER BY r.ts LIMIT 1"
    )
    id_at = f"SELECT min(r.id), max(r.id) FROM records r WHERE r.ts = ? AND {cond}"
    out: list[Gap] = []
    row = conn.execute(
        f"SELECT r.ts FROM records r INDEXED BY records_ts WHERE r.ts >= ? AND r.ts <= ? AND {cond} "
        "ORDER BY r.ts LIMIT 1",
        [lo, hi, *params],
    ).fetchone()
    if row is None:
        return out, True
    t = row[0]
    for _ in range(max_steps):
        r = conn.execute(last_in, [t, min(t + min_gap, hi), *params]).fetchone()
        if r is not None and r[0] > t:
            t = r[0]
            continue
        r = conn.execute(next_after, [t, hi, *params]).fetchone()
        if r is None:
            return out, True
        if r[0] - t > min_gap:
            before = conn.execute(id_at, [t, *params]).fetchone()[1]
            after = conn.execute(id_at, [r[0], *params]).fetchone()[0]
            out.append(Gap(before, t, after, r[0]))
        t = r[0]
    return out, False


def segments(
    conn: sqlite3.Connection, sources: list[int]
) -> list[tuple[int, int, str, int, int, int | None, int | None]]:
    """(source, seg, reason, first_seq, count, ts_first, ts_src_first) of the given sources."""
    if not sources:
        return []
    rows = conn.execute(
        "SELECT source_id, seg, reason, first_seq, count, ts_first, ts_src_first FROM segments "
        f"WHERE source_id IN ({','.join('?' * len(sources))}) ORDER BY source_id, seg",
        sources,
    )
    return [tuple(r) for r in rows]


def process_names(conn: sqlite3.Connection, pids: list[int]) -> dict[int, str]:
    """Process names from bugreport process lists (the main thread's name when known)."""
    pids = [p for p in set(pids) if p is not None]
    if not pids:
        return {}
    out: dict[int, str] = {}
    marks = ",".join("?" * len(pids))
    for pid, name in conn.execute(
        f"SELECT pid, name FROM processes WHERE pid IN ({marks}) ORDER BY tid != pid", pids
    ):
        out.setdefault(pid, name)
    return out


def regex_tally(
    conn: sqlite3.Connection,
    where: list[str],
    params: list[Any],
    rx: re.Pattern[str],
    max_rows: int,
    key: str | None,
    step: int | None,
    mono: bool = False,
) -> tuple[dict[tuple[int | None, Any], list[int]], bool]:
    """Regex matches counted per (bucket start or None, key value): [count, smallest id].
    Scans at most ``max_rows`` candidates; the flag is False when it stopped early."""
    kcol = COUNT_KEYS[key] if key else "NULL"
    tcol = "r.ts_src" if mono else "r.ts"
    cond = list(where)
    if step:
        cond.append("r.ts IS NULL AND r.ts_src IS NOT NULL" if mono else "r.ts IS NOT NULL")
    sql = (
        f"SELECT r.id, r.msg, t.name, {kcol}, {tcol} FROM {REC_FROM}{_where_sql(cond)} ORDER BY r.id LIMIT ?"
    )
    out: dict[tuple[int | None, Any], list[int]] = {}
    search = rx.search
    for n, (rid, msg, tag, k, t) in enumerate(conn.execute(sql, [*params, max_rows + 1])):
        if n >= max_rows:
            return out, False
        if not (search(msg) or (tag and search(tag))):
            continue
        b = (t // step) * step if step and t is not None else None
        cell = out.get((b, k))
        if cell is None:
            out[(b, k)] = [1, rid]
        else:
            cell[0] += 1
    return out, True


def ts_bounds(
    conn: sqlite3.Connection, where: list[str], params: list[Any], mono: bool = False
) -> tuple[int | None, int | None]:
    """First and last time of the records passing ``where``: wall clock, or with ``mono`` the
    monotonic clock of records without a wall-clock time. Walks the index from both ends."""
    cond = " AND ".join(where) if where else "1"
    if mono:
        base = f"SELECT r.ts_src FROM records r WHERE r.ts IS NULL AND r.ts_src IS NOT NULL AND {cond}"
        lo = conn.execute(f"{base} ORDER BY r.ts_src LIMIT 1", params).fetchone()
        hi = conn.execute(f"{base} ORDER BY r.ts_src DESC LIMIT 1", params).fetchone()
    else:
        base = f"SELECT r.ts FROM records r INDEXED BY records_ts WHERE r.ts IS NOT NULL AND {cond}"
        lo = conn.execute(f"{base} ORDER BY r.ts LIMIT 1", params).fetchone()
        hi = conn.execute(f"{base} ORDER BY r.ts DESC LIMIT 1", params).fetchone()
    return (lo[0] if lo else None), (hi[0] if hi else None)


def first_by_time(conn: sqlite3.Connection, where: list[str], params: list[Any]) -> int | None:
    """Id of the earliest wall-clock record passing ``where``."""
    cond = " AND ".join(where) if where else "1"
    row = conn.execute(
        f"SELECT r.id FROM records r INDEXED BY records_ts WHERE r.ts IS NOT NULL AND {cond} "
        "ORDER BY r.ts, r.id LIMIT 1",
        params,
    ).fetchone()
    return row[0] if row else None
