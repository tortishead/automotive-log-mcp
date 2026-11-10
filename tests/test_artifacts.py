"""M2 artifacts: parsers, signatures, the record on the timeline, citations, redaction, schema."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from automotive_log_mcp.app import App
from automotive_log_mcp.connectors.artifacts import anr, dropbox, java_crash, tombstone
from automotive_log_mcp.connectors.base import SourceContext
from automotive_log_mcp.model import RawRecord
from automotive_log_mcp.store import db, query

from .conftest import FIXTURES, copy_fixture
from .test_golden import make_bugreport_zip


def lines(rel: str) -> list[str]:
    return (FIXTURES / rel).read_text(encoding="utf-8").splitlines()


@pytest.fixture
def br(app: App, tmp_path: Path) -> App:
    app.ingestor.add(app.config.case_db("br"), str(make_bugreport_zip(tmp_path)))
    return app


def by_type(app: App, case: str = "br") -> dict[str, list[query.ArtifactRow]]:
    out: dict[str, list[query.ArtifactRow]] = {}
    for a in app.list_artifacts(case) or []:
        out.setdefault(a.type, []).append(a)
    return out


# -- parsers ----------------------------------------------------------------------------------


def test_tombstone_signal() -> None:
    a = tombstone.from_lines(lines("tombstone/tombstone_00"))
    assert (a.type, a.pid, a.tid, a.level) == ("tombstone", 812, 830, "F")
    assert a.fields["signal"] == {
        "number": 11,
        "name": "SIGSEGV",
        "code": 1,
        "code_name": "SEGV_MAPERR",
        "fault_addr": "0x0000000000000010",
    }
    assert a.fields["cause"] == "null pointer dereference"
    assert a.ts_raw == "2026-01-01 00:00:05.123456789+0100" and a.fields["utc_offset"] == "+0100"
    assert len(a.fields["frames"]) == 5 and a.fields["other_threads"] == 1
    assert [s.name for s in a.sections] == [
        "header",
        "registers",
        "backtrace",
        "memory",
        "memory_map",
        "threads",
        "open_files",
        "log",
    ]
    assert a.line_count == len(lines("tombstone/tombstone_00"))  # sections cover the file
    assert a.signature == (
        "SIGSEGV android.hardware.automotive.vehicle@V1-example-service: "
        "libvhal_example.so!VehicleHal::setValue < libvhal_example.so!VehicleHal::onSetValues "
        "< libbinder_ndk.so!AIBinder_Class_onTransact"
    )


def test_tombstone_abort_skips_abort_frames_in_signature() -> None:
    a = tombstone.from_lines(lines("tombstone/tombstone_01"))
    assert a.fields["signal"]["name"] == "SIGABRT" and "fault_addr" not in a.fields["signal"]
    assert a.fields["abort_message"].startswith("Check failed: stream != nullptr")
    assert a.signature == (
        "SIGABRT vendor.example.audio-service: libaudiohal_example.so!AudioStream::open "
        "< libaudiohal_example.so!AudioHal::startZone"
    )
    assert "Abort message 'Check failed" in a.headline


def test_tombstone_without_symbols_uses_offsets() -> None:
    text = [
        tombstone.BANNER,
        "pid: 5, tid: 6, name: x  >>> /system/bin/x <<<",
        "signal 7 (SIGBUS), code 2 (BUS_ADRERR), fault addr 0x1000",
        "backtrace:",
        "      #00 pc 0000000000001a2c  /system/lib64/libfoo.so",
    ]
    a = tombstone.from_lines(text)
    assert a.signature == "SIGBUS x: libfoo.so+0x1a2c"
    assert a.ts is None


def test_anr_lock_holder_and_native_process() -> None:
    a = anr.from_lines(lines("anr/anr_2026-01-01-00-04-10-500"))
    f = a.fields
    assert (a.process, a.pid, a.level) == ("com.example.media", 2345, "E")
    assert f["subject"].startswith("Input dispatching timed out")
    assert f["main"]["state"] == "Blocked" and f["main"]["sys_tid"] == 2345
    assert f["main"]["waiting"] == {
        "lock": "0x0e3f5a21",
        "class": "com.example.media.Library",
        "holder_tid": 15,
    }
    holder = f["lock_chain"][0]
    assert (holder["tid"], holder["name"], holder["state"], holder["sys_tid"]) == (
        15,
        "MediaScanner",
        "Sleeping",
        2370,
    )
    assert "deadlock" not in f and "binder" not in f
    assert f["other_processes"] == [
        {"pid": 812, "process": "/vendor/bin/hw/android.hardware.automotive.vehicle@V1-example-service"}
    ]
    names = [s.name for s in a.sections]
    assert names[:3] == ["header", "p2345", "p2345.t1.main"]
    assert "p812.t830.vehiclehal" in names
    assert a.signature == (
        "ANR Blocked com.example.media: com.example.media.Library.currentTrack "
        "< com.example.media.PlayerActivity.onTouch < android.view.View.dispatchTouchEvent"
    )
    assert 'lock held by tid 15 "MediaScanner" (Sleeping)' in a.headline


def test_anr_deadlock() -> None:
    text = """\
