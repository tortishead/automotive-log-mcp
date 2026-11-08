"""M1 performance test on a generated logcat (excluded from the default run).

    PERF_MB=1024 pytest -m perf -s tests/test_perf.py

Writes the log and the case DB under pytest's temporary directory (outside the repository) and
deletes both afterwards. Prints lines/s and DB size against the plan's targets (>= 100k lines/s,
DB <= 1.5x input); the test fails only on wrong results, not on missed targets, so the numbers are
always reported.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from automotive_log_mcp.app import App

pytestmark = pytest.mark.perf

GEN = Path(__file__).resolve().parent.parent / "scripts" / "gen_logcat.py"


def test_ingest_generated_logcat(app: App, tmp_path: Path) -> None:
    mb = float(os.environ.get("PERF_MB", "100"))
    log = tmp_path / "perf.log"
    subprocess.run([sys.executable, str(GEN), str(log), "--mb", str(mb)], check=True)
    size = log.stat().st_size
    physical = sum(1 for _ in log.open("rb"))
    db_path = app.config.case_db("perf")
    try:
        start = time.perf_counter()
        f = app.ingestor.add(db_path, str(log)).files[0]
        secs = time.perf_counter() - start
        db_size = sum(p.stat().st_size for p in db_path.parent.glob("perf.db*"))
        rate = physical / secs
        print(
            f"\nperf: {size / 2**20:.0f} MiB, {physical:,} lines, {f.records:,} records in {secs:.1f} s "
            f"-> {rate:,.0f} lines/s (target >= 100,000: {'met' if rate >= 100_000 else 'NOT met'}); "
            f"DB {db_size / 2**20:.0f} MiB = {db_size / size:.2f}x input "
            f"(target <= 1.5x: {'met' if db_size <= 1.5 * size else 'NOT met'})"
        )
        markers = sum(1 for line in log.open("rb") if line.startswith(b"--------- "))
        assert f.lines + markers == physical
    finally:
        for p in db_path.parent.glob("perf.db*"):
            p.unlink()
        log.unlink()
