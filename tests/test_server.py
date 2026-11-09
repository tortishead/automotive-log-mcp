from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client

from automotive_log_mcp.config import Config, ConfigError
from automotive_log_mcp.policy import Policy
from automotive_log_mcp.server import TOOL_NAMES, build_server

P0_TOOLS = set(TOOL_NAMES)


def run(fn: Any) -> Any:
    return anyio.run(fn)


def calllog_lines(config: Config) -> list[dict[str, Any]]:
    assert config.call_log is not None
    return [json.loads(line) for line in config.call_log.read_text(encoding="utf-8").splitlines()]


def test_lists_all_p0_tools_with_annotations(config: Config) -> None:
    async def main() -> None:
        async with Client(build_server(config, Policy())) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert set(tools) == P0_TOOLS
            for name, tool in tools.items():
                assert tool.description, name
                assert tool.annotations is not None
                assert tool.annotations.read_only_hint is (name != "add_source"), name
                assert tool.annotations.destructive_hint is False
                assert "case" in tool.input_schema["required"]
            search = tools["search"].input_schema
            assert search["properties"]["mode"]["enum"] == ["text", "regex"]
            assert "from" in search["$defs"]["Filter"]["properties"]
            assert tools["list_sources"].output_schema is not None

            prompts = (await client.list_prompts()).prompts
            assert [p.name for p in prompts] == ["defect_triage"]

    run(main)


def test_list_sources_on_empty_case(config: Config) -> None:
    async def main() -> None:
        async with Client(build_server(config, Policy())) as client:
            result = await client.call_tool("list_sources", {"case": "TICKET-1"})
            assert not result.is_error
            assert result.structured_content == {"case": "TICKET-1", "exists": False, "sources": []}
            assert "No sources" in result.content[0].text  # type: ignore[union-attr]

    run(main)
    (line,) = calllog_lines(config)
    assert line["tool"] == "list_sources"
    assert line["args"] == {"case": "TICKET-1"}
    assert line["status"] == "ok"
    assert line["bytes_out"] > 0
    assert "No sources" not in json.dumps(line)  # no tool output in the call log


def test_tool_errors_are_clear_and_logged(config: Config) -> None:
    async def main() -> None:
        async with Client(build_server(config, Policy())) as client:
            result = await client.call_tool(
                "search", {"case": "c1", "query": "crash", "filter": {"from": "-5m", "level": "E"}}
            )
            assert result.is_error
            assert "case 'c1' does not exist" in result.content[0].text  # type: ignore[union-attr]

    run(main)
    (line,) = calllog_lines(config)
    assert line["status"] == "error"
    assert line["args"]["filter"] == {"from": "-5m", "level": "E"}


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("list_sources", {"case": "../etc"}),
        ("context", {"case": "c1", "at": "log:2"}),
        ("get_records", {"case": "c1", "ids": []}),
        ("search", {"case": "c1", "query": "x", "filter": {"unknown": 1}}),
    ],
)
def test_invalid_params_fail_loudly(config: Config, tool: str, args: dict[str, Any]) -> None:
    async def main() -> None:
        async with Client(build_server(config, Policy())) as client:
            result = await client.call_tool(tool, args)
            assert result.is_error

    run(main)


def test_policy_hides_tools(config: Config) -> None:
    policy = Policy(allowed_tools=frozenset({"list_sources", "search"}))

    async def main() -> None:
        async with Client(build_server(config, policy)) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert names == {"list_sources", "search"}

    run(main)


def test_policy_unknown_tool_is_an_error(config: Config) -> None:
    with pytest.raises(ConfigError, match="srch"):
        build_server(config, Policy(allowed_tools=frozenset({"srch"})))


def test_byte_budget_refuses_after_limit(config: Config) -> None:
    async def main() -> None:
        async with Client(build_server(config, Policy(byte_budget=10))) as client:
            first = await client.call_tool("list_sources", {"case": "c1"})
            assert not first.is_error
            second = await client.call_tool("list_sources", {"case": "c1"})
            assert second.is_error
            assert "budget" in second.content[0].text  # type: ignore[union-attr]

    run(main)
    assert [line["status"] for line in calllog_lines(config)] == ["ok", "refused"]


def test_call_log_can_be_disabled(home: Path) -> None:
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("call_log: null\n", encoding="utf-8")
    from automotive_log_mcp.config import load_config

    config = load_config()
    assert config.call_log is None

    async def main() -> None:
        async with Client(build_server(config, Policy())) as client:
            await client.call_tool("list_sources", {"case": "c1"})

    run(main)
    assert not (home / "calllog.jsonl").exists()


def test_defect_triage_prompt(config: Config) -> None:
    async def main() -> None:
        async with Client(build_server(config, Policy())) as client:
            result = await client.get_prompt("defect_triage", {"case": "c1", "symptom": "HVAC freezes"})
            text = result.messages[0].content.text  # type: ignore[union-attr]
            assert "c1" in text
            assert "HVAC freezes" in text

    run(main)
