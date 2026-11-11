"""M5 command connector: an external program writes JSON lines (D13)."""

from __future__ import annotations

import dataclasses
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from automotive_log_mcp.app import App
from automotive_log_mcp.config import Config, ConfigError
from automotive_log_mcp.connectors.base import ConnectorError
from automotive_log_mcp.connectors.command import CommandConnector
from automotive_log_mcp.core.ingest import IngestError
from automotive_log_mcp.policy import Policy
from automotive_log_mcp.store import db
from automotive_log_mcp.tools import Tools

from .conftest import FIXED_SECRET

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

SCRIPT = r"""
import json, sys
src = open(sys.argv[1], encoding="utf-8") if len(sys.argv) > 1 else sys.stdin
lines = src.read().splitlines()
print(json.dumps({"type": "meta", "values": {"timezone": "UTC", "ecu": "ECU7"}}))
for n, line in enumerate(lines, 1):
    if line == "CRASH":
        print(json.dumps({"type": "artifact", "artifact_type": "ecu_reset", "headline": "ECU reset",
                          "sections": [{"name": "Dump", "lines": ["pc=0x1", "lr=0x2"]}],
                          "ts": "2026-10-01 08:00:09.000", "signature": "reset"}))
    elif line == "GARBAGE":
        print("this is not json")
    elif line == "FAIL":
        sys.stderr.write("decoder: unsupported block at line %d\n" % n)
        sys.exit(3)
    else:
        print(json.dumps({"ts": "2026-10-01 08:00:%02d.250" % n, "level": "error" if "err" in line else "I",
                          "tag": "ECU", "pid": 7, "msg": line, "attrs": {"ecu": "ECU7", "n": n}}))
"""


def _rules(tmp_path: Path, command: list[str], extra: str = "") -> Path:
    d = tmp_path / "rules"
    d.mkdir(exist_ok=True)
    (d / "ecu2jsonl.py").write_text(SCRIPT, encoding="utf-8")
    (d / "ecu.yaml").write_text(
        "connector: ecu_log\nversion: '2'\n"
        f"command: {json.dumps(command)}\n"
        "sniff: {filenames: ['*.ecu']}\n" + extra,
        encoding="utf-8",
    )
    return d


def _app(config: Config, rule_dir: Path) -> App:
    cfg = dataclasses.replace(config, rule_dirs=(rule_dir,))
    cfg.home.mkdir(parents=True, exist_ok=True)
    (cfg.home / "secret.key").write_bytes(FIXED_SECRET)
    return App.create(cfg, Policy())


@pytest.mark.parametrize(
    "command", [["{python}", "{dir}/ecu2jsonl.py", "{path}"], ["{python}", "{dir}/ecu2jsonl.py"]]
)
def test_command_connector_records_artifacts_meta(config: Config, tmp_path: Path, command: list[str]) -> None:
    app = _app(config, _rules(tmp_path, command))
    log = tmp_path / "trace.ecu"
    log.write_text("door open\nerr: CAN timeout to 00:11:22:33:44:55\nGARBAGE\nCRASH\n", encoding="utf-8")
    res = app.ingestor.add(app.config.case_db("e"), str(log))
    assert res.files[0].records == 4
    s = (app.list_sources("e") or [])[0]
    assert s.format == "ecu_log" and s.connector_version == "2"
    assert s.meta.get("timezone") == "UTC" and s.meta.get("ecu") == "ECU7"
    assert s.unparsed == 1
    tools = Tools(app)
    out = tools.search("e", "CAN timeout").text
    assert "E 7 ECU  err: CAN timeout to <MAC:" in out, out  # redacted like any source
    assert "00:11:22" not in out
    arts = tools.list_artifacts("e").text
    assert "ecu_reset" in arts and "ECU reset" in arts, arts
    assert "pc=0x1" in tools.read_artifact("e", "art:1", section="dump").text
    rec = tools.get_records("e", ["log:1#1"]).data.records[0]
    assert rec.attrs == {"ecu": "ECU7", "n": 1} and rec.ts_raw == "2026-10-01 08:00:01.250"


def test_command_connector_failure_fails_the_import(config: Config, tmp_path: Path) -> None:
    app = _app(config, _rules(tmp_path, ["{python}", "{dir}/ecu2jsonl.py", "{path}"]))
    log = tmp_path / "bad.ecu"
    log.write_text("ok\nFAIL\n", encoding="utf-8")
    with pytest.raises(ConnectorError, match="exited with status 3: decoder: unsupported block at line 2"):
        app.ingestor.add(app.config.case_db("f"), str(log))
    s = (app.list_sources("f") or [])[0]
    assert s.status == "failed" and "status 3" in (s.error or "")


def test_command_connector_reads_archive_members(config: Config, tmp_path: Path) -> None:
    app = _app(config, _rules(tmp_path, ["{python}", "{dir}/ecu2jsonl.py", "{path}"]))
    z = tmp_path / "logs.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("car/trace.ecu", "door open\ndoor closed\n")
    app.ingestor.add(app.config.case_db("z"), str(z))
    rows = app.list_sources("z") or []
    assert [(s.format, s.records) for s in rows] == [("ecu_log", 2)]


def test_command_connector_timeout(config: Config, tmp_path: Path) -> None:
    d = _rules(tmp_path, ["{python}", "-c", "import time; time.sleep(30)"], "timeout: 0.5\n")
    app = _app(config, d)
    log = tmp_path / "slow.ecu"
    log.write_text("x\n", encoding="utf-8")
    with pytest.raises(ConnectorError, match=r"stopped after 0\.5 s"):
        app.ingestor.add(app.config.case_db("t"), str(log))


