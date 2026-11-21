"""allowed_roots in policy.yaml: add_source reads only below the listed folders."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from automotive_log_mcp.app import App
from automotive_log_mcp.config import ConfigError, load_config
from automotive_log_mcp.core.ingest import IngestError
from automotive_log_mcp.policy import load_policy

from .conftest import FIXED_SECRET, REF_TIME


def test_allowed_roots_apply_to_ingest_and_load_from_the_policy(home: Path, tmp_path: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "secret.key").write_bytes(FIXED_SECRET)
    logs = tmp_path / "logs"
    logs.mkdir()
    f = logs / "a.log"
    f.write_text("10-01 08:00:00.000   100   100 E CarService: failed\n", encoding="utf-8")
    os.utime(f, (REF_TIME, REF_TIME))
    (home / "policy.yaml").write_text(f"allowed_roots: [{json.dumps(str(logs))}, sub]\n", encoding="utf-8")
    policy = load_policy(home / "policy.yaml")
    assert policy.allowed_roots == (logs.resolve(), (home / "sub").resolve())
    app = App.create(load_config(), policy)
    app.ingestor.add(app.config.case_db("c"), str(f))
    other = tmp_path / "other.log"
    other.write_text("x\n", encoding="utf-8")
    with pytest.raises(IngestError, match="outside the directories"):
        app.ingestor.add(app.config.case_db("c"), str(other))
    (home / "policy.yaml").write_text("allowed_roots: x\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="allowed_roots"):
        load_policy(home / "policy.yaml")
