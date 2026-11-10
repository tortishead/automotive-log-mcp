"""Token accounting: call-log fields, structured output off by default, slim schemas, and the
``calls`` analysis (with and without a Claude Code transcript)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client

from automotive_log_mcp import callreport as cr
from automotive_log_mcp.cli import main as cli_main
from automotive_log_mcp.config import Config, ConfigError, load_config
from automotive_log_mcp.policy import Policy
from automotive_log_mcp.server import build_server, slim_schema
from automotive_log_mcp.usage import CiteSet, args_hashes, compact_cites, est_tokens

from .conftest import FIXED_SECRET
from .test_golden import make_bugreport_zip


def calllog(config: Config) -> list[dict[str, Any]]:
    assert config.call_log is not None
    return [json.loads(x) for x in config.call_log.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def case_zip(config: Config, tmp_path: Path) -> Path:
    config.home.mkdir(parents=True, exist_ok=True)
    (config.home / "secret.key").write_bytes(FIXED_SECRET)
    return make_bugreport_zip(tmp_path)


# -- helpers ---------------------------------------------------------------------------------------------


def test_compact_cites_and_overlap() -> None:
    out = compact_cites([(3, 1), (3, 2), (3, 3), (3, 9), (1, 5)], [4, 2, 4])
    assert out == ["art:2", "art:4", "log:1#5", "log:3#1-3", "log:3#9"]
    cs = CiteSet.parse(out)
    assert cs.overlaps(CiteSet.from_text("see log:3#2 here"))
    assert cs.overlaps(CiteSet.from_text("art:4/backtrace:L17"))
    assert not cs.overlaps(CiteSet.from_text("log:3#4 and log:2#1 and art:5"))
    assert not CiteSet.from_text("no citations")


def test_args_hashes_exact_and_near() -> None:
    a = args_hashes("search", {"case": "T-1", "query": "fatal  error", "limit": 50, "cursor": None})
    b = args_hashes("search", {"case": "T-1", "query": "fatal error", "limit": 50})
    c = args_hashes("search", {"case": "T-1", "query": "fatal error", "limit": 10, "cursor": "x"})
    d = args_hashes("count", {"case": "T-1", "query": "fatal error"})
    assert a == b  # whitespace and None values do not matter
    assert a[0] != c[0] and a[1] == c[1]  # another page or limit: near-duplicate
    assert d[1] != a[1]  # another tool
    assert est_tokens(0) == 0 and est_tokens(1) == 1 and est_tokens(8) == 2


def test_slim_schema_keeps_meaning() -> None:
    schema = {
        "title": "searchArguments",
        "properties": {
            "title": {"type": "string", "title": "Title"},  # a property named 'title' stays
            "cursor": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Cursor"},
            "limit": {"default": 50, "type": "integer", "title": "Limit"},
        },
    }
    assert slim_schema(schema) == {
        "properties": {
            "title": {"type": "string"},
            "cursor": {"type": "string"},
            "limit": {"default": 50, "type": "integer"},
        }
    }


def test_structured_output_config(home: Path) -> None:
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("structured_output: true\n", encoding="utf-8")
    assert load_config().structured_output is True
    (home / "config.yaml").write_text("structured_output: 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="structured_output"):
        load_config()


# -- the server ------------------------------------------------------------------------------------------


def test_default_server_sends_text_only_and_logs_token_fields(config: Config, case_zip: Path) -> None:
    async def main() -> None:
        async with Client(build_server(config, Policy())) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert all(t.output_schema is None for t in tools.values())
            props = tools["search"].input_schema["properties"]
            assert "title" not in props["cursor"] and "anyOf" not in props["cursor"]
            r = await client.call_tool("add_source", {"case": "T-1", "path": str(case_zip)})
            assert not r.is_error
            r = await client.call_tool("list_artifacts", {"case": "T-1"})
            assert r.structured_content is None and "art:1" in r.content[0].text  # type: ignore[union-attr]
            # null for an optional parameter is still accepted (slim schemas only drop the anyOf)
            r = await client.call_tool("search", {"case": "T-1", "query": "failed", "cursor": None})
            assert not r.is_error
            await client.call_tool("search", {"case": "T-1", "query": "failed", "limit": 1})
            await client.call_tool("search", {"case": "T-1", "query": "failed"})
            r = await client.call_tool(
                "check_citations", {"case": "T-1", "text": "The HVAC app crashed (art:1, log:3#1)."}
            )
            assert not r.is_error

    anyio.run(main)
    log = calllog(config)
    assert [e["seq"] for e in log] == [1, 2, 3, 4, 5, 6]
    assert "server" in log[0] and all("server" not in e for e in log[1:])
    srv = log[0]["server"]
    assert srv["structured"] is False and srv["fixed"]["tools_model_bytes"] > 1000
    assert set(srv["fixed"]["per_tool"]) >= {"search", "detect"}
    arts = log[1]
    assert arts["structured_bytes"] == 0 and arts["text_bytes"] == arts["bytes_out"] > 0
    assert arts["tokens_est"] == est_tokens(arts["text_bytes"])
    assert arts["results"] > 0 and arts["total"] == 7 and "art:1" in arts["cites"]
    s1, s2, s3 = log[2], log[3], log[4]
    assert s1["args_hash"] == s3["args_hash"]  # explicit null cursor and default: the same call
    assert s2["args_hash"] != s1["args_hash"] and s2["near_hash"] == s1["near_hash"]
    check = log[5]
    assert check["answer_cites"] == ["art:1", "log:3#1"] and check["ok"] is True
    assert "HVAC" not in json.dumps(check)  # the answer text is never logged


def test_structured_on_logs_structured_bytes(config: Config, case_zip: Path) -> None:
    cfg = dataclasses.replace(config, structured_output=True)

    async def main() -> None:
        async with Client(build_server(cfg, Policy())) as client:
            await client.call_tool("add_source", {"case": "T-1", "path": str(case_zip)})
            r = await client.call_tool("list_artifacts", {"case": "T-1"})
            assert r.structured_content is not None

    anyio.run(main)
    e = calllog(config)[1]
    assert e["structured_bytes"] > e["text_bytes"]
    assert e["tokens_est"] == est_tokens(e["structured_bytes"])


def test_capped_output_is_logged(home: Path, case_zip: Path) -> None:
    (home / "config.yaml").write_text("caps: {max_lines: 2}\n", encoding="utf-8")
    config = load_config()

    async def main() -> None:
        async with Client(build_server(config, Policy())) as client:
            await client.call_tool("add_source", {"case": "T-1", "path": str(case_zip)})
            await client.call_tool("list_artifacts", {"case": "T-1"})

    anyio.run(main)
    assert calllog(config)[1]["capped"] == ["lines"]


# -- analysis --------------------------------------------------------------------------------------------


def _transcript(path: Path, uses: list[tuple[str, dict[str, Any], str]], answer: str) -> Path:
    """A stream-json-like transcript: one request per tool use, then the answer."""
    lines: list[dict[str, Any]] = []
    prompt = 10_000
    for i, (name, inp, result) in enumerate(uses):
        lines.append(
            {
                "type": "assistant",
                "message": {
                    "id": f"m{i}",
                    "content": [{"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}],
                    "usage": {"input_tokens": 2, "cache_read_input_tokens": prompt, "output_tokens": 20},
                },
            }
        )
        err = result.startswith("<tool_use_error>")
        lines.append(
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": f"t{i}", "content": result, "is_error": err}
                    ]
                },
            }
        )
        prompt += 20 + len(result) // 2
    lines.append(
        {
            "type": "assistant",
            "message": {
                "id": "final",
                "content": [{"type": "text", "text": answer}],
                "usage": {"input_tokens": 2, "cache_read_input_tokens": prompt, "output_tokens": 300},
            },
        }
    )
    lines.append({"type": "result", "subtype": "success", "num_turns": len(uses) + 1, "result": answer})
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return path


def test_analysis_finds_repeats_uncited_and_rejected_calls(
    config: Config, case_zip: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = [
        ("add_source", {"case": "T-1", "path": str(case_zip)}),
        ("list_artifacts", {"case": "T-1"}),
        ("search", {"case": "T-1", "query": "failed"}),
        ("search", {"case": "T-1", "query": "failed"}),
        ("read_artifact", {"case": "T-1", "id": "art:1"}),
    ]
    texts: list[str] = []

    async def main() -> None:
        async with Client(build_server(config, Policy())) as client:
            for name, args in calls:
                r = await client.call_tool(name, args)
                texts.append(r.content[0].text)  # type: ignore[union-attr]

    anyio.run(main)
    answer = "The HVAC app crashed with a NullPointerException (art:1)."
    uses = [(f"mcp__automotive-log__{n}", a, t) for (n, a), t in zip(calls, texts, strict=True)]
    uses.insert(4, ("read_artifact", {"case": "T-1", "id": "art:1"}, "<tool_use_error>Error: No such tool"))
    tr = _transcript(tmp_path / "run.jsonl", uses, answer)

    sessions = cr.read_calllog(config.call_log)  # type: ignore[arg-type]
    assert len(sessions) == 1
    t = cr.read_transcript(tr)
    assert cr.match_score(t, sessions[0]) == 5
    report = cr.analyse(sessions[0], t)
    text = report.text()
    assert "repeat of #3" in text
    assert "not cited" in text  # the searches: the answer cites only art:1
    assert any("never reached the tool" in p for p in report.problems)
    assert any("not checked with check_citations" in p for p in report.problems)
    assert all(c.seen_bytes is not None and c.measured for c in sessions[0].calls)
    assert "model (transcript): 7 requests" in text
    assert "tool results re-sent with later requests" in text and "first request re-sent: 7 x" in text

    # the CLI picks the session matching the transcript, and lists sessions
    assert cli_main(["calls", "--transcript", str(tr)]) == 0
    out = capsys.readouterr().out
    assert "repeat of #3" in out and "suggestions:" in out
    assert cli_main(["calls", "--list"]) == 0
    assert "5 calls" in capsys.readouterr().out
    assert cli_main(["calls", "--fixed"]) == 0
    out = capsys.readouterr().out
    assert "per request" in out and "search" in out


def test_analysis_reads_old_call_log_lines(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home.mkdir(parents=True)
    old = [
        {
            "ts": "2026-11-09T10:00:00.000+00:00",
            "session": "s-old",
            "tool": "search",
            "args": {"case": "c", "query": "x", "limit": 50},
            "status": "ok",
            "duration_ms": 1.0,
            "bytes_out": 4000,
        },
        {
            "ts": "2026-11-09T10:00:01.000+00:00",
            "session": "s-old",
            "tool": "search",
            "args": {"case": "c", "query": "x", "limit": 50},
            "status": "error",
            "duration_ms": 1.0,
            "bytes_out": 0,
            "error": "invalid query",
        },
    ]
    (home / "calllog.jsonl").write_text("\n".join(json.dumps(x) for x in old) + "\n", encoding="utf-8")
    assert cli_main(["calls", "--last"]) == 0
    out = capsys.readouterr().out
    assert "session s-old" in out and "repeat of #1" in out and "1 tool errors" in out
    assert cli_main(["calls", "--session", "nope"]) == 1


# -- one log file per round ------------------------------------------------------------------------------


def test_round_files_split_after_a_pause(tmp_path: Path) -> None:
    from automotive_log_mcp.calllog import CallLog

    now = [0.0]
    log = CallLog(
        tmp_path / "calllog.jsonl",
        "s1",
        rounds_dir=tmp_path / "rounds",
        round_gap=600,
        reporter=lambda f: f"report of {f.name}",
        clock=lambda: now[0],
    )
    info = {"version": "x"}
    for t in (0, 10, 700, 710):  # 690 s pause before the third call: a new round
        now[0] = t
        log.write("search", {"case": "c", "query": "q"}, "ok", 1.0, 10, None, None, lambda: info)
    files = sorted((tmp_path / "rounds").glob("*.jsonl"))
    assert [f.name.rsplit("_", 1)[-1] for f in files] == ["r1.jsonl", "r2.jsonl"]
    assert all(":" not in f.name for f in files)  # valid file names on Windows
    rounds = [[json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] for f in files]
    assert [len(r) for r in rounds] == [2, 2]
    assert [("server" in e, e["round"]) for r in rounds for e in r] == [
        (True, 1),
        (False, 1),
        (True, 2),
        (False, 2),
    ]
    main = (tmp_path / "calllog.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x) for x in main] == rounds[0] + rounds[1]  # the same lines as the call log
    assert files[1].with_suffix(".txt").read_text(encoding="utf-8") == f"report of {files[1].name}\n"


def test_server_writes_round_file_with_report(
    config: Config, case_zip: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def main() -> None:
        async with Client(build_server(config, Policy())) as client:
            await client.call_tool("add_source", {"case": "T-1", "path": str(case_zip)})
            await client.call_tool("list_artifacts", {"case": "T-1"})
            await client.call_tool("list_artifacts", {"case": "T-1"})

    anyio.run(main)
    assert config.rounds_dir == config.home / "rounds"
    (f,) = config.rounds_dir.glob("*_r1.jsonl")
    report = f.with_suffix(".txt").read_text(encoding="utf-8")
    assert "3 calls" in report and "repeat of #2" in report and "problems:" in report
    assert "NullPointerException" not in f.read_text(encoding="utf-8")  # no log content
    assert cli_main(["calls", "--log", str(f)]) == 0
    assert "repeat of #2" in capsys.readouterr().out


def test_rounds_follow_call_log_setting(home: Path) -> None:
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("call_log: null\n", encoding="utf-8")
    assert load_config().rounds_dir is None
    (home / "config.yaml").write_text("rounds_dir: null\nround_gap_minutes: 30\n", encoding="utf-8")
    cfg = load_config()
    assert cfg.rounds_dir is None and cfg.call_log is not None and cfg.round_gap_minutes == 30
    (home / "config.yaml").write_text("round_gap_minutes: 0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="round_gap_minutes"):
        load_config()


# -- the same file in a new session ---------------------------------------------------------------------


def test_same_file_in_another_case_is_not_indexed_again(config: Config, case_zip: Path) -> None:
    texts: list[str] = []

    async def main() -> None:
        async with Client(build_server(config, Policy())) as client:
            for args in (
                {"case": "T-1", "path": str(case_zip)},
                {"case": "bugreport-analysis", "path": str(case_zip)},  # a new chat, a new case name
                {"case": "bugreport-analysis", "path": str(case_zip), "reindex": True},
            ):
                r = await client.call_tool("add_source", args)
                assert not r.is_error, r.content
                texts.append(r.content[0].text)  # type: ignore[union-attr]

    anyio.run(main)
    assert texts[0].startswith("Indexed")
    assert "already indexed in case 'T-1'" in texts[1] and "case='T-1'" in texts[1]
    assert texts[2].startswith("Indexed")  # reindex=true: a separate copy, as asked
    assert config.case_db("bugreport-analysis").exists()


def test_redirect_creates_no_empty_case(app: Any, case_zip: Path) -> None:
    r1 = app.ingestor.add(app.config.case_db("T-1"), str(case_zip))
    r2 = app.ingestor.add(app.config.case_db("T-2"), str(case_zip))
    assert r2.files[0].other_case == "T-1" and r2.files[0].source_ids == r1.source_ids
    assert not app.config.case_db("T-2").exists()


def test_interrupted_import_is_redone_with_a_note(app: Any, case_zip: Path) -> None:
    from automotive_log_mcp.store import db

    path = app.config.case_db("T-1")
    app.ingestor.add(path, str(case_zip))
    with db.opened(path) as conn, conn:
        conn.execute("UPDATE sources SET status = 'indexing'")  # as if the server was stopped
    again = app.ingestor.add(path, str(case_zip)).files[0]
    assert not again.existing and again.records > 0
    assert "did not finish" in again.notes[0]
    assert app.ingestor.add(path, str(case_zip)).files[0].existing  # done now: not indexed again
