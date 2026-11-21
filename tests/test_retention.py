"""Automatic cleanup at server start: old cases, old round files, a large call log."""

from __future__ import annotations

import dataclasses
import os
import sqlite3
import time
from pathlib import Path

import pytest

from automotive_log_mcp.app import App
from automotive_log_mcp.config import ConfigError, Retention, load_config
from automotive_log_mcp.retention import DAY, cleanup
from automotive_log_mcp.store import db

from .conftest import copy_fixture

LOG = "logcat/threadtime.log"


def age(path: Path, days: float) -> None:
    t = time.time() - days * DAY
    os.utime(path, (t, t))


def make_case(app: App, tmp_path: Path, case: str) -> tuple[Path, Path]:
    """A case with one indexed log; returns (case DB, original log)."""
    log = copy_fixture(LOG, tmp_path / f"logs_{case}")
    app.ingestor.add(app.config.case_db(case), str(log))
    return app.config.case_db(case), log


def test_unused_case_is_removed_with_its_side_files(app: App, tmp_path: Path) -> None:
    dbf, _ = make_case(app, tmp_path, "old")
    wal = dbf.with_name(dbf.name + "-wal")
    wal.write_bytes(b"")
    for f in (dbf, wal):
        age(f, 31)
    assert cleanup(app.config) == ["removed case 'old' (not used for 30 days)"]
    assert not dbf.exists() and not wal.exists()


def test_recent_case_is_kept_and_opening_it_counts_as_use(app: App, tmp_path: Path) -> None:
    dbf, _ = make_case(app, tmp_path, "c")
    age(dbf, 31)
    app.list_sources("c")  # a tool reads the case
    assert cleanup(app.config) == []
    assert dbf.exists()


def test_case_whose_original_is_gone_is_kept(app: App, tmp_path: Path) -> None:
    dbf, log = make_case(app, tmp_path, "c")
    log.unlink()
    age(dbf, 365)
    assert cleanup(app.config) == []
    assert dbf.exists()


def test_case_with_an_unfinished_import_is_kept(app: App, tmp_path: Path) -> None:
    dbf, _ = make_case(app, tmp_path, "c")
    with db.opened(dbf) as conn, conn:
        conn.execute("UPDATE sources SET status = 'indexing'")
    age(dbf, 365)
    assert cleanup(app.config) == []


def test_unreadable_db_is_kept(app: App) -> None:
    app.config.cases_dir.mkdir(parents=True, exist_ok=True)
    junk = app.config.cases_dir / "junk.db"
    junk.write_bytes(b"not a database")
    other = app.config.cases_dir / "other.db"
    sqlite3.connect(other).close()  # a SQLite file without a sources table
    for f in (junk, other):
        age(f, 365)
    assert cleanup(app.config) == []
    assert junk.exists() and other.exists()


def test_case_rule_can_be_turned_off_or_changed(app: App, tmp_path: Path) -> None:
    dbf, _ = make_case(app, tmp_path, "c")
    age(dbf, 100)
    off = dataclasses.replace(app.config, retention=Retention(case_days=None))
    assert cleanup(off) == []
    longer = dataclasses.replace(app.config, retention=Retention(case_days=200))
    assert cleanup(longer) == []
    assert dbf.exists()


def test_old_round_files_are_removed(app: App) -> None:
    rounds = app.config.rounds_dir
    assert rounds is not None
    rounds.mkdir(parents=True)
    old, new, other = rounds / "a_r1.jsonl", rounds / "b_r1.txt", rounds / "notes.md"
    for f in (old, new, other):
        f.write_text("x", encoding="utf-8")
    age(old, 40)
    age(other, 40)
    assert cleanup(app.config) == ["removed 1 round files older than 30 days"]
    assert not old.exists() and new.exists() and other.exists()


def test_large_call_log_is_rotated(app: App) -> None:
    log = app.config.call_log
    assert log is not None
    log.parent.mkdir(parents=True, exist_ok=True)
    small = dataclasses.replace(app.config, retention=Retention(call_log_mb=1))
    log.write_bytes(b"x" * 1000)
    assert cleanup(small) == []
    log.write_bytes(b"x" * 1_000_001)
    assert cleanup(small) == ["call log over 1 MB: moved to calllog.jsonl.1"]
    assert not log.exists() and log.with_name("calllog.jsonl.1").stat().st_size == 1_000_001


def test_retention_config(home: Path) -> None:
    assert load_config().retention == Retention(30, 30, 20)
    home.mkdir(parents=True)
    cfg = home / "config.yaml"
    cfg.write_text("retention: {case_days: 7, call_log_mb: null}\n", encoding="utf-8")
    assert load_config().retention == Retention(7, 30, None)
    cfg.write_text("retention: null\n", encoding="utf-8")
    assert load_config().retention == Retention(None, None, None)


@pytest.mark.parametrize(
    "text",
    [
        "retention: 5\n",
        "retention: {days: 5}\n",
        "retention: {case_days: 0}\n",
        "retention: {case_days: x}\n",
    ],
)
def test_invalid_retention_config(home: Path, text: str) -> None:
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="retention"):
        load_config()


def test_server_start_reports_cleanup_on_stderr(
    app: App, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from automotive_log_mcp.cli import _cleanup

    dbf, _ = make_case(app, tmp_path, "old")
    age(dbf, 31)
    _cleanup(app.config)
    out, err = capsys.readouterr()
    assert out == ""  # stdout is the MCP stream
    assert err == "automotive-log-mcp: removed case 'old' (not used for 30 days)\n"
