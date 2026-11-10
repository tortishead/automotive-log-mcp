"""Case DBs: open and migrate, and the batch writer (D3).

One SQLite file per case, WAL journal, ``synchronous=NORMAL``. Writers commit every 50k records,
with the FTS rows in the same transaction, so readers (other connections) always see a
consistent, committed prefix of an import while it runs.

Connections are short-lived and closed explicitly (Windows keeps files locked while open).
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from importlib import resources
from pathlib import Path
from typing import Any

from ..core.words import camel_parts

SCHEMA_VERSION = 3
BATCH_ROWS = 50_000


class StoreError(RuntimeError):
    """Raised when the store cannot work in this environment or on this file."""


FTS5_HELP = (
    "This Python's SQLite library ({version}) was built without FTS5, which automotive-log-mcp "
    "needs for full-text search. Use a Python whose sqlite3 module includes FTS5: the python.org "
    "installers and most Linux distribution builds do; on conda, install the 'sqlite' package from "
    "conda-forge; or build Python against a SQLite compiled with SQLITE_ENABLE_FTS5."
)


def check_fts5() -> None:
    """Fail with a clear message when FTS5 is unavailable (M0 item 5)."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE fts5_check USING fts5(x)")
    except sqlite3.OperationalError as e:
        raise StoreError(FTS5_HELP.format(version=sqlite3.sqlite_version)) from e
    finally:
        conn.close()


def _schema_sql(name: str = "schema.sql") -> str:
    return resources.files("automotive_log_mcp.store").joinpath(name).read_text(encoding="utf-8")


# Steps from version n-1 to n, in order. Version 1 is schema.sql; later steps never edit it, so a
# case DB of any older version opens and is brought up to date.
MIGRATIONS: dict[int, str] = {1: "schema.sql", 2: "schema_v2.sql", 3: "schema_v3.sql"}


def _migrate(conn: sqlite3.Connection, path: Path) -> None:
    has_meta = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
    version = 0
    if has_meta:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        version = int(row[0]) if row else 0
    if version > SCHEMA_VERSION:
        raise StoreError(
            f"case DB {path} has schema version {version}, newer than this automotive-log-mcp "
            f"supports ({SCHEMA_VERSION}); upgrade automotive-log-mcp"
        )
    if version == SCHEMA_VERSION:
        return
    for step in range(version + 1, SCHEMA_VERSION + 1):
        # executescript commits first; each step and its version bump are one transaction.
        conn.executescript(
            "BEGIN;\n"
            + _schema_sql(MIGRATIONS[step])
            + f"\nINSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', '{step}');\nCOMMIT;"
        )


def connect(path: Path, create: bool = False) -> sqlite3.Connection:
    """Open a case DB (migrating it if needed). ``create`` makes the file if it is missing."""
    if not path.exists():
        if not create:
            raise StoreError(f"case DB {path} does not exist")
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(path, check_same_thread=False)
    except sqlite3.Error as e:
        raise StoreError(f"cannot open case DB {path}: {e}") from e
    try:
        conn.create_function("camel_parts", 2, camel_parts, deterministic=True)
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _migrate(conn, path)
    except sqlite3.DatabaseError as e:
        conn.close()
        raise StoreError(f"{path} is not a usable case DB: {e}") from e
    except BaseException:
        conn.close()
        raise
    return conn


@contextlib.contextmanager
def opened(path: Path, create: bool = False) -> Iterator[sqlite3.Connection]:
    conn = connect(path, create=create)
    try:
        yield conn
    finally:
        conn.close()


def dumps(value: Any) -> str | None:
    if not value:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


Statement = tuple[str, tuple[Any, ...]]


