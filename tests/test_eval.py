"""The evaluation harness on the synthetic cases (eval/cases.yaml), as CI runs it.

The recorded baseline is what evidence recall gave when the cases were first run; a change in it
(better or worse) should be a deliberate update here and in the plan's M4 status.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

EVAL = Path(__file__).resolve().parent.parent / "eval"

# case -> (exact, by artifact, missed): first run of the synthetic cases (M4 five and M6 five,
# Nov 10, 2026)
BASELINE = {
    "syn-hvac-crash": (["log:1#2"], ["art:1/exception:L3"], []),
    "syn-radio-anr": ([], ["art:2/p1500.t1.main:L12", "art:2/p1500.t12.radioworker:L23"], []),
    "syn-vhal-abort": (["log:1#15", "log:1#16"], [], []),
    "syn-lmk-media": (["log:1#11", "log:1#12"], [], []),
    # detect shows the avc denial, not hvacd's own "Permission denied" error before it
    "syn-boot-selinux": (["log:1#5"], [], ["log:1#4"]),
    # M6: a kernel log (seconds since boot) and a logcat; the cause shows only on the timeline
    "syn-can-busoff": (["log:2#6", "log:1#6"], [], []),
    "syn-watchdog-audio": (["log:1#4", "log:1#8"], [], []),
    "syn-evs-crash": (["log:1#5"], ["art:1/backtrace:L16"], []),
    # only diff (boot 1 against boot 2) shows the firmware lines: no level filter or rule has them
    "syn-wifi-fw-ota": (["log:1#15", "log:1#16"], [], []),
    "syn-bt-restart": (["log:1#3", "log:1#5"], [], []),
}


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, EVAL / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_evidence_recall_on_synthetic_cases(tmp_path: Path) -> None:
    er = _load("evidence_recall")
    out = tmp_path / "r.json"
    report = tmp_path / "r.md"
    rc = er.main([str(EVAL / "cases.yaml"), "--synthetic-only", "--json", str(out), "--report", str(report)])
    assert rc == 0
    results = {r["id"]: r for r in json.loads(out.read_text(encoding="utf-8"))}
    assert set(results) == set(BASELINE)
    for cid, (exact, art, missed) in BASELINE.items():
        r = results[cid]
        assert r["status"] == "ok" and not r["unresolved"], r
        assert (r["exact"], r["artifact"], r["missed"]) == (exact, art, missed), cid
        assert all(c["bytes"] <= 8192 for c in r["calls"])
    assert results["syn-can-busoff"]["order"].startswith("ok")
    text = report.read_text(encoding="utf-8")
    assert "19/20 root-cause citations surfaced" in text
    assert "syn-can-busoff: log:2#6 < log:1#6: ok" in text
    # with --min-recall 1 the known miss fails the run
    assert (
        er.main(
            [
                str(EVAL / "cases.yaml"),
                "--case",
                "syn-boot-selinux",
                "--min-recall",
                "1",
                "--report",
                str(report),
            ]
        )
        == 1
    )


def test_order_check_fails_for_a_wrong_order(tmp_path: Path) -> None:
    er = _load("evidence_recall")
    text = (
        (EVAL / "cases.yaml")
        .read_text(encoding="utf-8")
        .replace("order: [log:2#6, log:1#6]", "order: [log:1#6, log:2#6]")
    )
    swapped = EVAL / "cases.swapped-test.yaml"  # next to cases.yaml: its files are relative to it
    swapped.write_text(text, encoding="utf-8")
    try:
        out = tmp_path / "r.json"
        args = [
            str(swapped),
            "--case",
            "syn-can-busoff",
            "--json",
            str(out),
            "--report",
            str(tmp_path / "r.md"),
        ]
        assert er.main(args) == 1
        r = json.loads(out.read_text(encoding="utf-8"))[0]
        assert "log:1#6 is not before log:2#6 on the case clock" in r["order"]
    finally:
        swapped.unlink()


def test_case_file_errors(tmp_path: Path) -> None:
    er = _load("evidence_recall")
    bad = tmp_path / "bad.yaml"
    for text, match in (
        ("cases: [{id: 'a b', files: [x], expected: {citations: [log:1#1]}}]", "'id'"),
        ("cases: [{id: a, files: [x], expected: {citations: [log:1]}}]", "malformed"),
        ("cases: [{id: a, files: [x], expected: {}}]", "non-empty list"),
        ("cases: [{id: a, files: [x], expected: {citations: [log:1#1]}, queries: [{tool: rm}]}]", "query"),
        ("cases: [{id: a, files: [x], expected: {citations: [log:1#1], order: [x]}}]", "order"),
    ):
        bad.write_text(text, encoding="utf-8")
        with pytest.raises(er.CaseError, match=match):
            er.load_cases(bad)
    missing = tmp_path / "missing.yaml"
    missing.write_text(
        "cases: [{id: a, files: [nope.log], expected: {citations: [log:1#1]}}]", encoding="utf-8"
    )
    assert er.main([str(missing), "--report", str(tmp_path / "m.md")]) == 0
    assert "files missing" in (tmp_path / "m.md").read_text(encoding="utf-8")
    assert er.main([str(missing), "--strict", "--report", str(tmp_path / "m.md")]) == 1


def test_callstats(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cs = _load("callstats")
    log = tmp_path / "calllog.jsonl"
    rows = [
        {
            "ts": "2026-11-10T09:00:00.000+00:00",
            "session": "s1",
            "tool": "list_sources",
            "args": {"case": "T-1"},
            "status": "ok",
            "duration_ms": 3.0,
            "bytes_out": 300,
        },
        {
            "ts": "2026-11-10T09:00:05.000+00:00",
            "session": "s1",
            "tool": "detect",
            "args": {"case": "T-1"},
            "status": "ok",
            "duration_ms": 30.0,
            "bytes_out": 2000,
        },
        {
            "ts": "2026-11-10T09:01:00.000+00:00",
            "session": "s1",
            "tool": "check_citations",
            "args": {"case": "T-1", "text": "<10 chars>"},
            "status": "error",
            "duration_ms": 1.0,
            "bytes_out": 0,
        },
        {
            "ts": "2026-11-10T10:00:00.000+00:00",
            "session": "s2",
            "tool": "search",
            "args": {"case": "T-2"},
            "status": "ok",
            "duration_ms": 5.0,
            "bytes_out": 500,
        },
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert cs.main([str(log), "--case", "T-1", "--markdown"]) == 0
    out = capsys.readouterr().out
    assert "| s1 | T-1 | 2026-11-10T09:00:00 | 3 | 2,300 | 1 | 1 |" in out
    assert "s2" not in out
    assert cs.main([str(log), "--since", "2026-11-10T09:30"]) == 0
    assert "session s2" in capsys.readouterr().out
