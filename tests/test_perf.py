"""Performance on a generated logcat (excluded from the default run).

    PERF_MB=1024 python -m pytest -m perf -s tests/test_perf.py

The log and the case DB are built once per run under pytest's temporary directory and deleted
afterwards; ``PERF_KEEP=<dir>`` keeps both in ``<dir>`` and reuses them in later runs (the ingest
test is then skipped). Tests print their numbers against the plan's targets and fail only on wrong
results, not on missed targets, so the numbers are always reported.

- M1 ingest: lines/s (target >= 100k) and DB size (target <= 1.5x input).
- M3 tools: every tool's text output under 8 KB with a broad query; latency: text search < 1 s,
  context < 200 ms, filtered regex < 5 s.
- M4 tools (overview, count, detect, check_citations): text under 8 KB; latency is reported (the
  plan sets no target for them).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from automotive_log_mcp.app import App
from automotive_log_mcp.config import load_config
from automotive_log_mcp.policy import Policy
from automotive_log_mcp.tools import Filter, Output, Tools

from .conftest import FIXED_SECRET

pytestmark = pytest.mark.perf

GEN = Path(__file__).resolve().parent.parent / "scripts" / "gen_logcat.py"


@dataclass
class Built:
    app: App
    log: Path
    size: int
    physical: int
    ingest_s: float | None  # None when reused from PERF_KEEP
    records: int


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Built]:
    mb = float(os.environ.get("PERF_MB", "100"))
    keep = os.environ.get("PERF_KEEP")
    base = Path(keep) if keep else tmp_path_factory.mktemp("perf")
    home = base / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "secret.key").write_bytes(FIXED_SECRET)
    old = os.environ.get("AUTOMOTIVE_LOG_MCP_HOME")
    os.environ["AUTOMOTIVE_LOG_MCP_HOME"] = str(home)
    try:
        app = App.create(load_config(home=home), Policy())
        log = base / f"perf-{mb:g}.log"
        db_path = app.config.case_db("perf")
        if not log.exists():
            subprocess.run([sys.executable, str(GEN), str(log), "--mb", str(mb)], check=True)
        physical = sum(1 for _ in log.open("rb"))
        ingest_s: float | None = None
        if not (keep and db_path.exists()):
            for p in db_path.parent.glob("perf.db*"):
                p.unlink()
            start = time.perf_counter()
            app.ingestor.add(db_path, str(log))
            ingest_s = time.perf_counter() - start
        records = sum(s.records for s in app.list_sources("perf") or [])
        yield Built(app, log, log.stat().st_size, physical, ingest_s, records)
    finally:
        if old is None:
            os.environ.pop("AUTOMOTIVE_LOG_MCP_HOME", None)
        else:
            os.environ["AUTOMOTIVE_LOG_MCP_HOME"] = old
        if not keep:
            shutil.rmtree(base, ignore_errors=True)


def test_ingest_generated_logcat(built: Built) -> None:
    if built.ingest_s is None:
        pytest.skip("DB reused from PERF_KEEP; delete it to measure ingest")
    db_path = built.app.config.case_db("perf")
    db_size = sum(p.stat().st_size for p in db_path.parent.glob("perf.db*"))
    rate = built.physical / built.ingest_s
    print(
        f"\nperf ingest: {built.size / 2**20:.0f} MiB, {built.physical:,} lines, {built.records:,} records in "
        f"{built.ingest_s:.1f} s -> {rate:,.0f} lines/s (target >= 100,000: "
        f"{'met' if rate >= 100_000 else 'NOT met'}); DB {db_size / 2**20:.0f} MiB = "
        f"{db_size / built.size:.2f}x input (target <= 1.5x: {'met' if db_size <= 1.5 * built.size else 'NOT met'})"
    )
    rows = built.app.list_sources("perf") or []
    lines = sum(s.lines for s in rows)
    markers = sum(1 for line in built.log.open("rb") if line.startswith(b"--------- "))
    assert lines + markers == built.physical


# (label, target seconds or None, call) — broad queries on purpose: the caps must hold.
Call = Callable[[Tools], Output]


def _calls(tools: Tools) -> list[tuple[str, str, float | None, Call]]:
    mid = _middle(tools)
    hvac = Filter.model_validate({"level": "E", "tags": ["HvacCtrl"]})
    window = Filter.model_validate({"from": f"{mid}-5m", "to": "+10m"})
    return [
        ("search", "text, broad ('failed'), collapsed", 1.0, lambda t: t.search("perf", "failed")),
        ("search", "text, very broad ('com'), collapsed", 1.0, lambda t: t.search("perf", "com")),
        ("search", "text, broad, not collapsed", 1.0, lambda t: t.search("perf", "failed", collapse=False)),
        (
            "search",
            "text, rare (one word)",
            1.0,
            lambda t: t.search("perf", "NullPointerException", collapse=False),
        ),
        ("search", "text, count_only", 1.0, lambda t: t.search("perf", "setProperty", count_only=True)),
        (
            "search",
            "text + filter (level E, tag)",
            1.0,
            lambda t: t.search("perf", "setProperty", filter=hvac),
        ),
        ("search", "text + 15 min window", 1.0, lambda t: t.search("perf", "dropped", filter=window)),
        (
            "search",
            "regex filtered (literal prefilter, level E, tag)",
            5.0,
            lambda t: t.search("perf", r"status=\w+_AVAILABLE, prop=0x[0-9a-f]+", mode="regex", filter=hvac),
        ),
        (
            "search",
            "regex filtered (15 min window, no literal word)",
            5.0,
            lambda t: t.search("perf", r"queue=\d{3}\b", mode="regex", filter=window),
        ),
        (
            "search",
            "regex unfiltered (capped scan)",
            None,
            lambda t: t.search("perf", r"latency \d+ ms", mode="regex", count_only=True),
        ),
        ("context", "scope=source", 0.2, lambda t: t.context("perf", mid)),
        ("context", "scope=pid", 0.2, lambda t: t.context("perf", mid, scope="pid")),
        ("list_artifacts", "all", None, lambda t: t.list_artifacts("perf")),
        ("read_artifact", "summary", None, lambda t: t.read_artifact("perf", "art:1")),
        ("read_artifact", "section", None, lambda t: t.read_artifact("perf", "art:1/exception")),
        ("get_records", "100 ids", None, lambda t: t.get_records("perf", _ids(mid))),
        ("list_sources", "-", None, lambda t: _list_sources(t)),
        ("overview", "whole case", None, lambda t: t.overview("perf")),
        ("overview", "15 min window", None, lambda t: t.overview("perf", window)),
        ("count", "by tag", None, lambda t: t.count("perf")),
        ("count", "by level, bucket 10m", None, lambda t: t.count("perf", by="level", bucket="10m")),
        ("count", "by template", None, lambda t: t.count("perf", by="template")),
        ("count", "text, broad ('failed'), by tag", None, lambda t: t.count("perf", text="failed")),
        (
            "count",
            "regex filtered (level E, tag), by pid",
            None,
            lambda t: t.count("perf", by="pid", text=r"status=\w+_AVAILABLE", mode="regex", filter=hvac),
        ),
        ("detect", "all rules", None, lambda t: t.detect("perf")),
        ("detect", "all rules, 15 min window", None, lambda t: t.detect("perf", filter=window)),
        (
            "check_citations",
            "answer with 40 citations",
            None,
            lambda t: t.check_citations("perf", _answer(mid)),
        ),
    ]


def _middle(tools: Tools) -> str:
    rows = tools.app.list_sources("perf") or []
    main = max(rows, key=lambda s: s.records)
    return f"log:{main.id}#{main.records // 2}"


def _answer(mid: str) -> str:
    sid, seq = mid[4:].split("#")
    paras = [
        f"Finding {i}: `setProperty failed` at log:{sid}#{int(seq) + i} and log:{sid}#{int(seq) + 1000 * i}."
        for i in range(20)
    ]
    return "# Answer\n\n" + "\n\n".join(paras) + "\n\nThe rest is a guess.\n"


def _ids(mid: str) -> list[str]:
    sid, seq = mid[4:].split("#")
    return [f"log:{sid}#{int(seq) + i}" for i in range(100)]


def _list_sources(tools: Tools) -> Output:
    from automotive_log_mcp.server import _source_info

    rows = tools.app.list_sources("perf") or []
    text = "\n".join(f"{s.id} {s.format} {s.records} {s.name}" for s in rows)
    from automotive_log_mcp.server import ListSourcesResult

    return Output(text, ListSourcesResult(case="perf", exists=True, sources=[_source_info(s) for s in rows]))


def test_tools_caps_and_latency(built: Built) -> None:
    tools = Tools(built.app)
    report = [f"\nperf tools on {built.size / 2**20:.0f} MiB ({built.records:,} records):"]
    failures: list[str] = []
    for tool, label, target, call in _calls(tools):
        call(tools)  # warm the page cache once, as a second call from an agent would
        times = []
        for _ in range(3):
            start = time.perf_counter()
            out = call(tools)
            times.append(time.perf_counter() - start)
        best = min(times)
        text_b = len(out.text.encode("utf-8"))
        json_b = len(json.dumps(out.data.model_dump(mode="json", by_alias=True, exclude_none=True)))
        verdict = "" if target is None else (" ok" if best < target else f" NOT met (< {target:g} s)")
        report.append(
            f"  {tool:<15} {label:<50} {best * 1000:8.0f} ms{verdict:<18} text {text_b:5d} B  json {json_b:6d} B"
        )
        if text_b > 8192:
            failures.append(f"{tool} {label}: {text_b} B text")
    print("\n".join(report))
    assert not failures, failures
