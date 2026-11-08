"""add_source and list_sources over the MCP client, call-log redaction, policy."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client

from automotive_log_mcp import server as server_mod
from automotive_log_mcp.config import Config
from automotive_log_mcp.core.redact import Redactor
from automotive_log_mcp.policy import Policy
from automotive_log_mcp.server import build_server

from .conftest import FIXED_SECRET, copy_fixture
from .test_golden import make_bugreport_zip


def calllog(config: Config) -> list[dict[str, Any]]:
    assert config.call_log is not None
    return [json.loads(x) for x in config.call_log.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def fixed_secret(config: Config) -> None:
    config.home.mkdir(parents=True, exist_ok=True)
    (config.home / "secret.key").write_bytes(FIXED_SECRET)


def test_add_and_list_bugreport(config: Config, tmp_path: Path, fixed_secret: None) -> None:
    z = make_bugreport_zip(tmp_path)

    async def main() -> None:
        async with Client(build_server(config, Policy())) as client:
            r = await client.call_tool("add_source", {"case": "T-1", "path": str(z)})
            assert not r.is_error, r.content
            text = r.content[0].text  # type: ignore[union-attr]
            assert "Indexed" in text and "sources 1, 2, 3, 4, 5, 6" in text
            again = await client.call_tool("add_source", {"case": "T-1", "path": str(z)})
            assert "already in the case" in again.content[0].text  # type: ignore[union-attr]

            ls = await client.call_tool("list_sources", {"case": "T-1"})
            assert not ls.is_error
            sc = ls.structured_content
            assert sc is not None and sc["exists"] is True
            by_stream = {s["name"].rsplit(":", 1)[-1]: s for s in sc["sources"]}
            assert by_stream["crash"]["records"] == 1
            assert by_stream["kernel"]["format"] == "kernel"
            assert by_stream["main"]["fingerprint"].startswith("google/sdk_car_x86_64")
            assert by_stream["main"]["ts_from"] == "2025-12-31 23:59:58.100"
            lines = ls.content[0].text.splitlines()  # type: ignore[union-attr]
            assert lines[-1].startswith("fingerprint: google/")

    anyio.run(main)
    log = calllog(config)
    assert [e["tool"] for e in log] == ["add_source", "add_source", "list_sources"]


def test_add_source_errors(config: Config, tmp_path: Path) -> None:
    notes = tmp_path / "notes.txt"
    notes.write_text("hello\n", encoding="utf-8")

    async def main() -> None:
        async with Client(build_server(config, Policy(blocked_sources=("*secret*",)))) as client:
            for args, match in [
                ({"path": str(tmp_path / "missing.log")}, "no such file"),
                ({"path": str(notes)}, "unknown format"),
                ({"path": str(notes), "format": "nope"}, "unknown format 'nope'"),
                ({"path": str(tmp_path / "secret.log")}, "no such file"),
            ]:
                r = await client.call_tool("add_source", {"case": "c", **args})
                assert r.is_error, args
                assert match in r.content[0].text, (args, r.content)  # type: ignore[union-attr]
            (tmp_path / "secret.log").write_text("x\n", encoding="utf-8")
            r = await client.call_tool("add_source", {"case": "c", "path": str(tmp_path / "secret.log")})
            assert r.is_error and "blocked" in r.content[0].text  # type: ignore[union-attr]

    anyio.run(main)


def test_call_log_arguments_are_redacted(config: Config, tmp_path: Path, fixed_secret: None) -> None:
    raw_path = tmp_path / "WVWZZZ1JZXW000001" / "trace 192.168.1.23.log"

    async def main() -> None:
        async with Client(build_server(config, Policy())) as client:
            await client.call_tool("add_source", {"case": "c", "path": str(raw_path)})
            await client.call_tool("search", {"case": "c", "query": "peer 3c:5a:b4:01:02:03"})

    anyio.run(main)
    text = config.call_log.read_text(encoding="utf-8")  # type: ignore[union-attr]
    assert "WVWZZZ1JZXW000001" not in text and "192.168.1.23" not in text and "3c:5a:b4" not in text
    assert "<VIN:" in text and "<MAC:" in text
    r = Redactor(FIXED_SECRET)
    for entry in calllog(config):
        assert r.find(json.dumps(entry)) == []


def test_add_source_reports_background_indexing(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = copy_fixture("logcat/threadtime.log", tmp_path)
    monkeypatch.setattr(server_mod, "ADD_WAIT_S", 0.0)
    import threading

    from automotive_log_mcp.core import ingest

    gate = threading.Event()
    orig = ingest.Ingestor.add

    def slow_add(self, *a, **k):  # type: ignore[no-untyped-def]
        gate.wait(10)
        return orig(self, *a, **k)

    monkeypatch.setattr(ingest.Ingestor, "add", slow_add)

    async def main() -> None:
        async with Client(build_server(config, Policy())) as client:
            r = await client.call_tool("add_source", {"case": "c", "path": str(p)})
            assert "background" in r.content[0].text  # type: ignore[union-attr]
            ls = await client.call_tool("list_sources", {"case": "c"})
            assert ls.structured_content is not None
            gate.set()

    anyio.run(main)
