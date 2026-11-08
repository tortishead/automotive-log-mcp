from __future__ import annotations

from automotive_log_mcp.core.segments import SegmentTracker
from automotive_log_mcp.core.timeparse import NS, YearTracker, ts_raw_of, wall_ns


def test_year_rollover_increments_year() -> None:
    y = YearTracker(2026, 1)  # log taken in January 2026
    assert [y.year_for(m) for m in (12, 12, 1, 12, 1, 2)] == [2025, 2025, 2026, 2025, 2026, 2026]


def test_year_from_reference_without_rollover() -> None:
    y = YearTracker(2026, 11)
    assert [y.year_for(m) for m in (10, 11)] == [2026, 2026]


def test_leap_day_without_year() -> None:
    assert wall_ns(2024, 2, 29, 0, 0, 0) - wall_ns(2024, 2, 28, 0, 0, 0) == 86400 * NS


def test_ts_raw_reproduced() -> None:
    ts = wall_ns(2026, 10, 11, 8, 12, 3, "412")
    assert ts_raw_of(None, ts, None, "md_ms") == "10-11 08:12:03.412"
    assert ts_raw_of(None, None, 1_234_567_000, "sec_us") == "1.234567"
    assert ts_raw_of("raw", ts, None, "md_ms") == "raw"


def test_segments() -> None:
    t = SegmentTracker()
    s = 60 * NS
    segs = [
        t.observe(1, 100 * s, 100 * s, False, False),
        t.observe(2, 101 * s, 101 * s, False, False),
        t.observe(3, 100 * s, 100 * s, False, False),  # 60 s back: not more than 60 s
        t.observe(4, 98 * s, 98 * s, False, False),  # 3 min back: new segment
        t.observe(5, 99 * s, 99 * s, False, True),  # boot marker
        t.observe(6, 99 * s, 99 * s, False, True),  # consecutive markers: same segment
    ]
    assert segs == [1, 1, 1, 2, 3, 3]
    assert [x.reason for x in t.segments] == ["start", "wall_jump_back", "boot"]


def test_monotonic_reset() -> None:
    t = SegmentTracker()
    assert [t.observe(i, None, v, True, False) for i, v in enumerate((5, 9, 1, 2), 1)] == [1, 1, 2, 2]
    assert t.segments[1].reason == "monotonic_reset"
