from __future__ import annotations

import gzip
import io
import json
import sqlite3
import tarfile
import threading
from pathlib import Path

import pytest

from automotive_log_mcp.app import App
from automotive_log_mcp.config import Config, ConfigError
from automotive_log_mcp.connectors import base, regex_yaml
from automotive_log_mcp.connectors.base import ConnectorError, Registry, build_registry
from automotive_log_mcp.core.ingest import IngestError
from automotive_log_mcp.model import RawRecord
from automotive_log_mcp.policy import Policy
from automotive_log_mcp.store import db

from .conftest import FIXTURES, copy_fixture
from .test_golden import make_bugreport_zip

LINE = "10-11 08:12:03.412  1234  1250 E HvacCtrl: setProperty failed vin=WVWZZZ1JZXW000001\n"


def count(app: App, case: str, sql: str = "SELECT count(*) FROM records") -> int:
    with db.opened(app.config.case_db(case)) as conn:
        return int(conn.execute(sql).fetchone()[0])


def test_same_file_twice_is_a_noop_and_force_reindexes(app: App, tmp_path: Path) -> None:
    p = copy_fixture("logcat/threadtime.log", tmp_path)
    db_path = app.config.case_db("c")
    first = app.ingestor.add(db_path, str(p)).files[0]
    again = app.ingestor.add(db_path, str(p)).files[0]
    assert again.existing and again.source_ids == first.source_ids
    n = count(app, "c")
    forced = app.ingestor.add(db_path, str(p), force=True).files[0]
    assert not forced.existing
    assert count(app, "c") == n
    assert count(app, "c", "SELECT count(*) FROM sources") == 3
    with db.opened(db_path) as conn:  # FTS stays in sync with records after delete + reindex
        assert (
            conn.execute("SELECT count(*) FROM records_fts WHERE records_fts MATCH 'HvacCtrl'").fetchone()[0]
            == 4
        )
        conn.execute("INSERT INTO records_fts(records_fts) VALUES ('integrity-check')")
        assert conn.execute("SELECT min(count) FROM templates").fetchone()[0] >= 1


def test_record_counts_match_input_within_multiline_merges(app: App, tmp_path: Path) -> None:
    p = copy_fixture("logcat/threadtime.log", tmp_path)
    app.ingestor.add(app.config.case_db("c"), str(p))
    physical = p.read_text(encoding="utf-8").splitlines()
    markers = sum(1 for ln in physical if ln.startswith("--------- "))
    rows = app.list_sources("c") or []
    assert sum(s.lines for s in rows) + markers == len(physical)
    assert sum(s.records for s in rows) == count(app, "c")


def test_unknown_format_names_top_guesses(app: App, tmp_path: Path) -> None:
    p = tmp_path / "notes.txt"
    p.write_text("hello\nworld\n", encoding="utf-8")
    with pytest.raises(IngestError, match=r"unknown format.*top guesses: \w+ 0\.\d\d, \w+ 0\.\d\d, \w+"):
        app.ingestor.add(app.config.case_db("c"), str(p))
    forced = app.ingestor.add(app.config.case_db("c"), str(p), format="logcat").files[0]
    assert forced.records == 2  # unparseable lines are kept as records, never a failed import
    assert count(app, "c", "SELECT count(*) FROM records WHERE level IS NULL") == 2


def test_missing_file_and_unknown_forced_format(app: App, tmp_path: Path) -> None:
    with pytest.raises(IngestError, match="no such file"):
        app.ingestor.add(app.config.case_db("c"), str(tmp_path / "nope.log"))
    p = copy_fixture("logcat/time.log", tmp_path)
    with pytest.raises(IngestError, match="unknown format 'dlt'"):
        app.ingestor.add(app.config.case_db("c"), str(p), format="dlt")


def test_archives_tar_gz_and_directory(app: App, tmp_path: Path) -> None:
    gz = tmp_path / "main.log.gz"
    gz.write_bytes(gzip.compress(LINE.encode()))
    tgz = tmp_path / "logs.tar.gz"
    with tarfile.open(tgz, "w:gz") as tf:
        for name, rel in [("a/kernel.txt", "kernel/dmesg.txt"), ("a/b/logcat.txt", "logcat/uid.log")]:
            data = (FIXTURES / rel).read_bytes()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 1790000000
            tf.addfile(info, io.BytesIO(data))
    r1 = app.ingestor.add(app.config.case_db("c"), str(gz)).files[0]
    assert r1.records == 1
    r2 = app.ingestor.add(app.config.case_db("c"), str(tgz)).files[0]
    names = {s.name: s.format for s in app.list_sources("c") or []}
    assert names["logs.tar.gz!a/kernel.txt"] == "kernel"
    assert names["logs.tar.gz!a/b/logcat.txt"] == "logcat"
    assert r2.records == 9
    d = tmp_path / "dir"
    d.mkdir()
    (d / "x.log").write_text(LINE, encoding="utf-8")
    (d / "readme.md").write_text("# not a log\n", encoding="utf-8")
    res = app.ingestor.add(app.config.case_db("d"), str(d))
    assert len(res.files) == 1 and any("readme.md" in s for s in res.skipped)


