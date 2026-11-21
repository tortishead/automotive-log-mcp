"""Automatic cleanup of the home directory, run once when the server starts.

Three rules, each set under ``retention`` in ``config.yaml`` and turned off with ``null``:

- ``case_days``: a case DB not used for this many days is deleted, with its ``-wal`` and
  ``-shm`` files. Every open of a case by a tool or an import touches the DB file
  (``store/db.py``), so its mtime is the last use. A case is kept when one of its original log
  files is gone (the DB is then the only copy of those logs), when it has a source still
  indexing, or when it cannot be read (another version, or not a case DB).
- ``rounds_days``: round files (``rounds_dir``) older than this are deleted.
- ``call_log_mb``: a call log above this size is renamed to ``<name>.1``, replacing the previous
  one, so at most about twice the limit is kept.

Errors never stop the server: a file that cannot be read or removed is left alone.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from .config import Config

DAY = 86400.0
DB_SIDE_FILES = ("-wal", "-shm")


def cleanup(config: Config, now: float | None = None) -> list[str]:
    """Apply the retention rules; returns one line per file removed or rotated."""
    now = time.time() if now is None else now
    r = config.retention
    done: list[str] = []
    if r.case_days is not None:
        done += _old_cases(config.cases_dir, now - r.case_days * DAY, r.case_days)
    if r.rounds_days is not None and config.rounds_dir is not None:
        done += _old_rounds(config.rounds_dir, now - r.rounds_days * DAY, r.rounds_days)
    if r.call_log_mb is not None and config.call_log is not None:
        done += _rotate(config.call_log, r.call_log_mb)
    return done


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _old_cases(cases_dir: Path, cutoff: float, days: int) -> list[str]:
    done: list[str] = []
    if not cases_dir.is_dir():
        return done
    for db in sorted(cases_dir.glob("*.db")):
        # Last use: the DB, or a WAL that is newer (written since the last checkpoint).
        times = [t for t in (_mtime(db), *(_mtime(_side(db, s)) for s in DB_SIDE_FILES)) if t]
        if not times or max(times) >= cutoff or not _deletable(db):
            continue
        try:
            for f in (db, *(_side(db, s) for s in DB_SIDE_FILES)):
                f.unlink(missing_ok=True)
        except OSError:
            continue  # open in another server (Windows), or no permission
        done.append(f"removed case '{db.stem}' (not used for {days} days)")
    return done


def _side(db: Path, suffix: str) -> Path:
    return db.with_name(db.name + suffix)


def _deletable(db: Path) -> bool:
    """Whether the case can be rebuilt from its logs: every original file still exists and no
    import is unfinished. Read-only, so the check does not count as a use."""
    try:
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
    except (sqlite3.Error, ValueError, OSError):
        return False
    try:
        rows = conn.execute("SELECT DISTINCT path, status FROM sources").fetchall()
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    return all(st != "indexing" and Path(p).exists() for p, st in rows)


def _old_rounds(rounds_dir: Path, cutoff: float, days: int) -> list[str]:
    if not rounds_dir.is_dir():
        return []
    n = 0
    for f in rounds_dir.iterdir():
        if f.suffix not in (".jsonl", ".txt") or not f.is_file():
            continue
        t = _mtime(f)
        if t is not None and t < cutoff:
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
    return [f"removed {n} round files older than {days} days"] if n else []


def _rotate(call_log: Path, mb: int) -> list[str]:
    try:
        size = call_log.stat().st_size
    except OSError:
        return []
    if size <= mb * 1_000_000:
        return []
    old = call_log.with_name(call_log.name + ".1")
    try:
        os.replace(call_log, old)
    except OSError:
        return []
    return [f"call log over {mb} MB: moved to {old.name}"]
