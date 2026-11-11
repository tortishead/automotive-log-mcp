"""Time and citation arguments: the ``from``/``to`` forms the tools accept, durations, citations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from ..core.timeparse import NS, wall_ns
from ..model import SEQ_MASK
from ..store import query
from .models import ToolInputError

FULL_TS = re.compile(
    r"(\d{4})-(\d\d)-(\d\d)(?:[ T](\d\d):(\d\d)(?::(\d\d)(?:[.,](\d{1,9}))?)?)?\s*(?:[+-]\d\d:?\d\d|Z|UTC)?"
)


SHORT_TS = re.compile(r"(\d\d)-(\d\d)[ T](\d\d):(\d\d)(?::(\d\d)(?:[.,](\d{1,9}))?)?")


CLOCK_TS = re.compile(r"(\d\d):(\d\d)(?::(\d\d)(?:[.,](\d{1,9}))?)?")


MONO_TS = re.compile(r"\[?\s*(\d+)\.(\d{1,9})\s*\]?")


_DUR_PART = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)")


DUR = r"(?:\d+(?:\.\d+)?\s*(?:ms|s|m|h|d)\s*)+"


REL_TIME = re.compile(rf"([+-])\s*({DUR})")


CITE_OFFSET = re.compile(rf"(log:\d+#\d+|art:\d+)\s*(?:([+-])\s*({DUR}))?")


_UNIT_NS = {"ms": NS // 1000, "s": NS, "m": 60 * NS, "h": 3600 * NS, "d": 86400 * NS}


TIME_FORMS = (
    "a printed timestamp ('01-01 00:00:01.000', '2026-01-01 00:00:01.000', '00:00:01', or "
    "'123.456789' since boot), a relative time ('-5m', '+30s'), or a citation ('log:3#1', "
    "'art:2', 'log:3#1-30s')"
)


def duration_ns(text: str) -> int:
    total = 0
    for num, unit in _DUR_PART.findall(text):
        total += int(float(num) * _UNIT_NS[unit])
    return total


def granularity(sec: str | None, frac: str | None, minute: str | None = "0") -> int:
    if minute is None:
        return 86400 * NS
    if sec is None:
        return 60 * NS
    if not frac:
        return NS
    return int(10 ** (9 - len(frac)))


@dataclass(frozen=True)
class Bound:
    clock: Literal["wall", "mono"]
    ns: int
    gran: int = 1  # the value covers [ns, ns + gran)


@dataclass
class Span:
    wall: tuple[int, int] | None  # ts range of the selected sources
    mono: tuple[int, int] | None  # ts_src range of sources without wall time


ART_ID = re.compile(r"art:(\d+)")


LOG_CITE = re.compile(r"log:(\d+)#(\d+)")


def parse_log_cite(text: str) -> tuple[int, int]:
    m = LOG_CITE.fullmatch(text.strip())
    if m is None:
        raise ToolInputError(f"invalid citation {text!r}; expected log:<source>#<seq>, e.g. log:3#120")
    sid, seq = int(m[1]), int(m[2])
    if seq < 1 or seq > SEQ_MASK:
        raise ToolInputError(f"invalid citation {text!r}: seq starts at 1")
    return sid, seq


def sources_span(rows: list[query.SourceRow]) -> Span:
    walls = [(s.ts_min, s.ts_max) for s in rows if s.ts_min is not None and s.ts_max is not None]
    monos = [
        (s.ts_src_min, s.ts_src_max)
        for s in rows
        if s.ts_min is None and s.ts_src_min is not None and s.ts_src_max is not None
    ]
    return Span(
        (min(a for a, _ in walls), max(b for _, b in walls)) if walls else None,
        (min(a for a, _ in monos), max(b for _, b in monos)) if monos else None,
    )


def full_ts_ns(full: str) -> int:
    """A full timestamp as printed (``YYYY-MM-DD hh:mm:ss.mmm``) back to ns."""
    m = FULL_TS.fullmatch(full)
    if m is None:
        raise ValueError(f"not a full timestamp: {full!r}")
    return wall_ns(
        int(m[1]), int(m[2]), int(m[3]), int(m[4] or 0), int(m[5] or 0), int(m[6] or 0), m[7] or ""
    )
