"""Read side of a case DB: source listing, a basic search, record formatting, relink.

M1 provides what the CLI ``query`` and the ``list_sources`` tool need. The full filter object,
collapse, cursors and caps arrive with the P0 tools in M3.
"""

from __future__ import annotations

import fnmatch
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.levels import LEVEL_RANK
from ..core.timeparse import NS, ts_raw_of
from ..model import SEQ_BITS, citation


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


@dataclass
class Hit:
    source_id: int
    seq: int
    ts_raw: str | None
    level: str | None
    tag: str | None
    pid: int | None
    tid: int | None
    msg: str
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def cite(self) -> str:
        return citation(self.source_id, self.seq)


@dataclass
class Query:
    text: str | None = None
    regex: str | None = None
    min_level: str | None = None
    tags: list[str] | None = None
    sources: list[int] | None = None
    pid: int | None = None
    limit: int = 50


_FTS_BARE = re.compile(r"^[\w\s]+$")


def fts_query(text: str) -> str:
    """Pass simple word queries through; quote anything else as one phrase per token."""
    if _FTS_BARE.match(text) or any(op in text for op in ('"', " AND ", " OR ", " NOT ", "NEAR(")):
        return text
    return " ".join('"' + tok.replace('"', '""') + '"' for tok in text.split())


def search(conn: sqlite3.Connection, q: Query, ts_formats: dict[int, str | None]) -> tuple[list[Hit], int]:
    """Return up to ``q.limit`` hits (in source/seq order) and the total number of matches."""
    where: list[str] = []
    params: list[Any] = []
    if q.text:
        where.append("r.id IN (SELECT rowid FROM records_fts WHERE records_fts MATCH ?)")
        params.append(fts_query(q.text))
    if q.min_level:
        levels = [lvl for lvl, rank in LEVEL_RANK.items() if rank >= LEVEL_RANK[q.min_level]]
        where.append(f"r.level IN ({','.join('?' * len(levels))})")
        params.extend(levels)
        if LEVEL_RANK[q.min_level] >= LEVEL_RANK["W"]:
            where.append("r.level IN ('W', 'E', 'F')")  # lets SQLite use the partial level index
    if q.tags:
        ids = [
            tid
            for (tid, n) in conn.execute("SELECT id, name FROM tags").fetchall()
            if any(fnmatch.fnmatchcase(n, g) for g in q.tags)
        ]
        if not ids:
            return [], 0
        where.append(f"r.tag_id IN ({','.join('?' * len(ids))})")
        params.extend(ids)
    if q.sources:
        where.append("(" + " OR ".join("r.id BETWEEN ? AND ?" for _ in q.sources) + ")")
        for sid in q.sources:
            params.extend([sid << SEQ_BITS, ((sid + 1) << SEQ_BITS) - 1])
    if q.pid is not None:
        where.append("r.pid = ?")
        params.append(q.pid)
    rx = re.compile(q.regex) if q.regex else None
    sql = (
        "SELECT r.id, r.ts, r.ts_src, r.ts_raw, r.level, t.name, r.pid, r.tid, r.msg, r.attrs "
        "FROM records r LEFT JOIN tags t ON t.id = r.tag_id"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY r.id"
    )
    hits: list[Hit] = []
    total = 0
    for rid, ts, ts_src, ts_raw, level, tag, pid, tid, msg, attrs in conn.execute(sql, params):
        if rx is not None and not (rx.search(msg) or (tag and rx.search(tag))):
            continue
        total += 1
        if len(hits) < q.limit:
            sid, seq = rid >> SEQ_BITS, rid & ((1 << SEQ_BITS) - 1)
            hits.append(
                Hit(
                    sid,
                    seq,
                    ts_raw_of(ts_raw, ts, ts_src, ts_formats.get(sid)),
                    level,
                    tag,
                    pid,
                    tid,
                    msg,
                    json.loads(attrs) if attrs else {},
                )
            )
    return hits, total


def format_hit(h: Hit, max_chars: int = 200) -> str:
    """``log:2#18233  10-11 08:12:03.412  E 1234/1250 HvacCtrl  message`` (one line)."""
    msg = h.msg.replace("\n", " ⏎ ")
    if len(msg) > max_chars:
        msg = msg[: max_chars - 1] + "…"
    who = (
        f"{h.pid}/{h.tid}"
        if h.pid is not None and h.tid is not None
        else (str(h.pid) if h.pid is not None else "")
    )
    parts = [h.cite, h.ts_raw or "-", h.level or "-", who, h.tag or ""]
    head = "  ".join(parts[:2]) + "  " + " ".join(p for p in parts[2:] if p)
    return f"{head}  {msg}"


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