----- pid 7 at 2026-01-01 00:00:00 -----
Cmd line: com.example.dead
"main" prio=5 tid=1 Blocked
  at a.A.f(A.java:1)
  - waiting to lock <0x1> (a a.A) held by thread 2
  - locked <0x2> (a a.B)
"w" prio=5 tid=2 Blocked
  at a.B.g(B.java:1)
  - waiting to lock <0x2> (a a.B) held by thread 1
  - locked <0x1> (a a.A)
----- end 7 -----""".splitlines()
    a = anr.from_lines(text)
    assert a.fields["deadlock"] is True
    assert [h["tid"] for h in a.fields["lock_chain"]] == [2, 1]
    assert "deadlock" in a.headline and a.ts is not None


def test_java_crash_from_logcat_record() -> None:
    rec = RawRecord(
        msg="*** FATAL EXCEPTION IN SYSTEM PROCESS: main\njava.lang.IllegalStateException: no car service\n"
        "\tat com.android.car.CarServiceImpl.onCreate(CarServiceImpl.java:120)",
        ts=5,
        pid=1000,
        tid=1000,
        lines=3,
    )
    a = java_crash.from_record(rec)
    assert a is not None and a.process == "system_server" and a.fields["thread"] == "main"
    assert (
        a.signature
        == "java.lang.IllegalStateException system_server: com.android.car.CarServiceImpl.onCreate"
    )
    assert java_crash.from_record(RawRecord(msg="not a crash")) is None


def test_dropbox_routing_and_times() -> None:
    assert [
        dropbox.body_type(t) for t in ("data_app_crash", "system_server_anr", "data_app_native_crash")
    ] == [
        "java_crash",
        "anr",
        "tombstone",
    ]
    assert dropbox.body_type("system_server_wtf") == "dropbox"
    _, raw, zone = dropbox.local_time(1767222001000, None)
    assert (raw, zone) == ("2025-12-31 23:00:01.000", "UTC")
    _, raw, zone = dropbox.local_time(1767222001000, "Europe/Berlin")
    assert (raw, zone) == ("2026-01-01 00:00:01.000", "Europe/Berlin")
    assert dropbox.local_time(1767222001000, "Not/AZone")[2] == "UTC"


def test_dumpsys_dropbox_section_entries() -> None:
    text = """\
Drop box contents: 2 entries

========================================
2026-01-01 00:01:00 system_app_anr (text, 100 bytes)
Process: com.example.a
PID: 11
Subject: Input dispatching timed out

----- pid 11 at 2026-01-01 00:00:59 -----
Cmd line: com.example.a
"main" prio=5 tid=1 Native
  at android.os.BinderProxy.transactNative(Native method)
  at com.example.IFoo$Stub$Proxy.bar(IFoo.java:3)
----- end 11 -----

