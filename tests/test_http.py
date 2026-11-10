"""M6 HTTP transport: streamable HTTP from the SDK with a bearer token, the path policy for
add_source (allowed_roots), config and CLI errors. A real uvicorn server on a free port."""

from __future__ import annotations

import dataclasses
import json
import os
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from automotive_log_mcp.cli import main as cli_main
from automotive_log_mcp.config import Config, ConfigError, HttpConfig, load_config
from automotive_log_mcp.httpserve import ENV_TOKEN, build_http_app, http_policy, load_token
from automotive_log_mcp.policy import Policy, load_policy
from automotive_log_mcp.server import TOOL_NAMES

from .conftest import FIXED_SECRET, REF_TIME

TOKEN = "t0k3n-for-the-http-tests-only"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Server:
    def __init__(self, config: Config, policy: Policy) -> None:
        port = _free_port()
        self.config = dataclasses.replace(config, http=dataclasses.replace(config.http, port=port))
        self.app, self.policy = build_http_app(self.config, policy, TOKEN)
        self.url = f"http://127.0.0.1:{port}/mcp"
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> _Server:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.02)
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(10)


@pytest.fixture
def http_config(config: Config) -> Config:
    config.home.mkdir(parents=True, exist_ok=True)
    (config.home / "secret.key").write_bytes(FIXED_SECRET)
    return config


@pytest.fixture
def logs(tmp_path: Path) -> Path:
    d = tmp_path / "logs"
    d.mkdir()
    lines = [f"10-01 08:00:{i:02d}.000   100   100 E CarService: failed {i} times" for i in range(5)]
    f = d / "a.log"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(f, (REF_TIME, REF_TIME))
    return d


def _client(url: str, token: str | None = TOKEN) -> Client:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return Client(streamable_http_client(url, http_client=httpx2.AsyncClient(headers=headers, timeout=30)))


def _text(result: Any) -> str:
    return "\n".join(c.text for c in result.content if hasattr(c, "text"))


