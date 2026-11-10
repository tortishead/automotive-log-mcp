"""Words inside CamelCase identifiers are searchable (schema v3), also in migrated case DBs."""

from __future__ import annotations

from pathlib import Path

import pytest

from automotive_log_mcp.app import App
from automotive_log_mcp.core.words import camel_parts
from automotive_log_mcp.store import query
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
