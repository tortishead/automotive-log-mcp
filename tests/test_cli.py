"""The CLI: every tool as a command, built from its input schema, called through the MCP tool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from automotive_log_mcp.cli import main
from automotive_log_mcp.config import load_config
from automotive_log_mcp.policy import Policy
from automotive_log_mcp.server import TOOL_NAMES, LogServer
from automotive_log_mcp.toolcli import tool_parser

from .conftest import FIXED_SECRET, copy_fixture

BUGREPORT = "bugreport/bugreport-sdk_car-UAA1.250512.001-2026-01-01-00-10-00.txt"


@pytest.fixture
def case(home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> str:
    home.mkdir(parents=True, exist_ok=True)
    (home / "secret.key").write_bytes(FIXED_SECRET)
    path = copy_fixture(BUGREPORT, tmp_path / "logs")
    assert main(["add_source", "-q", "br", str(path)]) == 0
    assert "Indexed" in capsys.readouterr().out
    return "br"


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_every_tool_has_a_command(home: Path) -> None:
    server = LogServer(load_config(), Policy())
    for name in TOOL_NAMES:
        parser, spec = tool_parser(server, name)
        assert parser.prog.endswith(name)
        assert spec, name


def test_search_with_filter_options(case: str, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "search", case, "failed", "--level", "E", "--tag", "Hvac*", "--limit", "5")
    assert code == 0
    assert "log:1#2" in out and "setProperty failed" in out


def test_dashed_name_and_json(case: str, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "list-sources", case, "--json")
    assert code == 0
    data = json.loads(out)
    assert data["case"] == case and len(data["sources"]) == 7


def test_nullable_enum_and_bucket(case: str, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "count", case, "--by", "none", "--bucket", "1m", "--json")
    assert code == 0
    data = json.loads(out)
    assert "by" not in data and data["groups"] == [] and len(data["buckets"]) == 9


def test_positional_list_and_artifact(case: str, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "get_records", case, "log:1#2", "log:3#1")
    assert code == 0 and "log:1#2" in out and "log:3#1" in out
    code, out, _ = run(capsys, "read_artifact", case, "art:1/exception")
    assert code == 0 and "NullPointerException" in out


def test_attrs_key_value(case: str, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "search", case, "--attrs", "art=1", "--json")
    assert code == 0
    assert [h["cite"] for h in json.loads(out)["hits"]] == ["log:3#1"]


def test_errors(case: str, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "search", "nosuch", "x")
    assert code == 1 and "case 'nosuch' does not exist" in err and "Error executing" not in err
    code, _, err = run(capsys, "context", case, "bogus")
    assert code == 1 and "should match pattern" in err and "pydantic.dev" not in err
    code, _, err = run(capsys, "serach", case)
    assert code == 2 and "unknown command 'serach'" in err
    with pytest.raises(SystemExit) as exc:
        main(["--structured", "search", case])
    assert exc.value.code == 2


def test_policy_turns_off_a_command(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "policy.yaml").write_text("allowed_tools: [list_sources]\n", encoding="utf-8")
    code, _, err = run(capsys, "search", "br", "x")
    assert code == 2 and "turned off by the policy" in err
    code, out, _ = run(capsys, "list_sources", "br")
    assert code == 0 and "No sources" in out


def test_cli_calls_are_in_the_call_log(case: str, home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "overview", case)
    lines = [json.loads(x) for x in (home / "calllog.jsonl").read_text(encoding="utf-8").splitlines()]
    calls = [x for x in lines if x.get("tool") == "overview"]
    assert calls and calls[-1]["session"].startswith("cli-")