========================================
2026-01-01 00:02:00 SYSTEM_BOOT (text, 10 bytes)
Boot info: normal
""".splitlines()
    conn = dropbox.DropboxConnector()
    arts = list(conn.parse_lines(iter(text), SourceContext("s", "s", 2026)))
    assert [a.type for a in arts] == ["anr", "dropbox"]
    a0, a1 = arts
    assert a0.ts_raw == "2026-01-01 00:00:59"  # the trace's own dump time
    assert a0.fields["binder"]["interface"] == "com.example.IFoo" and a0.fields["binder"]["method"] == "bar"
    assert a0.fields["subject"] == "Input dispatching timed out"
    assert a0.sections[0].name == "entry" and a0.sections[1].name == "dropbox_header"
    assert a1.fields["first_line"] == "Boot info: normal" and a1.level == "W"


# -- in a case ------------------------------------------------------------------------------------


def test_bugreport_artifacts_are_on_the_timeline(br: App) -> None:
    arts = br.list_artifacts("br") or []
    types = by_type(br)
    assert {t: len(v) for t, v in types.items()} == {"java_crash": 3, "tombstone": 2, "dropbox": 1, "anr": 1}
    with db.opened(br.config.case_db("br")) as conn:
        for a in arts:
            assert a.ts is not None, a.name
            row = conn.execute("SELECT ts, attrs, msg FROM records_v WHERE id = ?", (a.record_id,)).fetchone()
            assert row is not None and row[0] == a.ts
            assert f'"art":{a.id}' in row[1]
            if a.record_cite != "log:3#1":  # the crash-buffer record is the crash itself
                assert row[2].startswith(f"[art:{a.id}] ")
        # Artifact text is searchable.
        hits = conn.execute(
            "SELECT rowid FROM artifact_fts WHERE artifact_fts MATCH 'onSetValues'"
        ).fetchall()
        assert hits
    crash = next(a for a in types["java_crash"] if a.record_cite == "log:3#1")
    assert crash.process == "com.example.hvac" and crash.fields["origin"] == "logcat"


def test_bugreport_anr_binder_peer_and_dropbox_time(br: App) -> None:
    (a,) = by_type(br)["anr"]
    assert a.fields["binder"]["peer"] == {
        "pid": 1000,
        "tid": 1100,
        "process": "system_server",
        "from": "BINDER TRANSACTIONS",
    }
    assert a.fields["lock_chain"][0]["name"] == "RadioWorker"
    assert "-> pid 1000 (system_server)" in a.summary
    gz = next(x for x in by_type(br)["java_crash"] if x.fields.get("dropbox_tag") == "data_app_crash")
    assert query.fmt_ns(gz.ts) == "2026-01-01 00:00:01.000" and gz.fields["ts_zone"] == "Europe/Berlin"
    rows = {s.name: s for s in br.list_sources("br") or []}
    # Metadata of the bugreport text applies to the other members of the zip.
    tomb = next(s for n, s in rows.items() if n.endswith("tombstone_00"))
    assert tomb.fingerprint is not None and tomb.meta["timezone"] == "Europe/Berlin"
    anr_src = next(s for n, s in rows.items() if n.endswith("VM TRACES AT LAST ANR"))
    assert anr_src.meta["section_file"] == "/data/anr/anr_2026-01-01-00-05-00-000"


def test_citations_resolve(br: App) -> None:
    t = next(a for a in by_type(br)["tombstone"] if a.pid == 812)
    a, lines_ = br.read_artifact("br", t.cite)
    assert lines_ is None and [s.name for s in a.sections][:3] == ["header", "registers", "backtrace"]
    bt = next(s for s in a.sections if s.name == "backtrace")
    _, sec = br.read_artifact("br", f"{t.cite}/backtrace")
    assert sec is not None and sec[0] == (bt.first_line, "backtrace:") and len(sec) == bt.lines
    original = lines("tombstone/tombstone_00")
    n = bt.first_line + 1
    for cite in (f"{t.cite}/backtrace:L{n}", f"{t.cite}:L{n}", f"{t.id}:L{n}"):
        _, one = br.read_artifact("br", cite)
        assert one == [(n, original[n - 1])]  # L<n> is line n of the original file
    _, rng = br.read_artifact("br", f"{t.cite}:L1-L3")
    assert [x[0] for x in rng or []] == [1, 2, 3]
    for bad, match in [
        (f"{t.cite}/nope", "no section 'nope'"),
        (f"{t.cite}/log:L2", "outside section 'log'"),
        (f"{t.cite}:L9999", "outside"),
        ("art:999", "no artifact art:999"),
        ("art:x", "invalid artifact citation"),
    ]:
        with pytest.raises(ValueError, match=match):
            br.read_artifact("br", bad)


def test_policy_hides_artifacts_of_blocked_sources(br: App) -> None:
    from dataclasses import replace

    br.policy = replace(br.policy, blocked_sources=("*tombstones*",))
    assert "tombstone" not in by_type(br)
    t = next(a for a in (br.list_artifacts("br") or []) if a.type == "anr")
    assert br.read_artifact("br", t.cite)[0].id == t.id


def test_reindex_replaces_artifacts(br: App, tmp_path: Path) -> None:
    before = [(a.type, a.signature) for a in br.list_artifacts("br") or []]
    path = next(s.path for s in br.list_sources("br") or [])
    br.ingestor.add(br.config.case_db("br"), path, force=True)
    after = [(a.type, a.signature) for a in br.list_artifacts("br") or []]
    assert sorted(after, key=str) == sorted(before, key=str)
    with db.opened(br.config.case_db("br")) as conn:
        assert conn.execute("SELECT count(*) FROM artifacts").fetchone()[0] == len(before)
        orphans = conn.execute(
            "SELECT count(*) FROM artifact_sections WHERE artifact_id NOT IN (SELECT id FROM artifacts)"
        ).fetchone()[0]
        assert orphans == 0
        conn.execute("INSERT INTO artifact_fts(artifact_fts) VALUES ('integrity-check')")


def test_standalone_artifact_files(app: App, tmp_path: Path) -> None:
    for rel in (
        "tombstone/tombstone_01",
        "anr/anr_2026-01-01-00-04-10-500",
        "dropbox/data_app_crash@1767222001000.txt",
    ):
        app.ingestor.add(app.config.case_db("c"), str(copy_fixture(rel, tmp_path)))
    got = {a.type: a for a in app.list_artifacts("c") or []}
    assert set(got) == {"tombstone", "anr", "java_crash"}
    assert got["java_crash"].fields["ts_zone"] == "UTC"  # no bugreport, so no device time zone
    assert {s.format for s in app.list_sources("c") or []} == {"tombstone", "anr", "dropbox"}


def test_v1_case_db_is_migrated(app: App, tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    try:
        v1 = resources.files("automotive_log_mcp.store").joinpath("schema.sql").read_text(encoding="utf-8")
        conn.executescript(v1)
        conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '1')")
        conn.execute("INSERT INTO templates(id, text, count) VALUES (1, 'kept', 1)")
        conn.commit()
    finally:
        conn.close()
    with db.opened(path) as c:
        assert c.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == str(
            db.SCHEMA_VERSION
        )
        assert c.execute("SELECT text FROM templates").fetchall() == [("kept",)]
        cols = {r[1] for r in c.execute("PRAGMA table_info(artifact_sections)")}
        assert {"first_line", "lines"} <= cols


# Fragments of real layouts, shuffled with arbitrary text: parsers must never raise.
FRAGMENTS = [
    tombstone.BANNER,
    "pid: 1, tid: 2, name: a  >>> b <<<",
    "signal 6 (SIGABRT), code -1 (SI_QUEUE), fault addr --------",
    "backtrace:",
    "      #00 pc 0000000000001a2c  /system/lib64/libfoo.so (f(int)+4)",
    "--- --- --- --- --- --- --- --- --- --- --- --- --- --- --- ---",
    "----- pid 3 at 2026-13-45 99:99:99 -----",
    "----- end 3 -----",
    '"main" prio=5 tid=1 Blocked',
    "  - waiting to lock <0x1> (a X) held by thread 9",
    "  - locked <0x1> (a X)",
    "  at a.B.c(B.java:1)",
    "Caused by: java.lang.Error",
    "========================================",
    "2026-01-01 00:00:00 data_app_crash (text, 1 bytes)",
    "Process: x",
    "",
]


@settings(max_examples=150, deadline=None)
@given(st.lists(st.one_of(st.sampled_from(FRAGMENTS), st.text(max_size=40)), max_size=30))
def test_parsers_never_raise(text: list[str]) -> None:
    text = [t.replace("\n", " ").replace("\r", " ") for t in text]
    # Sections cover the text exactly, so art:<id>:L<n> is line n of the input.
    assert tombstone.from_lines(text).line_count == len(text)
    assert anr.from_lines(text).line_count == len(text)
    java_crash.from_record(RawRecord(msg="FATAL EXCEPTION: main\n" + "\n".join(text)))
    for tag in ("data_app_crash", "system_app_anr", "data_app_native_crash", "other"):
        dropbox.parse_entry(tag, text, None, None)
    list(dropbox.DropboxConnector().parse_lines(iter(text), SourceContext("s", "s", 2026)))
