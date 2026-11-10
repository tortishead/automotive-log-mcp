"""Record messages in compressed blocks (schema 4), instead of a ``msg`` column.

The ``records`` table keeps the parsed fields; the (redacted) message text of a source's records
``first_seq .. first_seq + n - 1`` is one ``msg_blocks`` row: the messages joined with NUL and
compressed with zlib. A block holds about ``BLOCK_BYTES`` of text, so a lookup decompresses at
most that much, and records of one source are read in seq order, so a small cache of decoded
blocks serves nearly every lookup.

SQL reads a message with ``rmsg(id)``, a function every case connection registers
(:func:`attach`); ``records_v.msg`` is ``rmsg(r.id)``, so queries read it like a column.

While a file is indexed the last block of a source grows: each batch rewrites it with the
records added since. A reader that finds a seq beyond the block it cached reloads the block (and
the block list), so reads during an import see every committed record.

NUL in a message is stored as U+FFFD (the separator cannot occur in the text).
"""

from __future__ import annotations

import sqlite3
import zlib
from bisect import bisect_right
from collections import OrderedDict
from typing import Any

from ..model import SEQ_BITS, SEQ_MASK

BLOCK_BYTES = 64 * 1024  # text per block before compression
LEVEL = 6
CACHE_BLOCKS = 64
SEP = "\x00"

SCHEMA = """
CREATE TABLE IF NOT EXISTS msg_blocks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,  -- never reused: a cache key across deletes
    source_id INTEGER NOT NULL,
    first_seq INTEGER NOT NULL,
    n         INTEGER NOT NULL,                   -- messages in the block
    data      BLOB NOT NULL                       -- zlib(messages joined with NUL)
);
CREATE UNIQUE INDEX IF NOT EXISTS msg_blocks_source ON msg_blocks(source_id, first_seq);
"""


def encode(msgs: list[str]) -> bytes:
    return zlib.compress(SEP.join(msgs).encode("utf-8"), LEVEL)


def decode(data: bytes) -> list[str]:
    return zlib.decompress(data).decode("utf-8").split(SEP)


def clean(msg: str) -> str:
    return msg.replace(SEP, "�") if SEP in msg else msg


