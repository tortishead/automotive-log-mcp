"""Fast timestamp helpers shared by connectors.

All wall-clock times are kept as written (local time of the device), as ns since the epoch
computed as if the written time were UTC. The source's time zone is stored in its metadata; the
clock model (M5) maps to real UTC. This keeps printed timestamps round-trippable.
"""

from __future__ import annotations

import calendar
import datetime as _dt
from functools import lru_cache

NS = 1_000_000_000


@lru_cache(maxsize=4096)
def day_ns(year: int, month: int, day: int) -> int:
    """ns since the epoch at 00:00 of a date (proleptic, treated as UTC)."""
    return calendar.timegm((year, month, day, 0, 0, 0, 0, 0, 0)) * NS


def frac_ns(frac: str) -> int:
    """Fractional-seconds digits ('412', '412345', '412345678') to ns."""
    if not frac:
        return 0
    return int(frac[:9].ljust(9, "0"))


def wall_ns(year: int, month: int, day: int, hh: int, mm: int, ss: int, frac: str = "") -> int:
    return day_ns(year, month, day) + (hh * 3600 + mm * 60 + ss) * NS + frac_ns(frac)


def seconds_ns(text: str) -> int:
    """'123.456789' (seconds, optional fraction) to ns, without float rounding."""
    sec, _, frac = text.strip().partition(".")
    return int(sec) * NS + frac_ns(frac)


class YearTracker:
    """Assigns years to timestamps written without one (logcat ``MM-DD``, syslog ``Mon DD``).

    ``ref_year``/``ref_month`` come from when the log was taken (dumpstate time, file mtime).
    The first record gets ``ref_year``, or the year before if its month is after ``ref_month``
    (a log taken in January that starts in December). Afterwards December followed by January
    increments the year (New Year rollover); nothing else changes it.
    """

    def __init__(self, ref_year: int, ref_month: int = 12) -> None:
        self.ref_year = ref_year
        self.ref_month = ref_month
        self.year: int | None = None
        self.last_month = 0

    def year_for(self, month: int) -> int:
        if self.year is None:
            self.year = self.ref_year - 1 if month > self.ref_month else self.ref_year
        elif self.last_month == 12 and month == 1:
            self.year += 1
        elif self.last_month == 1 and month == 12:
            # A late December line right after the rollover (buffers interleave around
            # midnight): it belongs to the old year; stay in January.
            return self.year - 1
        self.last_month = month
        return self.year


# Per-source formats that reproduce ts_raw exactly, so ts_raw need not be stored (sources.meta
# 'ts_format'). Connectors set ts_raw=None only for records whose raw text has this form.
def _fmt_md_ms(ns: int) -> str:
    secs, frac = divmod(ns, NS)
    days, rem = divmod(secs, 86400)
    d = _dt.date(1970, 1, 1) + _dt.timedelta(days=days)
    hms = f"{rem // 3600:02d}:{rem // 60 % 60:02d}:{rem % 60:02d}"
    return f"{d.month:02d}-{d.day:02d} {hms}.{frac // 1_000_000:03d}"


def _fmt_sec_us(ns: int) -> str:
    secs, frac = divmod(ns, NS)
    return f"{secs}.{frac // 1000:06d}"


TS_FORMATS = {
    "md_ms": (_fmt_md_ms, "ts"),  # logcat MM-DD hh:mm:ss.mmm, from ts
    "sec_us": (_fmt_sec_us, "ts_src"),  # kernel seconds.micros since boot, from ts_src
}


def ts_raw_of(ts_raw: str | None, ts: int | None, ts_src: int | None, ts_format: str | None) -> str | None:
    """The timestamp as written: stored, or reproduced from the source's ts_format."""
    if ts_raw is not None or ts_format is None:
        return ts_raw
    fmt, field = TS_FORMATS[ts_format]
    value = ts if field == "ts" else (ts_src if ts_src is not None else ts)
    return fmt(value) if value is not None else None


def fmt_wall(ns: int, digits: int = 3) -> str:
    """'YYYY-MM-DD hh:mm:ss.fff' for a wall-clock ns value (as written, no zone)."""
    secs, frac = divmod(ns, NS)
    days, rem = divmod(secs, 86400)
    d = _dt.date(1970, 1, 1) + _dt.timedelta(days=days)
    out = f"{d.isoformat()} {rem // 3600:02d}:{rem // 60 % 60:02d}:{rem % 60:02d}"
    if digits:
        out += "." + f"{frac:09d}"[:digits]
    return out
