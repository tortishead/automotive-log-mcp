"""End to end over stdio with two independent clients: the MCP SDK client and raw JSON-RPC."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import anyio
from mcp import Client, StdioServerParameters

from automotive_log_mcp.server import TOOL_NAMES

SERVE = [sys.executable, "-m", "automotive_log_mcp", "serve", "--structured"]  # structured contract
SERVE_DEFAULT = [sys.executable, "-m", "automotive_log_mcp", "serve"]


def env_for(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["AUTOMOTIVE_LOG_MCP_HOME"] = str(home)
    env.pop("AUTOMOTIVE_LOG_MCP_CONFIG", None)
    return env


def test_sdk_client_over_stdio(home: Path) -> None:
    params = StdioServerParameters(command=SERVE[0], args=SERVE[1:], env=env_for(home))

    async def main() -> None:
        async with Client(params) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert names == set(TOOL_NAMES)
            result = await client.call_tool("list_sources", {"case": "c1"})
            assert result.structured_content is not None
            assert result.structured_content["sources"] == []

    anyio.run(main)
    lines = (home / "calllog.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["tool"] for line in lines] == ["list_sources"]


def test_raw_jsonrpc_client_over_stdio(home: Path) -> None:
    """A minimal client written against the MCP spec, not the SDK (2025-06-18 handshake)."""
    messages: list[dict[str, Any]] = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "raw-test-client", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "list_sources", "arguments": {"case": "c1"}},
        },
    ]
    responses: dict[int, dict[str, Any]] = {}
    with subprocess.Popen(
        SERVE,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env_for(home),
    ) as proc:
        assert proc.stdin is not None and proc.stdout is not None
        # One request at a time, waiting for each reply, as a real client does.
        for message in messages:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
            if "id" not in message:
                continue
            while True:
                line = proc.stdout.readline()
                assert line, f"server closed stdout; stderr: {proc.stderr.read() if proc.stderr else ''}"
                reply = json.loads(line)
                if reply.get("id") == message["id"]:
                    responses[reply["id"]] = reply
                    break
        proc.stdin.close()
        assert proc.wait(timeout=30) == 0

    init = responses[1]["result"]
    assert init["serverInfo"]["name"] == "automotive-log-mcp"
    assert "citation" in init["instructions"].lower()

    assert {t["name"] for t in responses[2]["result"]["tools"]} == set(TOOL_NAMES)

    call = responses[3]["result"]
    assert call.get("isError") is not True
    assert call["structuredContent"] == {"case": "c1", "exists": False, "sources": []}


def test_bad_config_exits_with_message(home: Path) -> None:
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("cases_dirr: x\n", encoding="utf-8")
    proc = subprocess.run(SERVE, capture_output=True, text=True, env=env_for(home), timeout=60, check=False)
    assert proc.returncode == 2
    assert "cases_dirr" in proc.stderr
    assert proc.stdout == ""


def test_p0_workflow_over_stdio(home: Path, tmp_path: Path) -> None:
    """The M3 workflow through a real stdio server: find the known crash, read its stack trace and
    the lines before it, and check that every citation the tools printed resolves."""
    import re

    from .conftest import FIXED_SECRET
    from .test_golden import make_bugreport_zip

    home.mkdir(parents=True, exist_ok=True)
    (home / "secret.key").write_bytes(FIXED_SECRET)
    z = make_bugreport_zip(tmp_path)
    params = StdioServerParameters(command=SERVE[0], args=SERVE[1:], env=env_for(home))

    async def main() -> None:
        async with Client(params) as client:

            async def call(name: str, **args: Any) -> str:
                r = await client.call_tool(name, {"case": "T-9", **args})
                assert not r.is_error, (name, args, r.content)
                assert r.structured_content is not None or name == "add_source"
                text = r.content[0].text  # type: ignore[union-attr]
                assert len(text.encode("utf-8")) <= 8192
                return text

            prompt = await client.get_prompt("defect_triage", {"case": "T-9", "symptom": "HVAC app crashes"})
            assert "list_artifacts" in str(prompt.messages[0].content)
            await call("add_source", path=str(z))
            seen = await call("list_artifacts", type="java_crash")
            assert "NullPointerException com.example.hvac" in seen
            summary = await call("read_artifact", id="art:4")
            section = re.search(r"next: read_artifact\(id='(art:4/[a-z0-9_.-]+)'\)", summary)
            assert section is not None
            stack = await call("read_artifact", id=section.group(1))
            assert "at com.example.hvac.Controller.update(Controller.java:42)" in stack
            before = await call("context", at="art:1", scope="pid", before=10, after=2)
            assert "setProperty failed, status=NOT_AVAILABLE" in before
            hits = await call("search", query="hvac", filter={"level": "W", "to": "log:3#1+1s"})
            text = "\n".join([seen, summary, stack, before, hits])

            logs = sorted(set(re.findall(r"\blog:\d+#\d+\b", text)))
            full = await call("get_records", ids=logs)
            for c in logs:
                assert re.search(rf"^{re.escape(c)}  ", full, re.M), c
            for c in sorted(set(re.findall(r"\bart:\d+(?:/[a-z0-9_.-]+)?(?::L\d+)?", text))):
                await call("read_artifact", id=c)
            for n in re.findall(r"^L(\d+) ", stack, re.M):
                line = await call("read_artifact", id=f"{section.group(1)}:L{n}")
                assert f"L{n}  " in line

    anyio.run(main)


def test_p1_workflow_over_stdio(home: Path, tmp_path: Path) -> None:
    """M4 over stdio: the prompt asks for check_citations; overview and detect orient, an answer
    built from their citations passes check_citations, a wrong citation fails it."""
    import re

    from .conftest import FIXED_SECRET
    from .test_golden import make_bugreport_zip

    home.mkdir(parents=True, exist_ok=True)
    (home / "secret.key").write_bytes(FIXED_SECRET)
    z = make_bugreport_zip(tmp_path)
    params = StdioServerParameters(command=SERVE[0], args=SERVE[1:], env=env_for(home))

    async def main() -> None:
        async with Client(params) as client:

            async def call(name: str, **args: Any) -> tuple[str, dict[str, Any]]:
                r = await client.call_tool(name, {"case": "T-9", **args})
                assert not r.is_error, (name, args, r.content)
                text = r.content[0].text  # type: ignore[union-attr]
                assert len(text.encode("utf-8")) <= 8192
                return text, r.structured_content or {}

            prompt = await client.get_prompt("defect_triage", {"case": "T-9", "symptom": "HVAC app crashes"})
            body = str(prompt.messages[0].content)
            assert "check_citations(case='T-9'" in body and "overview(case='T-9')" in body
            await call("add_source", path=str(z))
            ov, ov_data = await call("overview")
            assert ov_data["detect"]["java_crash"] == 3 and "levels:" in ov
            det, _ = await call("detect", rules=["java_crash", "vhal_error"])
            cites = re.findall(r"\blog:\d+#\d+", det)
            assert "log:3#1" in cites and "log:1#2" in cites
            counted, c_data = await call("count", by="level", bucket="1m")
            assert c_data["buckets"] and "bucket 1m" in counted
            answer = (
                "# HVAC crash\n\nsetProperty failed (log:1#2), then the app crashed (log:3#1, "
                "art:1/exception:L3).\n"
            )
            ok, ok_data = await call("check_citations", text=answer)
            assert ok_data["ok"] is True and ok.startswith("PASS")
            bad, bad_data = await call("check_citations", text=answer.replace("log:3#1", "log:3#7"))
            assert bad_data["ok"] is False and "log:3#7 does not resolve" in bad

    anyio.run(main)
    lines = [json.loads(x) for x in (home / "calllog.jsonl").read_text(encoding="utf-8").splitlines()]
    checks = [x for x in lines if x["tool"] == "check_citations"]
    assert len(checks) == 2 and all(x["args"]["text"].startswith("<") for x in checks)
    assert "setProperty" not in json.dumps(checks)