def test_command_connector_definition_errors(tmp_path: Path) -> None:
    p = tmp_path / "x.yaml"
    with pytest.raises(ConnectorError, match="list of arguments"):
        CommandConnector({"connector": "x", "command": "tool {path}"}, p)
    with pytest.raises(ConnectorError, match="unknown placeholder"):
        CommandConnector({"connector": "x", "command": ["tool", "{file}"]}, p)
    with pytest.raises(ConnectorError, match="unknown keys"):
        CommandConnector({"connector": "x", "command": ["tool"], "format": "jsonl"}, p)
    with pytest.raises(ConnectorError, match="magic"):
        CommandConnector({"connector": "x", "command": ["tool"], "sniff": {"magic": "zz"}}, p)


def test_command_connector_sniff() -> None:
    c = CommandConnector(
        {
            "connector": "x",
            "command": ["tool"],
            "sniff": {"filenames": ["*.bin"], "magic": "4d43", "head_regex": "^MC", "score": 0.8},
        },
        Path("x.yaml"),
    )
    assert c.sniff(b"MCU trace", "a.bin") == 0.8
    assert c.sniff(b"MCU trace", "a.txt") == 0.0
    assert c.sniff(b"XX", "a.bin") == 0.0
    forced_only = CommandConnector({"connector": "y", "command": ["tool"]}, Path("y.yaml"))
    assert forced_only.sniff(b"anything", "a.bin") == 0.0


def test_missing_program_is_a_clear_error(config: Config, tmp_path: Path) -> None:
    app = _app(config, _rules(tmp_path, ["no-such-decoder-xyz", "{path}"]))
    log = tmp_path / "a.ecu"
    log.write_text("x\n", encoding="utf-8")
    with pytest.raises(ConnectorError, match="program 'no-such-decoder-xyz' not found"):
        app.ingestor.add(app.config.case_db("m"), str(log))


def test_example_mcu_connector_and_marker_rule(config: Config, tmp_path: Path) -> None:
    rules = tmp_path / "rules"
    shutil.copytree(EXAMPLES / "connectors", rules)
    shutil.copy(EXAMPLES / "rules" / "markers_vhal_set.yaml", rules)
    app = _app(config, rules)
    assert [m.name for m in app.markers] == ["vhal_set_property"]
    hu = tmp_path / "headunit.log"
    hu.write_text(
        "10-01 08:00:01.000   900   900 I VehicleHal: setProperty prop=0x11400400 seq=17\n"
        "10-01 08:00:01.100   900   900 W VehicleHal: SET rejected by MCU\n",
        encoding="utf-8",
    )
    app.ingestor.add(app.config.case_db("mcu"), str(hu))
    app.ingestor.add(app.config.case_db("mcu"), str(rules / "mcu_0001.mcutrace"))
    tools = Tools(app)
    rows = app.list_sources("mcu") or []
    assert rows[1].format == "mcu_trace" and rows[1].meta.get("ecu") == "MCU1"
    out = tools.align("mcu").text
    # One marker pair is not enough for an automatic mapping (two are needed).
    assert "2  not placed" in out, out
    tools.align("mcu", "anchor", at="log:2#3", ref="log:1#1", offset="-2ms")  # at is 2 ms after ref
    tl = tools.timeline("mcu").text
    assert "log:2#3  ~10-01 08:00:01.002  E VHAL  SET prop=0x11400400 seq=17 rejected" in tl, tl
    assert "mcu_fault" in tools.list_artifacts("mcu").text


def test_marker_rule_file_errors(tmp_path: Path) -> None:
    from automotive_log_mcp.core.align import load_markers

    d = tmp_path / "r"
    d.mkdir()
    (d / "m.yaml").write_text(
        "marker: m\na: {match: 'x(?P<k>\\d)'}\nb: {match: 'y(?P<j>\\d)'}\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="same named groups"):
        load_markers([d])
    (d / "m.yaml").write_text(
        "marker: m\na: {match: 'x(?P<k>\\d)'}\nb: {match: 'y(?P<k>\\d)'}\ndelay: soon\n"
    )
    with pytest.raises(ConfigError, match="duration"):
        load_markers([d])


def test_marker_and_connector_files_do_not_break_detector_rules(config: Config, tmp_path: Path) -> None:
    rules = tmp_path / "rules"
    shutil.copytree(EXAMPLES / "connectors", rules)
    shutil.copy(EXAMPLES / "rules" / "markers_vhal_set.yaml", rules)
    app = _app(config, rules)
    assert "mcu_trace" in app.registry.names()
    assert app.rules.rules  # the package detector rules are still there


def test_ingest_error_for_unknown_forced_format(config: Config, tmp_path: Path) -> None:
    app = _app(config, _rules(tmp_path, ["{python}", "{dir}/ecu2jsonl.py", "{path}"]))
    log = tmp_path / "a.txt"
    log.write_text("x\n", encoding="utf-8")
    with pytest.raises(IngestError, match="unknown format"):
        app.ingestor.add(app.config.case_db("u"), str(log), format="ecu_lg")
    res = app.ingestor.add(app.config.case_db("u"), str(log), format="ecu_log")
    assert res.files[0].records == 1
    with db.opened(app.config.case_db("u")) as conn:
        assert conn.execute("SELECT count(*) FROM records").fetchone()[0] == 1