def test_tools_over_http_with_token_and_allowed_roots(
    http_config: Config, logs: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "secret.log"
    outside.write_text("10-01 08:00:00.000   1   1 I x: private\n", encoding="utf-8")
    (logs / "link.log").symlink_to(outside)
    policy = dataclasses.replace(Policy(), allowed_roots=(logs.resolve(),))
    with _Server(http_config, policy) as srv:

        async def main() -> None:
            async with _client(srv.url) as client:
                names = {t.name for t in (await client.list_tools()).tools}
                assert names == set(TOOL_NAMES)
                r = await client.call_tool("add_source", {"case": "h", "path": str(logs / "a.log")})
                assert not r.is_error, _text(r)
                assert "5 records" in _text(r)
                r = await client.call_tool("search", {"case": "h", "query": "failed"})
                assert "log:1#1" in _text(r)
                # outside the roots: refused, and the reply does not say whether the file exists
                for p in (str(outside), str(tmp_path / "missing.log"), str(logs / ".." / "secret.log")):
                    r = await client.call_tool("add_source", {"case": "h", "path": p})
                    assert r.is_error and "outside the directories" in _text(r), _text(r)
                    assert "no such file" not in _text(r)
                # a symbolic link inside a root that points outside it
                r = await client.call_tool("add_source", {"case": "h", "path": str(logs / "link.log")})
                assert r.is_error and "outside the directories" in _text(r)
                r = await client.call_tool("add_source", {"case": "h2", "path": str(logs)})
                assert "links outside the policy's allowed_roots" in _text(r)
                r = await client.call_tool("add_source", {"case": "h", "path": "a.log"})
                assert r.is_error and "absolute path" in _text(r)

        anyio.run(main)
    # the call log has the calls, as over stdio
    assert http_config.call_log is not None
    tools = [json.loads(x)["tool"] for x in http_config.call_log.read_text(encoding="utf-8").splitlines()]
    assert tools[:2] == ["add_source", "search"]


def test_missing_or_wrong_token_is_refused(http_config: Config) -> None:
    with _Server(http_config, Policy()) as srv:
        for headers in ({}, {"Authorization": "Bearer wrong-token-0123456789"}, {"Authorization": TOKEN}):
            r = httpx2.post(
                srv.url,
                headers={**headers, "content-type": "application/json", "accept": "application/json"},
                content=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
            )
            assert r.status_code == 401, (headers, r.status_code)
            assert "Bearer" in r.headers.get("www-authenticate", "")

        async def main() -> None:
            with pytest.raises(Exception):  # noqa: B017 - the client gives up on 401
                async with _client(srv.url, token="wrong-token-0123456789") as client:
                    await client.list_tools()

        anyio.run(main)


def test_http_without_allowed_roots_refuses_every_path(http_config: Config, logs: Path) -> None:
    assert http_policy(Policy()).allowed_roots == ()
    with _Server(http_config, Policy()) as srv:
        assert srv.policy.allowed_roots == ()

        async def main() -> None:
            async with _client(srv.url) as client:
                r = await client.call_tool("add_source", {"case": "h", "path": str(logs / "a.log")})
                assert r.is_error and "no allowed_roots" in _text(r)
                r = await client.call_tool("list_sources", {"case": "h"})
                assert not r.is_error

        anyio.run(main)


def test_dns_rebinding_protection_with_allowed_hosts(http_config: Config) -> None:
    cfg = dataclasses.replace(http_config, http=HttpConfig(allowed_hosts=("logs.example.internal:8765",)))
    with _Server(cfg, Policy()) as srv:
        r = httpx2.post(
            srv.url,
            headers={"Authorization": f"Bearer {TOKEN}", "content-type": "application/json"},
            content=b"{}",
        )
        assert r.status_code == 421  # Host 127.0.0.1:<port> is not listed


def test_allowed_roots_apply_to_the_cli_and_load_from_the_policy(
    home: Path, logs: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "policy.yaml").write_text(f"allowed_roots: [{json.dumps(str(logs))}, sub]\n", encoding="utf-8")
    policy = load_policy(home / "policy.yaml")
    assert policy.allowed_roots == (logs.resolve(), (home / "sub").resolve())
    assert cli_main(["index", "c", str(logs / "a.log"), "-q"]) == 0
    other = tmp_path / "other.log"
    other.write_text("x\n", encoding="utf-8")
    assert cli_main(["index", "c", str(other), "-q"]) == 1
    assert "outside the directories" in capsys.readouterr().err
    (home / "policy.yaml").write_text("allowed_roots: x\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="allowed_roots"):
        load_policy(home / "policy.yaml")


def test_token_and_http_config(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv(ENV_TOKEN, raising=False)
    cfg = load_config()
    with pytest.raises(ConfigError, match="needs a bearer token"):
        load_token(cfg)
    (home / "token.txt").write_text("short\n", encoding="utf-8")
    (home / "config.yaml").write_text(
        "http: {host: 0.0.0.0, port: 9000, path: /logs, token_file: token.txt, allowed_hosts: ['a:9000']}\n",
        encoding="utf-8",
    )
    cfg = load_config()
    assert cfg.http == HttpConfig("0.0.0.0", 9000, "/logs", home / "token.txt", ("a:9000",))
    with pytest.raises(ConfigError, match="at least 16"):
        load_token(cfg)
    (home / "token.txt").write_text(TOKEN + "\n", encoding="utf-8")
    assert load_token(cfg) == TOKEN
    monkeypatch.setenv(ENV_TOKEN, "from-the-environment-1234")
    assert load_token(cfg) == "from-the-environment-1234"
    for text, match in (
        ("http: {port: 0}", "port"),
        ("http: {path: mcp}", "path"),
        ("http: {tls: true}", "unknown keys under 'http'"),
        ("http: [1]", "mapping"),
    ):
        (home / "config.yaml").write_text(text + "\n", encoding="utf-8")
        with pytest.raises(ConfigError, match=match):
            load_config()
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.delenv(ENV_TOKEN)
    assert cli_main(["serve", "--http"]) == 2
    assert "needs a bearer token" in capsys.readouterr().err
    assert cli_main(["serve", "--port", "9000"]) == 2
    assert "need --http" in capsys.readouterr().err


@pytest.fixture(autouse=True)
def _no_token_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(ENV_TOKEN, raising=False)
    yield
