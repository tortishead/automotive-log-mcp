from __future__ import annotations

from pathlib import Path

import pytest

from automotive_log_mcp.cli import main

from .conftest import FIXED_SECRET, copy_fixture
from .test_golden import make_bugreport_zip


@pytest.fixture
def secret(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "secret.key").write_bytes(FIXED_SECRET)


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_index_query_sources(
    home: Path, tmp_path: Path, secret: None, capsys: pytest.CaptureFixture[str]
) -> None:
    z = make_bugreport_zip(tmp_path)
    code, out, err = run(capsys, "index", "T-1", str(z))
    assert code == 0, err
    # The zip's main entry (the bugreport text) is read first: its sources get the first ids.
    assert "sources 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12\n" in out and "lines/s" in out
    assert "note:" in out and "tombstone_01.pb" in out
    assert "hashing" in err  # progress on stderr

    code, out, _ = run(capsys, "index", "T-1", str(z), "-q")
    assert "already in case" in out

    code, out, _ = run(capsys, "sources", "T-1")
    assert code == 0 and "SYSTEM LOG:crash" in out and "KERNEL LOG:kernel" in out

    code, out, _ = run(capsys, "query", "T-1", "--search", "NullPointerException")
    assert code == 0
    assert out.splitlines()[0] == "2 matches:"
    assert out.splitlines()[1].startswith("log:3#1  01-01 00:00:01.000  E 1234/1250 AndroidRuntime  FATAL")

    code, out, _ = run(capsys, "query", "T-1", "--level", "E", "--count")
    assert out.strip() == "8"  # 3 log lines + the records of 5 artifacts (ANR E, crashes F)
    code, out, _ = run(capsys, "query", "T-1", "--tag", "Hvac*", "--limit", "1")
    assert out.splitlines()[1].startswith("log:1#1  12-31 23:59:58.100  I 1234/1250 HvacCtrl")
    assert "… 1 more match; next page: cursor=" in out
    code, out, _ = run(capsys, "query", "T-1", "--regex", r"status=\w+_AVAIL")
    assert "log:1#2" in out
    code, out, _ = run(capsys, "query", "T-1", "--search", "3c:5a:b4:01:02:03")  # raw value -> token
    assert "log:5#3" in out and "<MAC:" in out
    code, out, _ = run(capsys, "query", "T-1", "--search", "nothing_like_this")
    assert "No matches." in out and "no record contains 'nothing_like_this'" in out
    code, out, _ = run(
        capsys, "query", "T-1", "--level", "W", "--from=01-01 00:00:01", "--to=+2s", "--collapse"
    )
    assert code == 0 and out.splitlines()[0] == "3 matches, 3 distinct messages (collapsed):"
    assert "log:2#1" in out and "log:1#2" not in out


def test_errors(home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "sources", "nope")
    assert code == 1 and "does not exist" in err
    code, _, err = run(capsys, "index", "c", str(tmp_path / "missing.log"))
    assert code == 1 and "no such file" in err
    with pytest.raises(SystemExit):
        main(["index", "../bad", "x"])
    p = copy_fixture("logcat/time.log", tmp_path)
    run(capsys, "index", "c", str(p), "-q")
    code, _, err = run(capsys, "query", "c", "--regex", "(")
    assert code == 2 and "invalid regex" in err
    code, _, err = run(capsys, "query", "c", "--source", "99")
    assert code == 2 and "unknown source" in err


def test_relink_and_reindex(home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = copy_fixture("logcat/time.log", tmp_path)
    run(capsys, "index", "c", str(p), "-q")
    moved = tmp_path / "moved.log"
    p.rename(moved)
    code, out, _ = run(capsys, "sources", "c")
    assert "original missing" in out
    code, _, err = run(capsys, "reindex", "c", "1", "-q")
    assert code == 1 and "relink" in err
    code, out, _ = run(capsys, "relink", "c", "1", str(moved))
    assert code == 0 and "relinked sources 1" in out
    code, out, _ = run(capsys, "reindex", "c", "1", "-q")
    assert code == 0 and "reindexed" in out


def test_artifacts_command(
    home: Path, tmp_path: Path, secret: None, capsys: pytest.CaptureFixture[str]
) -> None:
    z = make_bugreport_zip(tmp_path)
    assert run(capsys, "index", "T-1", str(z), "-q")[0] == 0
    code, out, _ = run(capsys, "artifacts", "T-1")
    assert code == 0
    rows = out.splitlines()
    assert len(rows) == 7
    assert rows[0].startswith("art:1   java_crash 2026-01-01 00:00:01.000  pid 1234   com.example.hvac  ")
    tomb = next(r for r in rows if "SIGSEGV" in r)
    cite = tomb.split()[0]

    code, out, _ = run(capsys, "artifacts", "T-1", "--type", "anr")
    assert len(out.splitlines()) == 1 and "ANR Blocked com.example.radio" in out

    code, out, _ = run(capsys, "artifacts", "T-1", "--show", cite)
    assert code == 0
    assert "record log:11#1" in out and "signal 11 (SIGSEGV)" in out
    assert f"{cite}/backtrace" in out and "L16-L22" in out

    code, out, _ = run(capsys, "artifacts", "T-1", "--show", f"{cite}/backtrace:L17")
    assert out.startswith("L17  ") and "VehicleHal::setValue" in out

    code, _, err = run(capsys, "artifacts", "T-1", "--show", f"{cite}/nope")
    assert code == 2 and "no section 'nope'" in err
    code, _, err = run(capsys, "artifacts", "nope")
    assert code == 1 and "does not exist" in err


def test_p1_commands(home: Path, tmp_path: Path, secret: None, capsys: pytest.CaptureFixture[str]) -> None:
    z = make_bugreport_zip(tmp_path)
    assert run(capsys, "index", "-q", "T-1", str(z))[0] == 0
    code, out, _ = run(capsys, "rules")
    assert code == 0 and "java_crash" in out and "[package]" in out and len(out.strip().splitlines()) == 10
    code, out, _ = run(capsys, "rules", "--show", "anr")
    assert code == 0 and "rule: anr" in out
    code, out, _ = run(capsys, "detect", "T-1", "--rule", "java_crash", "--examples", "1")
    assert code == 0 and "[high] java_crash ×3" in out and "… 2 more messages" in out
    code, out, _ = run(capsys, "overview", "T-1", "--level", "E")
    assert code == 0 and "levels: F 4, E 4" in out
    code, out, _ = run(capsys, "count", "T-1", "--by", "level", "--bucket", "5m", "--search", "hvac")
    assert code == 0 and "bucket 5m" in out
    code, _, err = run(capsys, "count", "T-1", "--bucket", "1s")
    assert code == 2 and "use bucket=" in err
    good = tmp_path / "good.md"
    good.write_text("The app crashed (log:3#1).\n", encoding="utf-8")
    code, out, _ = run(capsys, "check", "T-1", str(good))
    assert code == 0 and out.startswith("PASS")
    bad = tmp_path / "bad.md"
    bad.write_text("The app crashed (log:3#9).\n", encoding="utf-8")
    code, out, _ = run(capsys, "check", "T-1", str(bad))
    assert code == 1 and "does not resolve" in out
