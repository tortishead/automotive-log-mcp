"""Words inside CamelCase identifiers are searchable (schema v3), also in migrated case DBs."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from automotive_log_mcp.app import App
from automotive_log_mcp.core.words import camel_parts
from automotive_log_mcp.store import db, query
from automotive_log_mcp.tools import Output, Tools

LOG = """\
01-01 00:00:01.000  1000  1234  1250 I EsoCarDelegateDisplayAreaPolicyProvider: onDisplayAreaAppeared id=3
01-01 00:00:02.000  1000  1234  1250 W CarService: HTTPServer not ready
01-01 00:00:03.000  1000  1234  1250 I Plain: nothing camel here
"""


def total(out: Output) -> int:
    return int(out.data.model_dump()["total"])


@pytest.fixture
def tools(app: App, tmp_path: Path) -> Tools:
    p = tmp_path / "logcat.txt"
    p.write_text(LOG, encoding="utf-8")
    app.ingestor.add(app.config.case_db("c"), str(p))
    return Tools(app)


def test_camel_parts() -> None:
    assert camel_parts("HTTPServer x", None) == "http server"
    assert camel_parts("plain words, snake_case 0x1F", "Tag") == ""
    assert camel_parts(None, "EsoCarDelegate") == "eso car delegate"
    assert camel_parts("snake_caseFoo x.BarBaz", None) == "case foo bar baz"


def test_query_rewrite() -> None:
    assert query.fts_query("DisplayArea") == '(DisplayArea OR "display area")'
    assert query.fts_query("DisplayArea*") == '(DisplayArea* OR "display area"*)'
    assert query.fts_query("CarService:") == '(CarService OR "car service")'
    assert query.fts_query("eso delegate") == "eso delegate"
    assert query.fts_query('"Display Area"') == '"Display Area"'  # quoted: as written


@pytest.mark.parametrize(
    "q",
    [
        "Delegate",
        "eso",
        "DisplayArea",
        "DisplayArea*",
        "CarDelegate",
        '"display area"',
        "PolicyProvider",
        "Appeared",
        "server",
        "DisplayArea AND id",
    ],
)
def test_parts_of_identifiers_match(tools: Tools, q: str) -> None:
    assert total(tools.search("c", q)) >= 1, q


def test_whole_words_still_match_and_plain_text_is_unchanged(tools: Tools) -> None:
    assert total(tools.search("c", "EsoCarDelegateDisplayAreaPolicyProvider")) == 1
    assert total(tools.search("c", "camel")) == 1
    assert total(tools.search("c", "Delegate")) == 1


def test_v2_case_db_is_migrated_and_reindexed(app: App, tools: Tools) -> None:
    path = app.config.case_db("c")
    # Turn the case DB back into schema v2: index without the parts column.
    conn = sqlite3.connect(path)
    conn.executescript(
        "DROP TABLE records_fts; DROP VIEW records_fts_v;"
        "CREATE VIRTUAL TABLE records_fts USING fts5(msg, tag, content='records_v', content_rowid='id',"
        " columnsize=0);"
        "INSERT INTO records_fts(records_fts) VALUES ('rebuild');"
        "UPDATE meta SET value = '2' WHERE key = 'schema_version';"
    )
    n = conn.execute("SELECT count(*) FROM records_fts WHERE records_fts MATCH 'delegate'").fetchone()[0]
    conn.close()
    assert n == 0
    assert total(Tools(app).search("c", "Delegate")) == 1
    with db.opened(path) as c2:
        c2.execute("INSERT INTO records_fts(records_fts) VALUES ('integrity-check')")
        ver = c2.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
    assert ver == str(db.SCHEMA_VERSION)
