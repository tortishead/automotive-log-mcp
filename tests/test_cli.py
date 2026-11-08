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
    assert "sources 1, 2, 3, 4, 5, 6" in out and "lines/s" in out
    assert "note:" in out and "tombstone_00" in out
    assert "hashing" in err  # progress on stderr

    code, out, _ = run(capsys, "index", "T-1", str(z), "-q")
    assert "already in case" in out

    code, out, _ = run(capsys, "sources", "T-1")
    assert code == 0 and "SYSTEM LOG:crash" in out and "KERNEL LOG:kernel" in out

    code, out, _ = run(capsys, "query", "T-1", "--search", "NullPointerException")
    assert code == 0
    assert out.splitlines()[0].startswith("log:4#1  01-01 00:00:01.000  E 1234/1250 AndroidRuntime  FATAL")

    code, out, _ = run(capsys, "query", "T-1", "--level", "E", "--count")
    assert out.strip() == "3"
    code, out, _ = run(capsys, "query", "T-1", "--tag", "Hvac*", "--limit", "1")
    assert out.splitlines()[1].startswith("… 1 more")
    code, out, _ = run(capsys, "query", "T-1", "--regex", r"status=\w+_AVAIL")
    assert "log:2#2" in out
    code, out, _ = run(capsys, "query", "T-1", "--search", "3c:5a:b4:01:02:03")  # raw value -> token
    assert "log:6#3" in out and "<MAC:" in out
    code, out, _ = run(capsys, "query", "T-1", "--search", "nothing_like_this")
    assert "No matches." in out


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