class BatchWriter:
    """Buffers record rows and writes them with their FTS rows, one transaction per batch.

    Templates and tags are interned in memory (text -> id). ``on_flush`` callables return extra
    statements (e.g. progress counters) committed in the same transaction as each batch.

    (A writer thread overlapping parse and write was measured and did not help: binding rows in
    ``executemany`` holds the GIL. See docs/implementation-plan.md, M1 status.)
    """

    def __init__(self, conn: sqlite3.Connection, batch_rows: int | None = None) -> None:
        self.conn = conn
        self.batch_rows = batch_rows or BATCH_ROWS
        self.rows: list[tuple[Any, ...]] = []
        self.fts: list[tuple[int, str, str | None, str]] = []
        self.tags: dict[str, int] = dict(conn.execute("SELECT name, id FROM tags").fetchall())
        self._templates: dict[str, int] = dict(conn.execute("SELECT text, id FROM templates").fetchall())
        self._new_templates: list[tuple[int, str]] = []
        self._new_tags: list[tuple[int, str]] = []
        self._template_counts: dict[int, int] = {}
        row = conn.execute("SELECT max(id) FROM templates").fetchone()
        self._next_template = (row[0] or 0) + 1
        row = conn.execute("SELECT max(id) FROM tags").fetchone()
        self._next_tag = (row[0] or 0) + 1
        self.on_flush: list[Callable[[], list[Statement]]] = []

    def template_id(self, text: str) -> int:
        tid = self._templates.get(text)
        if tid is None:
            tid = self._next_template
            self._next_template += 1
            self._templates[text] = tid
            self._new_templates.append((tid, text))
        counts = self._template_counts
        counts[tid] = counts.get(tid, 0) + 1
        return tid

    def tag_id(self, name: str) -> int:
        tid = self.tags.get(name)
        if tid is None:
            tid = self._next_tag
            self._next_tag += 1
            self.tags[name] = tid
            self._new_tags.append((tid, name))
        return tid

    def add(self, row: tuple[Any, ...], tag: str | None) -> None:
        """Row: (id, ts, ts_src, ts_raw, level, tag_id, pid, tid, msg, template_id, seg, attrs)."""
        self.rows.append(row)
        self.fts.append((row[0], row[8], tag, camel_parts(row[8], tag)))
        if len(self.rows) >= self.batch_rows:
            self.flush()

    def flush(self) -> None:
        extra: list[Statement] = []
        for fn in self.on_flush:
            extra.extend(fn())
        batch = (
            self._new_tags,
            self._new_templates,
            list(self._template_counts.items()),
            self.rows,
            self.fts,
            extra,
        )
        self._write(batch)
        self.discard()

    def discard(self) -> None:
        self.rows, self.fts = [], []
        self._new_tags, self._new_templates, self._template_counts = [], [], {}

    def _write(self, batch: tuple[Any, ...]) -> None:
        new_tags, new_templates, counts, rows, fts, extra = batch
        conn = self.conn
        with conn:
            if new_tags:
                conn.executemany("INSERT INTO tags(id, name) VALUES (?, ?)", new_tags)
            if new_templates:
                conn.executemany("INSERT INTO templates(id, text, count) VALUES (?, ?, 0)", new_templates)
            if counts:
                conn.executemany(
                    "UPDATE templates SET count = count + ? WHERE id = ?", [(n, t) for t, n in counts]
                )
            if rows:
                conn.executemany(
                    "INSERT INTO records(id, ts, ts_src, ts_raw, level, tag_id, pid, tid, msg, template_id, "
                    "seg, attrs) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    rows,
                )
                conn.executemany("INSERT INTO records_fts(rowid, msg, tag, parts) VALUES (?,?,?,?)", fts)
            for sql, params in extra:
                conn.execute(sql, params)


def delete_sources(conn: sqlite3.Connection, source_ids: list[int]) -> None:
    """Remove sources with their records, FTS rows and segments (one transaction)."""
    from ..model import SEQ_BITS

    with conn:
        for sid in source_ids:
            lo, hi = sid << SEQ_BITS, ((sid + 1) << SEQ_BITS) - 1
            # External-content FTS: delete index entries while the content rows still exist.
            conn.execute(
                "INSERT INTO records_fts(records_fts, rowid, msg, tag, parts) "
                "SELECT 'delete', id, msg, tag, parts FROM records_fts_v WHERE id BETWEEN ? AND ?",
                (lo, hi),
            )
            rows = conn.execute(
                "SELECT template_id, count(*) FROM records WHERE id BETWEEN ? AND ? GROUP BY template_id",
                (lo, hi),
            ).fetchall()
            conn.executemany("UPDATE templates SET count = count - ? WHERE id = ?", [(n, t) for t, n in rows])
            conn.execute("DELETE FROM records WHERE id BETWEEN ? AND ?", (lo, hi))
            conn.execute("DELETE FROM segments WHERE source_id = ?", (sid,))
            delete_artifacts(conn, "source_id = ?", (sid,))
            conn.execute("DELETE FROM sources WHERE id = ?", (sid,))
        conn.execute("DELETE FROM templates WHERE count <= 0")


ARTIFACT_COLS = (
    "id, source_id, record_id, type, name, path, sha256, member, ts, ts_raw, pid, tid, process, "
    "signature, headline, summary, fields, lines"
)


def insert_artifacts(
    conn: sqlite3.Connection,
    rows: list[tuple[Any, ...]],
    sections: list[tuple[int, int, str, int, int, str]],
) -> None:
    """Artifact rows (``ARTIFACT_COLS``) and their sections ``(artifact_id, idx, name, first_line,
    lines, text)``, with FTS rows; the caller holds the transaction."""
    conn.executemany(
        f"INSERT INTO artifacts({ARTIFACT_COLS}) VALUES ({','.join('?' * len(ARTIFACT_COLS.split(',')))})",
        rows,
    )
    for sec in sections:
        cur = conn.execute(
            "INSERT INTO artifact_sections(artifact_id, idx, name, first_line, lines, text) "
            "VALUES (?,?,?,?,?,?)",
            sec,
        )
        conn.execute("INSERT INTO artifact_fts(rowid, text) VALUES (?, ?)", (cur.lastrowid, sec[5]))


def delete_artifacts(conn: sqlite3.Connection, where: str, params: tuple[Any, ...]) -> None:
    """Remove artifacts matching ``where`` (on table artifacts) with sections and FTS rows."""
    ids = f"SELECT id FROM artifacts WHERE {where}"
    conn.execute(
        "INSERT INTO artifact_fts(artifact_fts, rowid, text) "
        f"SELECT 'delete', id, text FROM artifact_sections WHERE artifact_id IN ({ids})",
        params,
    )
    conn.execute(f"DELETE FROM artifact_sections WHERE artifact_id IN ({ids})", params)
    conn.execute(f"DELETE FROM artifacts WHERE {where}", params)
