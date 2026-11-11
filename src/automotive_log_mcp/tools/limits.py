"""Tunable limits of the tools (besides ``config.caps``).

Read as ``limits.NAME`` at call time, so a test can patch one in a single place.
"""

from __future__ import annotations

from ..core.timeparse import NS

AGG_ROWS = 300_000  # text matches grouped exactly; above, groups are estimated from a sample
TIME_INDEX_ROWS = 20_000  # text matches above which a page is read by walking the time index
MAX_BUCKETS = 60  # count(bucket=...) and the overview histogram
TOP_N = 8  # overview: top tags, pids, templates
GAP_NS = 30 * NS  # overview: gaps without a record longer than this
BURST_NS = 10 * NS  # overview: E/F counted per bucket of this size ...
BURST_MIN = 10  # ... a burst has more than 5× the median and more than this many
SAME_CRASH_NS = 10 * NS  # copies of one crash (crash buffer, dropbox, ANR file and section) are this close
