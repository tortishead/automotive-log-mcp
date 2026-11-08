"""Opening case DBs. M0 only checks that the SQLite build has FTS5; the schema arrives in M1."""

from __future__ import annotations

import sqlite3


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
