"""A sources filter names what it leaves out; an empty MCP query lists what passes the filter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
from mcp import Client

from automotive_log_mcp.app import App
from automotive_log_mcp.config import Config
from automotive_log_mcp.policy import Policy
from automotive_log_mcp.server import build_server
from automotive_log_mcp.tools import Filter, Tools

from .test_golden import make_bugreport_zip


def F(**kw: Any) -> Filter:
    return Filter.model_validate(kw)


def _br(app: App, tmp_path: Path) -> Tools:
    app.ingestor.add(app.config.case_db("br"), str(make_bugreport_zip(tmp_path)))
    return Tools(app)


def _source_with(tools: Tools, word: str) -> tuple[str, int]:
    """(source id of the first hit, total hits in the case)."""
    out = tools.search("br", word, collapse=False)
    d = out.data.model_dump()
    return d["hits"][0]["cite"].split("#")[0].removeprefix("log:"), d["total"]


def test_sources_filter_reports_matches_elsewhere(app: App, tmp_path: Path) -> None:
    tools = _br(app, tmp_path)
    sid, total = _source_with(tools, "hvac")
    out = tools.search("br", "hvac", filter=F(sources=[sid]))
    shown = out.data.model_dump()["total"]
    assert shown < total
    assert f"Outside the sources filter: {total - shown} more match" in out.text
    counted = tools.search("br", "hvac", filter=F(sources=[sid]), count_only=True)
    assert "Outside the sources filter" in counted.text
    # Without a sources filter: no such line.
    assert "Outside the sources filter" not in tools.search("br", "hvac").text


def test_sources_filter_regex_names_unsearched_sources(app: App, tmp_path: Path) -> None:
    tools = _br(app, tmp_path)
    sid, _ = _source_with(tools, "hvac")
    out = tools.search("br", r"[Hh]vac", mode="regex", filter=F(sources=[sid]))
    assert "Not searched (sources filter): log:" in out.text


def test_empty_query_over_mcp_lists_a_tag(config: Config, tmp_path: Path) -> None:
    zpath = make_bugreport_zip(tmp_path)

    async def main() -> None:
        async with Client(build_server(config, Policy())) as client:
            await client.call_tool("add_source", {"case": "br", "path": str(zpath)})
            for q in ({}, {"query": ""}, {"query": "*"}):
                r = await client.call_tool(
                    "search", {"case": "br", "filter": {"tags": ["HvacCtrl"]}, "collapse": False, **q}
                )
                assert not r.is_error, r.content
                text = r.content[0].text  # type: ignore[union-attr]
                lines = [ln for ln in text.splitlines() if ln.startswith("log:")]
                assert lines and all("HvacCtrl" in ln for ln in lines), text

    anyio.run(main)