def test_bugreport_processes_and_meta(app: App, tmp_path: Path) -> None:
    z = make_bugreport_zip(tmp_path)
    app.ingestor.add(app.config.case_db("br"), str(z))
    with db.opened(app.config.case_db("br")) as conn:
        procs = conn.execute("SELECT pid, tid, ppid, user, name FROM processes ORDER BY pid").fetchall()
    assert procs == [
        (1, 1, 0, "root", "init"),
        (1000, 1000, 500, "system", "system_server"),
        (1234, 1250, 500, "u0_a42", "hvac worker"),
    ]
    rows = app.list_sources("br") or []
    streams = {s.stream for s in rows if "#" in s.name and s.format in ("logcat", "kernel")}
    assert streams == {"main", "system", "crash", "events", "kernel"}
    meta = next(s.meta for s in rows if s.stream == "kernel")
    assert meta["timezone"] == "Europe/Berlin"
    assert "boot_time_estimate" in meta


def test_after_indexing_no_rule_matches_records_templates_or_meta(app: App, tmp_path: Path) -> None:
    z = make_bugreport_zip(tmp_path)
    app.ingestor.add(app.config.case_db("br"), str(z))
    for rel in (
        "logcat/threadtime.log",
        "kernel/kmsg.txt",
        "syslog/syslog.log",
        "tombstone/tombstone_01",
        "anr/anr_2026-01-01-00-04-10-500",
    ):
        app.ingestor.add(app.config.case_db("br"), str(copy_fixture(rel, tmp_path)))
    r = app.redactor
    with db.opened(app.config.case_db("br")) as conn:
        texts = [t for (t,) in conn.execute("SELECT text FROM templates")]
        texts += [m for (m,) in conn.execute("SELECT msg FROM records")]
        texts += [a for (a,) in conn.execute("SELECT attrs FROM records WHERE attrs IS NOT NULL")]
        texts += [m for (m,) in conn.execute("SELECT meta FROM sources WHERE meta IS NOT NULL")]
        texts += [n for (n,) in conn.execute("SELECT name FROM processes")]
        # Artifacts (M2): everything they store as text.
        for row in conn.execute(
            "SELECT name, member, ts_raw, process, signature, headline, summary, fields FROM artifacts"
        ):
            texts += [t for t in row if t]
        texts += [t for row in conn.execute("SELECT name, text FROM artifact_sections") for t in row]
        n_art = conn.execute("SELECT count(*) FROM artifacts").fetchone()[0]
        n_tokens = sum(t.count("<MAC:") + t.count("<SSID:") + t.count("<IP:") for t in texts)
        n_vin = sum(t.count("<VIN:") for t in texts)
    assert n_tokens >= 3  # the fixtures do contain values to redact
    assert n_art >= 7 and n_vin >= 4  # the abort message's VIN: headline, summary, fields, section
    leaks = [(t, r.find(t)) for t in texts if r.find(t)]
    assert leaks == []
    assert not any(
        "EMULATOR35X1X11X0" in t or "3c:5a:b4" in t or "HomeNet" in t or "WVWZZZ1JZXW000001" in t
        for t in texts
    )


