from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from automotive_log_mcp.app import App
from automotive_log_mcp.config import Config, load_config
from automotive_log_mcp.core.timeparse import ts_raw_of
from automotive_log_mcp.policy import Policy
from automotive_log_mcp.store import db, query

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = Path(__file__).parent / "golden"
FIXED_SECRET = b"0" * 64  # deterministic redaction tokens in golden files
REF_TIME = datetime(2026, 11, 1, 12, 0, 0).timestamp()  # mtime of copied fixtures: reference year


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("AUTOMOTIVE_LOG_MCP_HOME", str(h))
    monkeypatch.delenv("AUTOMOTIVE_LOG_MCP_CONFIG", raising=False)
    return h


@pytest.fixture
def config(home: Path) -> Config:
    return load_config()


@pytest.fixture
def app(config: Config) -> App:
    config.home.mkdir(parents=True, exist_ok=True)
    (config.home / "secret.key").write_bytes(FIXED_SECRET)
    return App.create(config, Policy())


def copy_fixture(rel: str, dest_dir: Path) -> Path:
    """Copy a fixture next to the test (originals stay untouched) with a fixed mtime."""
    dest = dest_dir / Path(rel).name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / rel, dest)
    os.utime(dest, (REF_TIME, REF_TIME))
    return dest


def dump_case(app: App, case: str) -> dict[str, Any]:
    """Sources and records of a case in a stable, readable form (for golden files)."""
    rows = app.list_sources(case) or []
    fmts = {s.id: s.ts_format for s in rows}
    out: dict[str, Any] = {"sources": [], "records": []}
    for s in rows:
        t0, t1 = query.time_range(s)
        out["sources"].append(
            {
                "id": s.id,
                "name": s.name,
                "format": s.format,
                "stream": s.stream,
                "status": s.status,
                "records": s.records,
                "lines": s.lines,
                "unparsed": s.unparsed,
                "range": [t0, t1],
                "segments": s.segments,
                "fingerprint": s.fingerprint,
                "meta": {k: v for k, v in s.meta.items() if k not in ("boot_time_estimate", "dumpstate_ns")},
            }
        )
    with db.opened(app.config.case_db(case)) as conn:
        for r in conn.execute(
            "SELECT source_id, seq, ts, ts_src, ts_raw, level, tag, pid, tid, msg, seg, attrs, "
            "(SELECT text FROM templates WHERE id = template_id) FROM records_v ORDER BY id"
        ):
            sid, seq, ts, ts_src, ts_raw, level, tag, pid, tid, msg, seg, attrs, tmpl = r
            rec = {
                "cite": f"log:{sid}#{seq}",
                "ts_raw": ts_raw_of(ts_raw, ts, None if ts_src == ts else ts_src, fmts.get(sid)),
                "ts": query.fmt_ns(ts),
                "ts_src": ts_src if ts_src != ts else None,
                "level": level,
                "tag": tag,
                "pid": pid,
                "tid": tid,
                "msg": msg,
                "template": tmpl,
                "seg": seg,
            }
            if attrs:
                rec["attrs"] = json.loads(attrs)
            out["records"].append({k: v for k, v in rec.items() if v is not None})
        out["segments"] = [
            list(row)
            for row in conn.execute(
                "SELECT source_id, seg, reason, first_seq, last_seq, count FROM segments ORDER BY 1, 2"
            )
        ]
    return out


def check_golden(name: str, data: dict[str, Any]) -> None:
    path = GOLDEN / f"{name}.json"
    text = json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True) + "\n"
    if os.environ.get("UPDATE_GOLDEN") or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if not os.environ.get("UPDATE_GOLDEN"):
            pytest.fail(f"golden file {path.name} was missing and has been written; review and rerun")
    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(text)