class BlockReader:
    """Message lookup by record id for one connection (``rmsg``)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._index: dict[int, tuple[list[int], list[int]]] = {}  # source -> (first seqs, block ids)
        self._cache: OrderedDict[int, list[str]] = OrderedDict()
        # The block of the last lookup as a record id range: scans read in id order, so nearly
        # every lookup is one comparison and an index (no shift, dict or bisect).
        self._lo = self._hi = 0
        self._msgs: list[str] = []

    def clear(self) -> None:
        self._index.clear()
        self._cache.clear()
        self._lo = self._hi = 0
        self._msgs = []

    def _blocks(self, sid: int, reload: bool = False) -> tuple[list[int], list[int]]:
        got = None if reload else self._index.get(sid)
        if got is None:
            rows = self.conn.execute(
                "SELECT first_seq, id FROM msg_blocks WHERE source_id = ? ORDER BY first_seq", (sid,)
            ).fetchall()
            got = ([r[0] for r in rows], [r[1] for r in rows])
            self._index[sid] = got
        return got

    def _block(self, bid: int, reload: bool = False) -> list[str]:
        msgs = None if reload else self._cache.get(bid)
        if msgs is not None:
            self._cache.move_to_end(bid)
            return msgs
        row = self.conn.execute("SELECT data FROM msg_blocks WHERE id = ?", (bid,)).fetchone()
        msgs = decode(row[0]) if row else []
        self._cache[bid] = msgs
        if len(self._cache) > CACHE_BLOCKS:
            self._cache.popitem(last=False)
        return msgs

    def msg(self, rid: int | None) -> str | None:
        if rid is not None and self._lo <= rid < self._hi:
            return self._msgs[rid - self._lo]
        return self._lookup(rid)

    def _lookup(self, rid: int | None) -> str | None:
        if rid is None:
            return None
        sid, seq = rid >> SEQ_BITS, rid & SEQ_MASK
        for reload in (False, True):
            firsts, ids = self._blocks(sid, reload)
            i = bisect_right(firsts, seq) - 1
            if i < 0:
                continue
            k = seq - firsts[i]
            msgs = self._block(ids[i], reload)
            if k < len(msgs):
                self._lo = rid - k
                self._hi = self._lo + len(msgs)
                self._msgs = msgs
                return msgs[k]
        return None


class BlockWriter:
    """Collects messages per source into blocks; :meth:`statements` writes what changed."""

    def __init__(self) -> None:
        # source -> [block id or None, first seq, messages, text bytes, changed]
        self._open: dict[int, list[Any]] = {}
        self._done: list[tuple[int | None, int, int, list[str]]] = []  # full blocks to write

    def add(self, rid: int, msg: str) -> None:
        sid, seq = rid >> SEQ_BITS, rid & SEQ_MASK
        st = self._open.get(sid)
        if st is not None and seq != st[1] + len(st[2]):
            self._finish(sid)  # not the next seq (never in an import): start a new block
            st = None
        if st is None:
            st = self._open[sid] = [None, seq, [], 0, False]
        st[2].append(clean(msg))
        st[3] += len(msg) + 1
        st[4] = True
        if st[3] >= BLOCK_BYTES:
            self._finish(sid)

    def _finish(self, sid: int) -> None:
        st = self._open.pop(sid)
        self._done.append((st[0], sid, st[1], st[2]))

    def write(self, conn: sqlite3.Connection) -> None:
        """Write full blocks and the changed open ones (inside the caller's transaction)."""
        for bid, sid, first, msgs in self._done:
            _put(conn, bid, sid, first, msgs)
        self._done = []
        for sid, st in self._open.items():
            if st[4]:
                st[0] = _put(conn, st[0], sid, st[1], st[2])
                st[4] = False

    def discard(self) -> None:
        self._open.clear()
        self._done = []


def _put(conn: sqlite3.Connection, bid: int | None, sid: int, first: int, msgs: list[str]) -> int:
    data = encode(msgs)
    if bid is None:
        cur = conn.execute(
            "INSERT INTO msg_blocks(source_id, first_seq, n, data) VALUES (?, ?, ?, ?)",
            (sid, first, len(msgs), data),
        )
        return int(cur.lastrowid or 0)
    conn.execute("UPDATE msg_blocks SET n = ?, data = ? WHERE id = ?", (len(msgs), data, bid))
    return bid


def attach(conn: sqlite3.Connection) -> BlockReader:
    """Register ``rmsg(id)`` on ``conn`` and return its reader."""
    reader = BlockReader(conn)
    conn.create_function("rmsg", 1, reader.msg, deterministic=True)
    return reader


def readers_clear(conn: sqlite3.Connection) -> None:
    reader = getattr(conn, "blocks", None)
    if isinstance(reader, BlockReader):
        reader.clear()


def migrate_v4(conn: sqlite3.Connection) -> None:
    """Schema 3 -> 4 (inside the caller's transaction): messages into blocks, ``records`` without
    ``msg``, and only the time and tag indexes. The text index is unchanged (same text)."""
    for stmt in SCHEMA.split(";"):
        if stmt.strip():
            conn.execute(stmt)
    if any(r[1] == "msg" for r in conn.execute("PRAGMA table_info(records)")):
        writer = BlockWriter()
        rows = conn.execute("SELECT id, msg FROM records ORDER BY id")  # streamed, not loaded
        for n, (rid, msg) in enumerate(rows, start=1):
            writer.add(rid, msg)
            if n % 200_000 == 0:
                writer.write(conn)
        writer.write(conn)
    for stmt in V4_RECORDS.split(";"):
        if stmt.strip():
            conn.execute(stmt)


# records rebuilt without msg; views read the message through rmsg(). Kept indexes: time (all
# time-ordered reads), per-source monotonic time, and tag (tag filters and counts).
# Dropped (schema <= 3): pid, template, level; their filters and counts scan the rows the time or
# tag index selected, or the whole table.
V4_RECORDS = """
DROP VIEW IF EXISTS records_fts_v;
DROP VIEW IF EXISTS records_v;
DROP INDEX IF EXISTS records_pid;
DROP INDEX IF EXISTS records_template;
DROP INDEX IF EXISTS records_level;
DROP INDEX IF EXISTS records_tag;
DROP INDEX IF EXISTS records_ts;
DROP INDEX IF EXISTS records_src_ts;
CREATE TABLE records_v4 (
    id          INTEGER PRIMARY KEY,
    ts          INTEGER,
    ts_src      INTEGER,
    ts_raw      TEXT,
    level       TEXT,
    tag_id      INTEGER,
    pid         INTEGER,
    tid         INTEGER,
    template_id INTEGER,
    seg         INTEGER,
    attrs       TEXT,
    source_id   INTEGER GENERATED ALWAYS AS (id >> 40) VIRTUAL,
    seq         INTEGER GENERATED ALWAYS AS (id & 1099511627775) VIRTUAL
);
INSERT INTO records_v4(id, ts, ts_src, ts_raw, level, tag_id, pid, tid, template_id, seg, attrs)
    SELECT id, ts, ts_src, ts_raw, level, tag_id, pid, tid, template_id, seg, attrs FROM records;
DROP TABLE records;
ALTER TABLE records_v4 RENAME TO records;
CREATE INDEX records_ts ON records(ts);
CREATE INDEX records_src_ts ON records(source_id, ts_src) WHERE ts_src IS NOT NULL;
CREATE INDEX records_tag ON records(tag_id);
CREATE VIEW records_v AS
    SELECT r.id, r.source_id, r.seq, r.ts, coalesce(r.ts_src, r.ts) AS ts_src, r.ts_raw, r.level,
           t.name AS tag, r.pid, r.tid, rmsg(r.id) AS msg, r.template_id, r.seg, r.attrs
    FROM records r LEFT JOIN tags t ON t.id = r.tag_id;
CREATE VIEW records_fts_v AS SELECT id, msg, tag, camel_parts(msg, tag) AS parts FROM records_v
"""