def test_failed_import_marks_sources_failed_and_keeps_no_rows(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "big.log"
    p.write_text(LINE * 30, encoding="utf-8")
    from automotive_log_mcp.connectors import logcat

    orig = logcat.LogcatConnector.parse_lines

    def broken(self, lines, ctx):  # type: ignore[no-untyped-def]
        for i, item in enumerate(orig(self, lines, ctx)):
            if i == 20:
                raise RuntimeError("disk on fire at vin WVWZZZ1JZXW000001")
            yield item

    monkeypatch.setattr(logcat.LogcatConnector, "parse_lines", broken)
    monkeypatch.setattr(db, "BATCH_ROWS", 5)
    with pytest.raises(RuntimeError):
        app.ingestor.add(app.config.case_db("c"), str(p))
    (src,) = app.list_sources("c") or []
    assert src.status == "failed" and src.error is not None
    assert "WVWZZZ" not in src.error  # errors are redacted too
    assert count(app, "c") == 0
    monkeypatch.setattr(logcat.LogcatConnector, "parse_lines", orig)
    again = app.ingestor.add(app.config.case_db("c"), str(p)).files[0]  # a failed file is redone
    assert again.records == 30 and not again.existing


def test_batches_are_committed_while_indexing(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Background indexing: another connection sees committed rows before the import ends."""
    p = tmp_path / "big.log"
    p.write_text(LINE * 40, encoding="utf-8")
    from automotive_log_mcp.connectors import logcat

    orig = logcat.LogcatConnector.parse_lines
    reached = threading.Event()
    go_on = threading.Event()

    def slow(self, lines, ctx):  # type: ignore[no-untyped-def]
        for i, item in enumerate(orig(self, lines, ctx)):
            if i == 25:
                reached.set()
                go_on.wait(10)
            yield item

    monkeypatch.setattr(logcat.LogcatConnector, "parse_lines", slow)
    monkeypatch.setattr(db, "BATCH_ROWS", 10)
    fut = app.workers.submit("c", app.config.case_db("c"), str(p), None)
    assert reached.wait(10)
    assert app.workers.busy("c")
    (src,) = app.list_sources("c") or []
    assert src.status == "indexing" and src.records == 20 and count(app, "c") == 20
    go_on.set()
    assert fut.result(10).files[0].records == 40
    (src,) = app.list_sources("c") or []
    assert src.status == "done" and src.records == 40


def test_blocked_sources(config: Config, tmp_path: Path) -> None:
    policy = Policy(blocked_sources=("*private*", "*FS/data/misc/*"))
    app = App.create(config, policy)
    priv = tmp_path / "private.log"
    priv.write_text(LINE, encoding="utf-8")
    with pytest.raises(IngestError, match="blocked"):
        app.ingestor.add(config.case_db("c"), str(priv))
    z = make_bugreport_zip(tmp_path)
    res = app.ingestor.add(config.case_db("c"), str(z)).files[0]
    assert any("blocked by policy" in n for n in res.notes)
    assert not any("misc" in s.name for s in app.list_sources("c") or [])
    # Sources indexed before a policy change are hidden afterwards.
    open_app = App.create(config, Policy())
    open_app.ingestor.add(config.case_db("c"), str(copy_fixture("logcat/time.log", tmp_path)))
    hidden = App.create(config, Policy(blocked_sources=("*time.log*",)))
    assert not any("time.log" in s.name for s in hidden.list_sources("c") or [])


def test_relink_checks_hash_and_missing_original(app: App, tmp_path: Path) -> None:
    p = copy_fixture("logcat/time.log", tmp_path)
    (sid,) = app.ingestor.add(app.config.case_db("c"), str(p)).source_ids
    moved = tmp_path / "moved.log"
    p.rename(moved)
    (src,) = app.list_sources("c") or []
    assert src.original_missing
    with pytest.raises(IngestError, match="missing"):
        app.ingestor.reindex(app.config.case_db("c"), sid)
    other = tmp_path / "other.log"
    other.write_text(LINE, encoding="utf-8")
    with pytest.raises(IngestError, match="SHA-256"):
        app.relink("c", sid, other)
    assert app.relink("c", sid, moved) == [sid]
    (src,) = app.list_sources("c") or []
    assert not src.original_missing and src.path == str(moved.resolve())
    assert app.ingestor.reindex(app.config.case_db("c"), sid).files[0].records == 2


def test_stale_when_connector_version_changes(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app.ingestor.add(app.config.case_db("c"), str(copy_fixture("logcat/time.log", tmp_path)))
    assert not (app.list_sources("c") or [])[0].stale
    monkeypatch.setattr(app.registry.get("logcat"), "version", "next")
    assert (app.list_sources("c") or [])[0].stale


def test_schema_migration_guard(app: App, tmp_path: Path) -> None:
    path = app.config.case_db("c")
    app.ingestor.add(path, str(copy_fixture("logcat/time.log", tmp_path)))
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    conn.close()
    with pytest.raises(db.StoreError, match="newer"):
        db.connect(path)


def test_cli_search_finds_redacted_value(app: App, tmp_path: Path) -> None:
    p = tmp_path / "v.log"
    p.write_text(LINE, encoding="utf-8")
    app.ingestor.add(app.config.case_db("c"), str(p))
    from automotive_log_mcp.tools import Filter, Tools

    out = Tools(app).search("c", "WVWZZZ1JZXW000001", filter=Filter.model_validate({"sources": ["1"]}))
    data = out.data.model_dump()
    assert data["total"] == 1 and data["hits"][0]["cite"] == "log:1#1"
    assert "log:1#1  10-11 08:12:03.412" in out.text and "WVWZZZ1JZXW000001" not in out.text


# -- registry and YAML connectors --------------------------------------------------------------


def write_yaml(d: Path, name: str, text: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


VENDOR = """\
connector: vendor_hvac
line: '^(?P<ts>\\d{4}-\\d\\d-\\d\\d \\d\\d:\\d\\d:\\d\\d\\.\\d{3}) \\[(?P<level>\\w+)\\] (?P<ecu>\\w+)/(?P<tag>\\w+): (?P<msg>.*)$'
timestamp: {format: '%Y-%m-%d %H:%M:%S.%f'}
levels: {ERR: E, INF: I}
continuation: '^\\s+'
"""


def test_yaml_connector_from_rule_dir(home: Path, tmp_path: Path) -> None:
    rules = tmp_path / "rules"
    write_yaml(rules, "vendor.yaml", VENDOR)
    # detector rules in the same directory are not connectors
    write_yaml(rules, "detector.yaml", "rule: not_a_connector\nseverity: low\ndescription: x\nmatch: x\n")
    from automotive_log_mcp.config import load_config

    cfg_file = tmp_path / "c.yaml"
    cfg_file.write_text(f"rule_dirs: [{rules.as_posix()}]\n", encoding="utf-8")
    app = App.create(load_config(cfg_file), Policy())
    log = tmp_path / "hvac.vlog"
    log.write_text(
        "2026-10-11 08:12:03.412 [ERR] ECU1/Hvac: setProperty failed\n   detail line\n"
        "2026-10-11 08:12:04.000 [INF] ECU1/Hvac: ok\n",
        encoding="utf-8",
    )
    f = app.ingestor.add(app.config.case_db("c"), str(log)).files[0]
    assert f.records == 2
    with db.opened(app.config.case_db("c")) as conn:
        rows = conn.execute("SELECT level, tag, msg, attrs FROM records_v ORDER BY id").fetchall()
    assert rows[0] == (
        "E",
        "Hvac",
        "setProperty failed\n   detail line",
        json.dumps({"ecu": "ECU1"}, separators=(",", ":")),
    )


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("connector: Bad Name\nline: '(?P<msg>.*)'\n", "lowercase"),
        ("connector: x\nline: '(.*)'\n", "named group 'msg'"),
        ("connector: x\nline: '(?P<msg>(.*)'\n", "not a valid regex"),
        ("connector: x\nline: '(?P<msg>.*)'\nextra: 1\n", "unknown keys"),
        ("connector: x\nline: '(?P<ts>\\S+) (?P<msg>.*)'\n", "timestamp.format"),
        ("connector: x\nline: '(?P<msg>.*)'\nlevels: {X: Q}\n", "not one of"),
    ],
)
def test_yaml_connector_validation(tmp_path: Path, text: str, match: str) -> None:
    write_yaml(tmp_path, "c.yaml", text)
    with pytest.raises(ConnectorError, match=match):
        regex_yaml.load_dirs(Registry(), [tmp_path])


def test_yaml_connector_name_clash_needs_replace(tmp_path: Path) -> None:
    write_yaml(tmp_path, "c.yaml", "connector: logcat\nline: '(?P<msg>.*)'\n")
    with pytest.raises(ConnectorError, match="replace: true"):
        build_registry([tmp_path])
    write_yaml(tmp_path, "c.yaml", "connector: logcat\nline: '(?P<msg>.*)'\nreplace: true\n")
    reg = build_registry([tmp_path])
    assert isinstance(reg.get("logcat"), regex_yaml.RegexConnector)


def test_missing_rule_dir_is_a_config_error(home: Path, tmp_path: Path) -> None:
    from automotive_log_mcp.config import load_config

    cfg_file = tmp_path / "c.yaml"
    cfg_file.write_text("rule_dirs: [nope]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="does not exist"):
        App.create(load_config(cfg_file), Policy())


class _EpConnector(base.TextConnector):
    name = "ep_text"
    version = "1"

    def sniff(self, head: bytes, filename: str) -> float:
        return 0.0

    def parse_lines(self, lines, ctx):  # type: ignore[no-untyped-def]
        for line in lines:
            yield RawRecord(msg=line)


def test_entry_point_connectors_only_when_policy_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    class EP:
        name = "ep_text"
        value = "pkg:Conn"

        def load(self) -> type:
            return _EpConnector

    monkeypatch.setattr(base.metadata, "entry_points", lambda group: [EP()])
    assert "ep_text" not in build_registry().names()
    assert "ep_text" in build_registry(allow_code_connectors=True).names()
