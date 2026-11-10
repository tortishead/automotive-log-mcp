"""Logcat buffer markers: one source per buffer only when logcat printed dividers."""

from __future__ import annotations

from pathlib import Path

from automotive_log_mcp.app import App
from automotive_log_mcp.connectors import logcat
from automotive_log_mcp.store import db

HEADER = """========================================================
== dumpstate: 2026-01-01 00:10:00
========================================================
Build fingerprint: 'google/sdk_car_x86_64/emulator_car64_x86_64:14/UAA1/1:userdebug/test-keys'
"""

# What dumpstate writes: buffers merged by time, 'beginning of' once per buffer, no 'switch to'.
MERGED = """--------- beginning of main
12-31 23:59:58.100  1000  1234  1250 I HvacCtrl: setTemp 21.5 for zone 1
--------- beginning of system
12-31 23:59:59.000  1000  1000  1100 I ActivityManager: Start proc 1234
--------- beginning of crash
01-01 00:00:01.000  1000  1234  1250 E AndroidRuntime: FATAL EXCEPTION: main
01-01 00:00:02.000  1000  1000  1100 W ActivityManager: Process com.example.hvac (pid 1234) has died
01-01 00:00:03.000  1000  1301  1301 I HvacCtrl: bound to vehicle hal
"""


def _logcat_sources(app: App, case: str) -> list[tuple[str | None, int]]:
    return [(s.stream, s.records) for s in app.list_sources(case) or [] if s.format == "logcat"]


def test_bugreport_system_log_without_dividers_is_one_source(app: App, tmp_path: Path) -> None:
    p = tmp_path / "bugreport-x-2026-01-01-00-10-00.txt"
    p.write_text(
        HEADER
        + "------ SYSTEM LOG (logcat -v threadtime -v printable -v uid -d *:v) ------\n"
        + MERGED
        + "------ 0.1s was the duration of 'SYSTEM LOG' ------\n",
        encoding="utf-8",
    )
    app.ingestor.add(app.config.case_db("br"), str(p))
    assert _logcat_sources(app, "br") == [("main+system+crash", 5)]
    (src,) = [s for s in app.list_sources("br") or [] if s.format == "logcat"]
    assert src.name.endswith("#SYSTEM LOG:main+system+crash")
    # The FATAL EXCEPTION still becomes a java_crash artifact.
    with db.opened(app.config.case_db("br")) as conn:
        assert [r[0] for r in conn.execute("SELECT type FROM artifacts")] == ["java_crash"]


def test_bugreport_system_log_with_dividers_splits(app: App, tmp_path: Path) -> None:
    body = MERGED.replace("01-01 00:00:02.000", "--------- switch to system\n01-01 00:00:02.000").replace(
        "01-01 00:00:03.000", "--------- switch to main\n01-01 00:00:03.000"
    )
    p = tmp_path / "bugreport-x-2026-01-01-00-10-00.txt"
    p.write_text(
        HEADER
        + "------ SYSTEM LOG (logcat -D -v threadtime -d *:v) ------\n"
        + body
        + "------ 0.1s was the duration of 'SYSTEM LOG' ------\n",
        encoding="utf-8",
    )
    app.ingestor.add(app.config.case_db("br"), str(p))
    assert sorted(_logcat_sources(app, "br")) == [("crash", 1), ("main", 2), ("system", 2)]


def test_plain_logcat_without_dividers_is_one_source(app: App, tmp_path: Path) -> None:
    p = tmp_path / "logcat.txt"
    p.write_text(MERGED, encoding="utf-8")
    app.ingestor.add(app.config.case_db("c"), str(p))
    assert _logcat_sources(app, "c") == [("main+system+crash", 5)]


def test_plain_logcat_with_dividers_splits(app: App, tmp_path: Path) -> None:
    p = tmp_path / "logcat.txt"
    p.write_text(
        MERGED.replace("01-01 00:00:03.000", "--------- switch to main\n01-01 00:00:03.000"),
        encoding="utf-8",
    )
    app.ingestor.add(app.config.case_db("c"), str(p))
    # Records after 'beginning of crash' up to the switch are crash: the divider proves it.
    assert sorted(_logcat_sources(app, "c")) == [("crash", 2), ("main", 2), ("system", 1)]


def test_long_run_without_switch_counts_as_merged(app: App, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(logcat, "HOLD_RECORDS", 3)
    lines = [
        "--------- beginning of main",
        "01-01 00:00:00.000  1  1 I A: x",
        "--------- beginning of system",
    ]
    lines += [f"01-01 00:00:0{i}.000  1  1 I B: y{i}" for i in range(1, 6)]
    lines += ["--------- switch to main", "01-01 00:00:09.000  1  1 I A: z"]
    p = tmp_path / "logcat.txt"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    app.ingestor.add(app.config.case_db("c"), str(p))
    # Decided "merged" after 3 held records; the late switch then starts a real 'main' source.
    assert sorted(_logcat_sources(app, "c"), key=str) == [("main", 1), ("main+system", 6)]
