"""Schema 4: messages in compressed blocks, read back through rmsg(); migration from schema 2/3."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from automotive_log_mcp.app import App
from automotive_log_mcp.model import SEQ_BITS
from automotive_log_mcp.store import db, textblocks
from automotive_log_mcp.tools import Tools

from .conftest import copy_fixture


def _rid(sid: int, seq: int) -> int:
    return (sid << SEQ_BITS) | seq


def test_blocks_round_trip_and_grow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(textblocks, "BLOCK_BYTES", 40)
    conn = db.connect(tmp_path / "c.db", create=True)
    w = textblocks.BlockWriter()
    msgs = [f"message {i} with\nnewline" for i in range(10)] + ["nul\x00inside", ""]
    for i, m in enumerate(msgs[:5], start=1):
        w.add(_rid(1, i), m)
    with conn:
        w.write(conn)
    reader = textblocks.BlockReader(conn)
    assert reader.msg(_rid(1, 5)) == msgs[4]
    assert reader.msg(_rid(1, 6)) is None
    for i, m in enumerate(msgs[5:], start=6):  # the open block grows; the reader catches up
        w.add(_rid(1, i), m)
    with conn:
        w.write(conn)
    got = [reader.msg(_rid(1, i)) for i in range(1, len(msgs) + 1)]
    assert got == [m.replace("\x00", "�") for m in msgs]
    assert conn.execute("SELECT count(*) FROM msg_blocks").fetchone()[0] > 1
    assert conn.execute("SELECT rmsg(?)", (_rid(1, 3),)).fetchone()[0] == msgs[2]
    conn.close()


def test_records_have_no_msg_column_and_text_reads_back(app: App, tmp_path: Path) -> None:
    p = copy_fixture("logcat/threadtime.log", tmp_path)
    app.ingestor.add(app.config.case_db("c"), str(p))
    with db.opened(app.config.case_db("c")) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(records)")]
        assert "msg" not in cols
        idx = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='records'")
        }
        assert idx == {"records_ts", "records_src_ts", "records_tag"}
        n = conn.execute("SELECT count(*) FROM records").fetchone()[0]
        msgs = [m for (m,) in conn.execute("SELECT msg FROM records_v")]
        assert len(msgs) == n and all(m is not None for m in msgs)
        conn.execute("INSERT INTO records_fts(records_fts) VALUES ('integrity-check')")


def test_reindex_replaces_blocks(app: App, tmp_path: Path) -> None:
    p = copy_fixture("logcat/threadtime.log", tmp_path)
    path = app.config.case_db("c")
    app.ingestor.add(path, str(p))
    with db.opened(path) as conn:
        before = conn.execute("SELECT count(*) FROM msg_blocks").fetchone()[0]
    app.ingestor.add(path, str(p), force=True)
    with db.opened(path) as conn:
        assert conn.execute("SELECT count(*) FROM msg_blocks").fetchone()[0] == before
        conn.execute("INSERT INTO records_fts(records_fts) VALUES ('integrity-check')")
    assert Tools(app).search("c", "HvacCtrl").data.model_dump()["total"] == 4


def _schema2_db(path: Path) -> None:
    """A case DB as schema 2 wrote it: msg column, five record indexes, a two-column text index."""
    conn = sqlite3.connect(path)
    conn.executescript(db._schema_sql("schema.sql") + db._schema_sql("schema_v2.sql"))
    conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '2')")
    conn.execute(
        "INSERT INTO sources(id, name, format, connector_version, redaction_hash, path, sha256, size, "
        "status, records, added_at) VALUES (1, 'x.log', 'logcat', '2', 'h', 'x.log', 'abc', 1, 'done', 3, 't')"
    )
    conn.execute(
        "INSERT INTO tags(id, name) VALUES (1, 'EsoCarDelegateDisplayAreaPolicyProvider'), (2, 'Hvac')"
    )
    conn.executemany(
        "INSERT INTO records(id, ts, level, tag_id, pid, tid, msg) VALUES (?, ?, ?, ?, 1, 1, ?)",
        [
            (_rid(1, 1), 1, "I", 1, "onDisplayAreaAppeared id=3"),
            (_rid(1, 2), 2, "E", 2, "setProperty failed\nsecond line"),
            (_rid(1, 3), 3, "W", 2, "plain words"),
        ],
    )
    conn.execute("INSERT INTO records_fts(records_fts) VALUES ('rebuild')")
    conn.commit()
    conn.close()


def test_schema2_db_migrates_to_blocks(app: App) -> None:
    path = app.config.case_db("old")
    path.parent.mkdir(parents=True, exist_ok=True)
    _schema2_db(path)
    with db.opened(path) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(
            db.SCHEMA_VERSION
        )
        assert "msg" not in [r[1] for r in conn.execute("PRAGMA table_info(records)")]
        msgs = [m for (m,) in conn.execute("SELECT msg FROM records_v ORDER BY id")]
        assert msgs == ["onDisplayAreaAppeared id=3", "setProperty failed\nsecond line", "plain words"]
        conn.execute("INSERT INTO records_fts(records_fts) VALUES ('integrity-check')")
    tools = Tools(app)
    assert tools.search("old", "Delegate").data.model_dump()["total"] == 1  # schema 3 parts, rebuilt
    assert tools.search("old", "second line").data.model_dump()["total"] == 1
    assert tools.search("old", r"failed\nsecond", mode="regex").data.model_dump()["total"] == 1
