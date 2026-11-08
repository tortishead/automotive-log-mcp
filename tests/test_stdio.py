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

SERVE = [sys.executable, "-m", "automotive_log_mcp", "serve"]


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
