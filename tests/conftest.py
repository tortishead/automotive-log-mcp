from __future__ import annotations

from pathlib import Path

import pytest

from automotive_log_mcp.config import Config, load_config


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("AUTOMOTIVE_LOG_MCP_HOME", str(h))
    monkeypatch.delenv("AUTOMOTIVE_LOG_MCP_CONFIG", raising=False)
    return h


@pytest.fixture
def config(home: Path) -> Config:
    return load_config()
