from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from automotive_log_mcp.config import ConfigError, load_config
from automotive_log_mcp.policy import Policy, load_policy
from automotive_log_mcp.store import db


def test_defaults(home: Path) -> None:
    c = load_config()
    assert c.cases_dir == home / "cases"
    assert c.policy_file == home / "policy.yaml"
    assert c.call_log == home / "calllog.jsonl"
    assert c.caps.max_lines == 50
    assert c.caps.max_bytes == 8192
    assert c.case_db("T-1") == home / "cases" / "T-1.db"


def test_relative_paths_resolve_against_config_file(tmp_path: Path, home: Path) -> None:
    cfg = tmp_path / "cfg" / "c.yaml"
    cfg.parent.mkdir()
    cfg.write_text("cases_dir: mycases\nrule_dirs: [rules]\ncaps: {max_lines: 20}\n", encoding="utf-8")
    c = load_config(cfg)
    assert c.cases_dir == cfg.parent / "mycases"
    assert c.rule_dirs == (cfg.parent / "rules",)
    assert c.caps.max_lines == 20
    assert c.caps.max_bytes == 8192


@pytest.mark.parametrize(
    "text",
    ["nope: 1\n", "caps: {max_lines: 0}\n", "caps: {lines: 5}\n", "rule_dirs: x\n", "- a\n", "a: [\n"],
)
def test_invalid_config(home: Path, text: str) -> None:
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config()


def test_explicit_missing_config_is_an_error(tmp_path: Path, home: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "missing.yaml")


def test_policy_missing_file_is_permissive(tmp_path: Path) -> None:
    p = load_policy(tmp_path / "policy.yaml")
    assert p == Policy()
    assert p.tool_allowed("search")


def test_policy_file(tmp_path: Path) -> None:
    f = tmp_path / "policy.yaml"
    f.write_text(
        "allowed_tools: [search]\nblocked_sources: ['*/private/*']\nblocked_ecus: [ECU1]\n"
        "byte_budget: 1000\nallow_code_connectors: true\n",
        encoding="utf-8",
    )
    p = load_policy(f)
    assert p.tool_allowed("search")
    assert not p.tool_allowed("context")
    assert p.source_blocked("/logs/private/a.txt")
    assert not p.source_blocked("/logs/a.txt")
    assert p.ecu_blocked("ECU1")
    assert p.byte_budget == 1000
    assert p.allow_code_connectors


@pytest.mark.parametrize(
    "text", ["byte_budget: -1\n", "allowed_tools: search\n", "allow_code_connectors: yes please\n", "x: 1\n"]
)
def test_invalid_policy(tmp_path: Path, text: str) -> None:
    f = tmp_path / "policy.yaml"
    f.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_policy(f)


def test_fts5_available() -> None:
    db.check_fts5()


def test_fts5_missing_gives_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class NoFts5:
        def execute(self, sql: str) -> None:
            raise sqlite3.OperationalError("no such module: fts5")

        def close(self) -> None:
            pass

    monkeypatch.setattr(db.sqlite3, "connect", lambda _: NoFts5())
    with pytest.raises(db.StoreError, match="FTS5"):
        db.check_fts5()
