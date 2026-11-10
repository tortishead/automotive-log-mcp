"""The P0 read tools: argument resolution, queries, compact text and structured output.

``server.py`` registers these with MCP and turns :class:`ToolInputError` into tool errors; the
CLI ``query`` uses :meth:`Tools.search` too, so both print the same lines. Each method returns an
:class:`Output`: the text the agent reads (capped by ``config.caps``) and a pydantic model for
structured content holding the same items.

Filter times (``from``/``to``) accept every timestamp the tools print, plus relative times and
citations; see :func:`Tools.resolve_time`.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .app import App
from .clocktools import ClockTools, TimelineRecord
from .core import citations as cit
from .core import format as fmt
from .core.timeparse import NS, ts_raw_of, wall_ns
from .detect.engine import SEVERITIES, Rule, RuleResult, run_rule
from .model import SEQ_BITS, SEQ_MASK, citation
from .store import db, query
from .store.query import ArtifactRow, Rec, RecordFilter

Level = Literal["V", "D", "I", "W", "E", "F"]


AGG_ROWS = 300_000  # text matches grouped exactly; above, groups are estimated from a sample
TIME_INDEX_ROWS = 20_000  # text matches above which a page is read by walking the time index
MAX_BUCKETS = 60  # count(bucket=...) and the overview histogram
TOP_N = 8  # overview: top tags, pids, templates
GAP_NS = 30 * NS  # overview: gaps without a record longer than this
BURST_NS = 10 * NS  # overview: E/F counted per bucket of this size ...
BURST_MIN = 10  # ... a burst has more than 5× the median and more than this many


class ToolInputError(ValueError):
    """Invalid arguments, or ids that do not exist; the message says how to fix the call."""


# -- argument and result models -------------------------------------------------------------------


class Filter(BaseModel):
    """Record filter; all fields optional, combined with AND."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    sources: list[str] | None = Field(
        default=None, description="Source ids to include, as list_sources prints them."
    )
    from_: str | None = Field(
        default=None,
        alias="from",
        description="Start time, inclusive: a timestamp as printed, '-5m' (before the case end), "
        "'+30s' (after its start), or a citation with optional offset ('log:3#1-30s', 'art:2').",
    )
    to: str | None = Field(
        default=None,
        description="End time, inclusive to the precision given; as 'from' ('+30s': after 'from').",
    )
    level: Level | None = Field(default=None, description="Minimum level: V < D < I < W < E < F.")
    tags: list[str] | None = Field(default=None, description="Tags to include; globs allowed, e.g. 'Car*'.")
    not_tags: list[str] | None = Field(default=None, description="Tags to exclude; globs allowed.")
    pid: int | None = Field(default=None, ge=0, description="Process id.")
    attrs: dict[str, str | int | float | bool] | None = Field(
        default=None,
        description="Attributes that must match exactly, as get_records shows them, e.g. {'art': 3}.",
    )


class RecordOut(BaseModel):
    cite: str = Field(description="Citation log:<source>#<seq>.")
    ts: str | None = Field(default=None, description="Time, accepted back as from/to.")
    level: str | None = None
    tag: str | None = None
    pid: int | None = None
    tid: int | None = None
    msg: str = Field(description="Message; cut at the output limit with … unless from get_records.")
    cut: bool | None = Field(default=None, description="True when msg was cut; get_records has it in full.")
    art: str | None = Field(default=None, description="The artifact this record belongs to (read_artifact).")
    count: int | None = Field(
        default=None, description="Collapsed: number of matches with this message template."
    )
    last: str | None = Field(default=None, description="Collapsed: citation of the last match.")
    last_ts: str | None = Field(default=None, description="Collapsed: time of the last match.")
    template: str | None = Field(default=None, description="Message template (variable parts as <*>).")
    literal: str | None = Field(
        default=None, description="Longest constant text of the template, for a code search."
    )
    ts_raw: str | None = Field(default=None, description="get_records: the timestamp as written in the log.")
    attrs: dict[str, Any] | None = Field(default=None, description="get_records: format-specific attributes.")
    source: str | None = Field(default=None, description="get_records: source id.")


class SearchResult(BaseModel):
    case: str
    mode: str
    query: str = Field(description="The query as run (raw values redacted, as in the logs).")
    total: int = Field(description="Matches.")
    exact: bool = Field(
        default=True,
        description="False when a regex scan stopped early (total is a lower bound) or when counts were "
        "estimated from a sample of a very broad text query (≈).",
    )
    groups: int = Field(description="Distinct messages (source, template, tag, level) among the matches.")
    hits: list[RecordOut]
    next_cursor: str | None = None
    warnings: list[str] = Field(default_factory=list)
    empty_reason: str | None = None


class ContextResult(BaseModel):
    case: str
    at: str
    scope: str
    records: list[RecordOut | TimelineRecord]
    note: str | None = None


class ArtifactOut(BaseModel):
    cite: str
    type: str
    ts: str | None = None
    pid: int | None = None
    process: str | None = None
    record: str = Field(description="Its record on the timeline, for context().")
    lines: int
    headline: str
    same_as: str | None = Field(
        default=None, description="The same crash already listed (e.g. crash buffer and dropbox)."
    )


class ArtifactGroupOut(BaseModel):
    type: str
    signature: str | None = None
    count: int
    artifacts: list[ArtifactOut]
    more: int = 0


class ListArtifactsResult(BaseModel):
    case: str
    total: int
    groups: list[ArtifactGroupOut]
    more_groups: int = 0
    empty_reason: str | None = None


class SectionOut(BaseModel):
    name: str
    cite: str
    first: int
    last: int


class LineOut(BaseModel):
    n: int
    text: str


class ReadArtifactResult(BaseModel):
    cite: str
    type: str
    name: str
    record: str
    ts: str | None = None
    ts_raw: str | None = None
    pid: int | None = None
    tid: int | None = None
    process: str | None = None
    signature: str | None = None
    summary: str | None = None
    sections: list[SectionOut] = Field(default_factory=list)
    same_crash: list[str] = Field(default_factory=list)
    lines: list[LineOut] | None = None
    next: str | None = Field(default=None, description="Citation of the lines not shown, to read next.")


class GetRecordsResult(BaseModel):
    case: str
    records: list[RecordOut]
    missing: list[str] = Field(default_factory=list)
    not_shown: list[str] = Field(default_factory=list, description="Left out at the byte cap; ask again.")


# -- P1 result models -----------------------------------------------------------------------------


class CountGroup(BaseModel):
    key: str | int | None = Field(
        default=None, description="The value: tag, level, pid, source id or template id (null: none)."
    )
    label: str = Field(description="As printed: tag name, level, pid and process, source name, template.")
    count: int
    example: str | None = Field(default=None, description="Citation of one record with this value.")


class CountBucket(BaseModel):
    ts: str = Field(description="Start of the bucket, accepted back as from.")
    count: int
    top: list[CountGroup] = Field(default_factory=list, description="Most frequent values in the bucket.")


class CountResult(BaseModel):
    case: str
    by: str | None = None
    query: str | None = None
    total: int
    exact: bool = Field(
        default=True, description="False for a capped regex scan (lower bounds) or a sampled broad query (≈)."
    )
    distinct: int = Field(default=0, description="Distinct values of 'by'.")
    groups: list[CountGroup] = Field(default_factory=list)
    bucket: str | None = None
    buckets: list[CountBucket] | None = None
    warnings: list[str] = Field(default_factory=list)


class HitOut(BaseModel):
    record: RecordOut
    count: int = Field(description="Hits in this group (same template, or same pid).")
    last: str | None = Field(default=None, description="Citation of the last hit in the group.")
    followed_by: str | None = Field(default=None, description="followed_by rules: the second event.")
    open: bool | None = Field(default=None, description="Absence rules: the log ends inside the window.")


class RuleHits(BaseModel):
    rule: str
    severity: str
    description: str
    count: int
    exact: bool = True
    first_ts: str | None = None
    last_ts: str | None = None
    hits: list[HitOut] = Field(default_factory=list)
    more_groups: int = 0
    note: str | None = None
    search: str | None = Field(default=None, description="A search call that lists every hit.")


class DetectResult(BaseModel):
    case: str
    rules_run: int
    results: list[RuleHits] = Field(description="Rules with hits, by severity and first time.")
    no_hits: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class TimeBucket(BaseModel):
    ts: str
    count: int
    errors: int = Field(description="Records of level E or F.")


class GapOut(BaseModel):
    before: str = Field(description="Last record before the gap.")
    after: str = Field(description="First record after it.")
    from_ts: str
    to_ts: str
    seconds: float


class BurstOut(BaseModel):
    from_ts: str
    to_ts: str
    errors: int
    first: str = Field(description="First E/F record of the burst.")
    top_tag: str | None = None


class SegmentOut(BaseModel):
    source: str
    seg: int
    reason: str
    first: str
    ts: str | None = None
    records: int


class OverviewResult(BaseModel):
    case: str
    sources: int
    records: int
    ts_from: str | None = None
    ts_to: str | None = None
    mono_from: str | None = Field(default=None, description="Records with only a monotonic clock: first.")
    mono_to: str | None = None
    levels: dict[str, int] = Field(default_factory=dict, description="Records per level ('-': unknown).")
    top_tags: list[CountGroup] = Field(default_factory=list)
    top_processes: list[CountGroup] = Field(default_factory=list)
    top_templates: list[CountGroup] = Field(default_factory=list)
    segments: list[SegmentOut] = Field(default_factory=list, description="Clock restarts, boots, jumps.")
    gaps: list[GapOut] = Field(default_factory=list, description="More than 30 s without a record.")
    gaps_complete: bool = True
    bursts: list[BurstOut] = Field(default_factory=list, description="E/F per 10 s above 5× the median.")
    histogram_step: str | None = None
    histogram: list[TimeBucket] = Field(default_factory=list)
    detect: dict[str, int] = Field(default_factory=dict, description="Detector rules with hits.")
    artifacts: dict[str, int] = Field(default_factory=dict, description="Artifacts per type.")
    exact: bool = True
    warnings: list[str] = Field(default_factory=list)


class CitationIssue(BaseModel):
    line: int = Field(description="Line of the checked text.")
    cite: str | None = None
    message: str


class CheckCitationsResult(BaseModel):
    case: str
    ok: bool = Field(description="True when every citation is well-formed and resolves (warnings allowed).")
    citations: int
    distinct: int
    resolved: int
    paragraphs: int
    uncited: int = Field(description="Paragraphs with a finding but no citation (warnings).")
    errors: list[CitationIssue] = Field(default_factory=list)
    warnings: list[CitationIssue] = Field(default_factory=list)


@dataclass
class Output:
    text: str
    data: BaseModel


# -- time parsing -------------------------------------------------------------------------------------

_FULL = re.compile(
    r"(\d{4})-(\d\d)-(\d\d)(?:[ T](\d\d):(\d\d)(?::(\d\d)(?:[.,](\d{1,9}))?)?)?\s*(?:[+-]\d\d:?\d\d|Z|UTC)?"
)
_SHORT = re.compile(r"(\d\d)-(\d\d)[ T](\d\d):(\d\d)(?::(\d\d)(?:[.,](\d{1,9}))?)?")
_CLOCK = re.compile(r"(\d\d):(\d\d)(?::(\d\d)(?:[.,](\d{1,9}))?)?")
_MONO = re.compile(r"\[?\s*(\d+)\.(\d{1,9})\s*\]?")
_DUR_PART = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)")
_DUR = r"(?:\d+(?:\.\d+)?\s*(?:ms|s|m|h|d)\s*)+"
_REL = re.compile(rf"([+-])\s*({_DUR})")
_CITE_OFFSET = re.compile(rf"(log:\d+#\d+|art:\d+)\s*(?:([+-])\s*({_DUR}))?")
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


def _gran(sec: str | None, frac: str | None, minute: str | None = "0") -> int:
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


_REGEX_HINT = re.compile(
    r"\\[dDwWsSbB.]|\.\*|\.\+|\[[^\]\s]+\]|\(\?|\w\|\w|^\^|\$$|\{\d+(,\d*)?\}|\w\+\s|\)\?"
)


def looks_like_regex(text: str) -> bool:
    return bool(_REGEX_HINT.search(text))


# -- the tools ------------------------------------------------------------------------------------------


@dataclass
class _Ctx:
    """One call's view of a case: visible sources and an open connection."""

    case: str
    conn: sqlite3.Connection
    sources: list[query.SourceRow]
    hidden: bool  # the policy hides some sources

    by_id: dict[int, query.SourceRow] = field(init=False)

    def __post_init__(self) -> None:
        self.by_id = {s.id: s for s in self.sources}

    def ts_format(self, sid: int) -> str | None:
        s = self.by_id.get(sid)
        return s.ts_format if s else None


class Tools:
    def __init__(self, app: App) -> None:
        self.app = app
        self.caps = app.config.caps
        self.clock = ClockTools(self)

    # -- plumbing ---------------------------------------------------------------------------------

    @contextmanager
    def _case(self, case: str) -> Iterator[_Ctx]:
        path = self.app.config.case_db(case)
        if not path.exists():
            raise ToolInputError(f"case '{case}' does not exist; add a log with add_source(case, path) first")
        with db.opened(path) as conn:
            all_rows = query.sources(conn, self.app.versions(), self.app.redactor.rules_hash)
            visible = [s for s in all_rows if not self.app.blocked(s.name, s.path)]
            yield _Ctx(case, conn, visible, len(visible) != len(all_rows))

    def _rec_out(self, r: Rec, ctx: _Ctx, *, full: bool = False, chars: int | None = None) -> RecordOut:
        attrs = r.attrs
        art = attrs.get("art")
        if full:
            msg, cut = r.msg, False
        else:
            msg, cut = fmt.clip(r.msg, chars or self.caps.max_msg_chars)
        out = RecordOut(
            cite=r.cite,
            ts=fmt.full_ts(r.ts, r.ts_src),
            level=r.level,
            tag=r.tag,
            pid=r.pid,
            tid=r.tid,
            msg=msg,
            cut=cut or None,
            art=f"art:{art}" if isinstance(art, int) else None,
        )
        if full:
            out.ts_raw = ts_raw_of(r.ts_raw, r.ts, r.ts_src, ctx.ts_format(r.source_id))
            out.attrs = attrs or None
            out.source = str(r.source_id)
        return out

    @staticmethod
    def line(o: RecordOut, short: str, last_short: str | None = None, approx: bool = False) -> str:
        """``log:2#18233  10-11 08:12:03.412  E 1234/1250 HvacCtrl  message``"""
        who = (
            f"{o.pid}/{o.tid}"
            if o.pid is not None and o.tid is not None
            else (str(o.pid) if o.pid is not None else "")
        )
        head = " ".join(p for p in (o.level or "-", who, o.tag or "") if p)
        text = f"{o.cite}  {short}  {head}  {o.msg}"
        if o.count and o.count > 1:
            text += f"  ×{'≈' if approx else ''}{o.count:,} until {last_short} ({o.last})"
        if o.art and not o.msg.startswith("[art:"):
            text += f"  → {o.art}"
        return text

    # -- filter -----------------------------------------------------------------------------------

    def resolve_filter(self, ctx: _Ctx, f: Filter | None, warnings: list[str]) -> RecordFilter:
        rf = RecordFilter()
        f = f or Filter.model_validate({})
        visible_ids = [s.id for s in ctx.sources]
        if f.sources:
            ids: list[int] = []
            for s in f.sources:
                text = s.strip().removeprefix("log:")
                if not text.isdigit() or int(text) not in ctx.by_id:
                    raise ToolInputError(
                        f"unknown source {s!r}; sources in case '{ctx.case}': "
                        f"{_id_ranges(visible_ids) or 'none'} (see list_sources)"
                    )
                ids.append(int(text))
            rf.sources = sorted(set(ids))
        elif ctx.hidden:
            rf.sources = visible_ids
        selected = [ctx.by_id[i] for i in (rf.sources if rf.sources is not None else visible_ids)]
        span = _span(selected)
        lo = self.resolve_time(ctx, f.from_, "from", span, None) if f.from_ else None
        hi = self.resolve_time(ctx, f.to, "to", span, lo) if f.to else None
        if lo and hi and lo.clock != hi.clock:
            raise ToolInputError(
                "'from' and 'to' use different clocks (wall-clock time vs seconds since boot); "
                "use the same kind for both"
            )
        either = lo or hi
        clock = either.clock if either else None
        if clock == "wall":
            rf.ts_from = lo.ns if lo else None
            rf.ts_to = hi.ns + hi.gran - 1 if hi else None
            mono_only = [s.id for s in selected if s.ts_min is None and s.records]
            if mono_only:
                warnings.append(
                    f"records of {_sources_word(mono_only)} only a monotonic clock (seconds since boot) "
                    "and are left out by a wall-clock from/to; filter them with e.g. from='120.5'"
                )
        elif clock == "mono":
            rf.mono_from = lo.ns if lo else None
            rf.mono_to = hi.ns + hi.gran - 1 if hi else None
        if lo and hi and (lo.ns > hi.ns + hi.gran - 1):
            raise ToolInputError(f"'from' ({f.from_}) is after 'to' ({f.to})")
        rf.min_level = f.level
        redact = self.app.redactor.redact
        if f.tags:
            globs = [redact(g) for g in f.tags]
            rf.tag_ids, unmatched = query.tag_ids(ctx.conn, globs)
            for g in unmatched:
                similar = query.similar_tags(ctx.conn, g)
                warnings.append(
                    f"tag '{g}' matches no tag in the case (tags are case-sensitive)"
                    + (f"; similar: {', '.join(similar)}" if similar else "")
                )
        if f.not_tags:
            rf.not_tag_ids, unmatched = query.tag_ids(ctx.conn, [redact(g) for g in f.not_tags])
            for g in unmatched:
                warnings.append(f"not_tags '{g}' matches no tag in the case")
        rf.pid = f.pid
        if f.attrs:
            attrs: dict[str, Any] = {}
            for k, v in f.attrs.items():
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", k):
                    raise ToolInputError(f"invalid attrs key {k!r}: letters, digits, '_', '.', '-'")
                attrs[k] = redact(v) if isinstance(v, str) else v
            rf.attrs = attrs
        return rf

    def resolve_time(self, ctx: _Ctx, spec: str, role: str, span: Span, other: Bound | None) -> Bound:
        """A from/to value as a bound on the wall clock or on the monotonic clock."""
        text = spec.strip()
        m = _FULL.fullmatch(text)
        if m:
            y, mo, d = int(m[1]), int(m[2]), int(m[3])
            try:
                ns = wall_ns(y, mo, d, int(m[4] or 0), int(m[5] or 0), int(m[6] or 0), m[7] or "")
            except ValueError:
                raise ToolInputError(f"invalid date or time in {role}={spec!r}") from None
            return Bound("wall", ns, _gran(m[6], m[7], m[4]))
        m = _SHORT.fullmatch(text)
        if m:
            mo, d = int(m[1]), int(m[2])
            year = self._year_for(span, mo, d, role, spec)
            try:
                ns = wall_ns(year, mo, d, int(m[3]), int(m[4]), int(m[5] or 0), m[6] or "")
            except ValueError:
                raise ToolInputError(f"invalid date or time in {role}={spec!r}") from None
            return Bound("wall", ns, _gran(m[5], m[6]))
        m = _CLOCK.fullmatch(text)
        if m:
            return self._clock_time(span, m, role, spec)
        m = _MONO.fullmatch(text)
        if m:
            return Bound("mono", int(m[1]) * NS + int(m[2].ljust(9, "0")), 10 ** (9 - len(m[2])))
        m = _REL.fullmatch(text)
        if m:
            delta = duration_ns(m[2])
            if m[1] == "-":
                base = span.wall[1] if span.wall else (span.mono[1] if span.mono else None)
                clock: Literal["wall", "mono"] = "wall" if span.wall else "mono"
                if base is None:
                    raise ToolInputError(f"{role}={spec!r}: the selected sources have no timestamps")
                return Bound(clock, base - delta)
            if role == "to" and other is not None:
                return Bound(other.clock, other.ns + delta)
            if span.wall:
                return Bound("wall", span.wall[0] + delta)
            if span.mono:
                return Bound("mono", span.mono[0] + delta)
            raise ToolInputError(f"{role}={spec!r}: the selected sources have no timestamps")
        m = _CITE_OFFSET.fullmatch(text)
        if m:
            b = self._cite_time(ctx, m[1], role)
            if m[2]:
                delta = duration_ns(m[3])
                b = Bound(b.clock, b.ns + delta if m[2] == "+" else b.ns - delta, b.gran)
            return b
        raise ToolInputError(f"cannot read {role}={spec!r}; use {TIME_FORMS}")

    def _year_for(self, span: Span, mo: int, d: int, role: str, spec: str) -> int:
        if span.wall is None:
            raise ToolInputError(
                f"{role}={spec!r} has no year and the selected sources have no wall-clock time"
            )
        lo, hi = span.wall
        y0, y1 = int(fmt.full_ts(lo)[:4]), int(fmt.full_ts(hi)[:4])  # type: ignore[index]
        day = 86400 * NS
        fits = []
        for y in range(y0, y1 + 1):
            try:
                t = wall_ns(y, mo, d, 0, 0, 0)
            except ValueError:
                continue
            if lo - day <= t <= hi + day:
                fits.append(y)
        if len(fits) > 1:
            raise ToolInputError(
                f"{role}={spec!r} is ambiguous: the case spans {y0}-{y1}; give the year (YYYY-MM-DD hh:mm:ss)"
            )
        if fits:
            return fits[0]
        return y1 if abs(wall_ns(y1, 1, 1, 0, 0, 0) - hi) < abs(wall_ns(y0, 1, 1, 0, 0, 0) - lo) else y0

    def _clock_time(self, span: Span, m: re.Match[str], role: str, spec: str) -> Bound:
        if span.wall is None:
            raise ToolInputError(
                f"{role}={spec!r} has no date and the selected sources have no wall-clock time"
            )
        lo, hi = span.wall
        day = 86400 * NS
        tod = (int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3] or 0)) * NS + (
            int(m[4].ljust(9, "0")) if m[4] else 0
        )
        gran = _gran(m[3], m[4])
        days = range(lo // day, hi // day + 1)
        fits = [dd * day + tod for dd in days if lo <= dd * day + tod + gran - 1 and dd * day + tod <= hi]
        if len(fits) == 1:
            return Bound("wall", fits[0], gran)
        if not fits:
            raise ToolInputError(f"{role}={spec!r} is outside the case's time range; give the date")
        raise ToolInputError(
            f"{role}={spec!r} matches several days of the case; give the date (MM-DD hh:mm:ss)"
        )

    def _cite_time(self, ctx: _Ctx, cite: str, role: str) -> Bound:
        if cite.startswith("art:"):
            aid = int(cite[4:])
            a = query.artifact(ctx.conn, aid)
            if a is None or a.source_id not in ctx.by_id:
                raise ToolInputError(f"{role}: no artifact {cite} in case '{ctx.case}'")
            rid = a.record_id
        else:
            sid, seq = parse_log_cite(cite)
            if sid not in ctx.by_id:
                raise ToolInputError(f"{role}: no source {sid} in case '{ctx.case}'")
            rid = (sid << SEQ_BITS) | seq
        rec = query.records_by_id(ctx.conn, [rid]).get(rid)
        if rec is None:
            raise ToolInputError(f"{role}: no record {cite} in case '{ctx.case}'")
        if rec.ts is not None:
            return Bound("wall", rec.ts)
        if rec.ts_src is not None:
            return Bound("mono", rec.ts_src)
        raise ToolInputError(f"{role}: {cite} has no timestamp")

    # -- search -----------------------------------------------------------------------------------

    def search(
        self,
        case: str,
        text: str | None,
        mode: str = "text",
        filter: Filter | None = None,
        collapse: bool = True,
        count_only: bool = False,
        limit: int = 50,
        cursor: str | None = None,
        busy: bool = False,
    ) -> Output:
        """``text`` None (an empty MCP query) matches every record that passes the filter."""
        warnings: list[str] = []
        q = self.app.redactor.redact(text.strip()) if text is not None else ""
        if text is not None and not q:
            raise ToolInputError("query is empty")
        if text is not None and q != text.strip():
            warnings.append("raw values in the query were redacted to tokens like <MAC:…>, as in the logs")
        rx: re.Pattern[str] | None = None
        if mode == "regex":
            try:
                rx = re.compile(q)
            except re.error as e:
                raise ToolInputError(f"invalid regex {q!r}: {e}") from None
        elif q and looks_like_regex(q):
            warnings.append(
                "the query looks like a regular expression, but mode='text' matches words (FTS5); "
                "pass mode='regex' to match it as one"
            )
        limit_eff = min(limit, self.caps.max_lines)
        if limit > limit_eff:
            warnings.append(f"limit {limit} reduced to {limit_eff} (config caps.max_lines)")
        key = fmt.query_key(
            {
                "c": case,
                "q": q,
                "m": mode,
                "f": filter.model_dump(by_alias=True, exclude_none=True) if filter else None,
                "col": collapse,
            }
        )
        offset = 0
        if cursor:
            try:
                offset = fmt.read_cursor(cursor, key)
            except fmt.CursorError as e:
                raise ToolInputError(str(e)) from None
        with self._case(case) as ctx:
            rf = self.resolve_filter(ctx, filter, warnings)
            where, params = rf.where()
            fts: str | None = None
            if rx is None and q:
                fts = query.fts_query(q)
                where = [query.FTS_MATCH, *where]
                params = [fts, *params]
            elif rx is not None:
                pre_where, pre_params = query.regex_prefilter(q)
                where, params = [*pre_where, *where], [*pre_params, *params]
            exact = True
            sample = 1
            n_fts = 0
            scan: query.ScanResult | None = None
            try:
                if rx is None:
                    n_fts = query.fts_count(ctx.conn, fts) if fts else 0
                    if fts and n_fts > AGG_ROWS:
                        # Looking up every match costs ~1.3 µs; above AGG_ROWS group a 1/k sample.
                        sample = -(-n_fts // AGG_ROWS)
                        swhere = [query.FTS_SAMPLE, *where[1:]]
                        groups = query.aggregate(ctx.conn, swhere, [fts, sample, *params[1:]])
                        for g in groups:
                            g.count *= sample
                            g.n_wall *= sample
                        exact = False
                        warnings.append(
                            f"{n_fts:,} records contain the words: counts (≈) and groups are estimated from "
                            f"every {sample}th of them, so rare messages may be missing and ×counts and "
                            "first/last times are approximate; narrow with level, tags, from/to or a longer "
                            "query for exact results"
                        )
                    else:
                        groups = query.aggregate(ctx.conn, where, params)
                else:
                    window = 0 if (collapse or count_only) else offset + limit_eff
                    scan = query.regex_scan(ctx.conn, where, params, rx, self.caps.regex_scan_rows, window)
                    groups, exact = scan.groups, scan.complete
            except sqlite3.OperationalError as e:
                raise ToolInputError(_fts_error(q, e)) from None
            total = sum(g.count for g in groups)
            if sample > 1 and not rf.parts():
                total = n_fts  # nothing but the words: the FTS count is exact
            approx = "at least " if (scan and not exact) else ("≈" if sample > 1 and rf.parts() else "")
            if total == 0:
                reason = self._empty_reason(ctx, q, fts, rx, rf, warnings)
                if busy:
                    reason += " An import is still running in this case; more records may arrive."
                data = SearchResult(
                    case=case,
                    mode=mode,
                    query=q,
                    total=0,
                    exact=exact,
                    groups=0,
                    hits=[],
                    warnings=warnings,
                    empty_reason=reason,
                )
                return Output(_join(["No matches. " + reason, *_warn_lines(warnings)]), data)
            if count_only:
                by_level = Counter[str]()
                for g in groups:
                    by_level[g.level or "-"] += g.count
                levels = ", ".join(f"{lvl} {n:,}" for lvl, n in sorted(by_level.items(), key=_level_order))
                head = (
                    f"{approx}{fmt.plural(total, 'match', 'matches')}, "
                    f"{fmt.plural(len(groups), 'distinct message', 'distinct messages')}"
                )
                lines = [f"{head} ({levels})"]
                if scan and not exact:
                    lines.append(_scan_note(scan, self.caps.regex_scan_rows))
                lines.extend(self._other_sources(ctx, filter, rf, fts, rx))
                data = SearchResult(
                    case=case,
                    mode=mode,
                    query=q,
                    total=total,
                    exact=exact,
                    groups=len(groups),
                    hits=[],
                    warnings=warnings,
                )
                return Output(_join([*lines, *_warn_lines(warnings)]), data)

            page = fmt.Page(
                self.caps.max_lines, self.caps.max_bytes, reserve=_reserve(self.caps.max_bytes, 1400)
            )
            # (record, output, short time, short time of the last match)
            hits: list[tuple[Rec, RecordOut, str, str | None]] = []
            if collapse:
                window_groups = groups[offset : offset + limit_eff]
                firsts = query.records_by_id(ctx.conn, [g.first_id for g in window_groups])
                lasts = query.records_by_id(ctx.conn, [g.last_id for g in window_groups if g.count > 1])
                for g in window_groups:
                    r = firsts[g.first_id]
                    o = self._rec_out(r, ctx)
                    last_short = None
                    if g.count > 1:
                        last = lasts[g.last_id]
                        o.count, o.last, o.last_ts = g.count, last.cite, fmt.full_ts(last.ts, last.ts_src)
                        last_short = fmt.short_ts(last.ts, last.ts_src)
                    hits.append((r, o, fmt.short_ts(r.ts, r.ts_src), last_short))
                n_items = len(groups)
            else:
                if scan is not None:
                    recs = scan.first[offset : offset + limit_eff]
                else:
                    n_wall = sum(g.n_wall for g in groups)
                    recs = query.select_ordered(
                        ctx.conn,
                        where,
                        params,
                        offset,
                        limit_eff,
                        n_wall,
                        by_time_index=n_fts > TIME_INDEX_ROWS,
                    )
                hits = [(r, self._rec_out(r, ctx), fmt.short_ts(r.ts, r.ts_src), None) for r in recs]
                n_items = total
            tmpl = query.template_texts(ctx.conn, [r.template_id for r, *_ in hits if r.template_id])
            shown: list[RecordOut] = []
            for r, o, short, last_short in hits:
                if not page.add(self.line(o, short, last_short, sample > 1)):
                    break
                t = tmpl.get(r.template_id) if r.template_id else None
                o.template = t
                # A synthetic artifact record's text is ours, not the program's: nothing to search for.
                o.literal = None if r.msg.startswith("[art:") else fmt.template_literal(t)
                shown.append(o)
            end = offset + len(shown)
            more = n_items - end
            header = (
                f"{approx}{fmt.plural(total, 'match', 'matches')}"
                + (
                    f", {fmt.plural(len(groups), 'distinct message', 'distinct messages')} (collapsed)"
                    if collapse
                    else ""
                )
                + (f"; showing {offset + 1}-{end}" if offset or more > 0 else "")
                + ":"
            )
            lit_lines = _literal_lines(shown)
            footer: list[str] = []
            next_cursor = None
            if more > 0:
                next_cursor = fmt.make_cursor(end, key)
                what = (
                    fmt.plural(more, "more distinct message", "more distinct messages")
                    if collapse
                    else fmt.plural(more, "more match", "more matches")
                )
                footer.append(
                    f'… {what}; next page: cursor="{next_cursor}"; or narrow by '
                    + self._narrow_hint(ctx, groups, rf)
                )
            if scan and not exact:
                footer.append(_scan_note(scan, self.caps.regex_scan_rows))
            footer.extend(self._other_sources(ctx, filter, rf, fts, rx))
            if busy:
                footer.append("An import is still running in this case; counts may grow.")
            text_out = _join([header, *page.lines, *lit_lines, *footer, *_warn_lines(warnings)])
            data = SearchResult(
                case=case,
                mode=mode,
                query=q,
                total=total,
                exact=exact,
                groups=len(groups),
                hits=shown,
                next_cursor=next_cursor,
                warnings=warnings,
            )
            return Output(text_out, data)

    def _other_sources(
        self,
        ctx: _Ctx,
        filter: Filter | None,
        rf: RecordFilter,
        fts: str | None,
        rx: re.Pattern[str] | None,
    ) -> list[str]:
        """With a ``sources`` filter: how many matches the other (visible) sources have, so a
        filter does not hide them silently. Text queries only (a regex would mean another scan)."""
        if not (filter and filter.sources) or rf.sources is None:
            return []
        others = [s.id for s in ctx.sources if s.id not in set(rf.sources) and s.records]
        if not others:
            return []
        if fts is None:
            if rx is None:
                return []
            ids = ", ".join(f"log:{i}" for i in others[:8]) + (", …" if len(others) > 8 else "")
            return [f"Not searched (sources filter): {ids}."]
        where, params = rf.where(frozenset({"sources"}))
        where = [
            query.FTS_MATCH,
            "(" + " OR ".join("r.id BETWEEN ? AND ?" for _ in others) + ")",
            *where,
        ]
        bounds: list[Any] = []
        for sid in others:
            bounds.extend([sid << SEQ_BITS, ((sid + 1) << SEQ_BITS) - 1])
        try:
            counts = query.count_by_source(ctx.conn, where, [fts, *bounds, *params])
        except sqlite3.OperationalError:
            return []
        if not counts:
            return ["No matches in the other sources either."]
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        listed = ", ".join(f"log:{sid} {n:,}" for sid, n in top[:6]) + (", …" if len(top) > 6 else "")
        n = sum(counts.values())
        return [
            f"Outside the sources filter: {fmt.plural(n, 'more match', 'more matches')} ({listed}); "
            "add them to 'sources' or drop it to see them."
        ]

    def _narrow_hint(self, ctx: _Ctx, groups: list[query.Group], rf: RecordFilter) -> str:
        parts: list[str] = []
        by_level = Counter[str]()
        by_tag = Counter[int | None]()
        for g in groups:
            by_level[g.level or "-"] += g.count
            by_tag[g.tag_id] += g.count
        if not rf.min_level and len(by_level) > 1:
            parts.append(
                "level ("
                + ", ".join(f"{lvl} {n:,}" for lvl, n in sorted(by_level.items(), key=_level_order))
                + ")"
            )
        if len(by_tag) > 1:
            top = by_tag.most_common(4)
            names = query.tag_names(ctx.conn, [t for t, _ in top])
            parts.append(
                "tags ("
                + ", ".join(f"{names.get(t, '-') if t is not None else '-'} {n:,}" for t, n in top)
                + (", …" if len(by_tag) > 4 else "")
                + ")"
            )
        walls = [g.first_ts for g in groups if g.first_ts is not None]
        if walls:
            parts.append(f"from/to (first match {fmt.short_ts(min(walls))})")
        parts.append("a longer query")
        return ", ".join(parts)

    def _empty_reason(
        self,
        ctx: _Ctx,
        q: str,
        fts: str | None,
        rx: re.Pattern[str] | None,
        rf: RecordFilter,
        warnings: list[str],
    ) -> str:
        if not ctx.sources:
            return f"Case '{ctx.case}' has no sources; add one with add_source."
        if not any(s.records for s in ctx.sources):
            return "The sources of this case have no records yet."
        conn = ctx.conn
        base_where: list[str] = []
        base_params: list[Any] = []
        if ctx.hidden:
            base_where, base_params = RecordFilter(sources=[s.id for s in ctx.sources]).where()

        def count(skip: frozenset[str] | None) -> int:
            if skip is None:
                where, params = list(base_where), list(base_params)
            else:
                where, params = rf.where(skip)
            if fts is not None:
                where, params = [query.FTS_MATCH, *where], [fts, *params]
            if rx is None:
                return query.count_candidates(conn, where, params)
            return sum(
                g.count
                for g in query.regex_scan(conn, where, params, rx, self.caps.regex_scan_rows, 0).groups
            )

        parts = rf.parts()
        unfiltered = count(None) if parts else 0
        if not parts or unfiltered == 0:
            if fts is None and rx is None:
                return "No record passes the filter."
            if fts is None:
                return f"The regex {q!r} matches no record in the case (message and tag, case-sensitive)."
            words = [w for w in q.split() if w not in ("AND", "OR", "NOT") and not w.startswith("NEAR(")]
            missing = []
            for w in words[:6]:
                try:
                    if query.fts_count(conn, query.fts_query(w)) == 0:
                        missing.append(w)
                except sqlite3.OperationalError:
                    pass
            what = (
                f"no record contains {', '.join(repr(w) for w in missing)}"
                if missing
                else "no record contains all words of the query"
            )
            return (
                f"In the whole case {what}. Text search matches whole words (case-insensitive): use "
                "'word*' for a prefix, or mode='regex' for substrings and patterns."
            )
        hints = []
        for part in parts:
            n = count(frozenset({part}))
            if n:
                hints.append(f"{n:,} without the {_PART_NAMES[part]}{_part_value(rf, part)}")
        if hints:
            found = fmt.plural(unfiltered, "match", "matches")
            return f"{found} in the case, none with the filter: " + "; ".join(hints) + "."
        found = fmt.plural(unfiltered, "match", "matches")
        return f"{found} in the case, but none pass the combined filter; relax several parts."

    # -- context ----------------------------------------------------------------------------------

    def context(self, case: str, at: str, before: int = 20, after: int = 20, scope: str = "source") -> Output:
        with self._case(case) as ctx:
            rid = self._anchor(ctx, at)
            anchor = query.records_by_id(ctx.conn, [rid]).get(rid)
            if anchor is None:
                sid = rid >> SEQ_BITS
                s = ctx.by_id[sid]
                raise ToolInputError(
                    f"no record {citation(sid, rid & SEQ_MASK)}; source {sid} has {s.records} records"
                )
            cap = self.caps.max_lines - 1
            b = min(before, max(cap - min(after, cap // 2), 0))
            a = min(after, cap - b)
            note: list[str] = []
            if (b, a) != (before, after):
                note.append(f"before/after reduced to {b}/{a} (config caps.max_lines)")
            if scope == "pid":
                if anchor.pid is None:
                    raise ToolInputError(f"{anchor.cite} has no pid; use scope='source'")
                same_file = [s for s in ctx.sources if s.sha256 == ctx.by_id[anchor.source_id].sha256]
                srcs = [
                    (s.id, s.records) for s in same_file if s.id == anchor.source_id or s.ts_min is not None
                ]
                seg = query.segment_of(ctx.conn, anchor.source_id, anchor.seq)
                recs = query.pid_neighbours(ctx.conn, anchor, srcs, b, a, seg)
                ids = sorted({r.source_id for r in recs})
                head = f"pid {anchor.pid} around {anchor.cite}, sources {_id_ranges(ids)} by time:"
            elif scope == "all":
                texts_all, outs_all, head, extra_all = self.clock.context_all(ctx, anchor, b, a)
            else:
                recs = query.source_neighbours(ctx.conn, rid, b, a)
                head = f"source {anchor.source_id} around {anchor.cite}:"
            if scope == "all":
                idx = next(i for i, t in enumerate(texts_all) if t.startswith("> "))
                outs: list[Any] = outs_all
                texts = texts_all
            else:
                extra_all = []
                idx = next(i for i, r in enumerate(recs) if r.id == rid)
                outs = [self._rec_out(r, ctx) for r in recs]
                texts = [
                    ("> " if i == idx else "  ") + self.line(o, fmt.short_ts(r.ts, r.ts_src))
                    for i, (r, o) in enumerate(zip(recs, outs, strict=True))
                ]
            lo, hi = 0, len(texts)
            budget = self.caps.max_bytes - 700
            size = sum(fmt.utf8_len(t) + 1 for t in texts)
            while size > budget and hi - lo > 1:
                if idx - lo >= hi - 1 - idx:
                    size -= fmt.utf8_len(texts[lo]) + 1
                    lo += 1
                else:
                    hi -= 1
                    size -= fmt.utf8_len(texts[hi]) + 1
            if (lo, hi) != (0, len(texts)):
                note.append(
                    f"output cap of {self.caps.max_bytes} bytes: {lo + len(texts) - hi} lines left out"
                )
            shown = outs[lo:hi]
            lines = [head, *texts[lo:hi], *extra_all]
            if scope == "source":
                first, last = recs[lo], recs[hi - 1]
                s = ctx.by_id[anchor.source_id]
                at_start = first.seq == 1 and lo == 0
                at_end = last.seq >= s.records and hi == len(recs)
                more = []
                if at_start:
                    lines.append("(start of source)")
                else:
                    more.append(f"context(at='{first.cite}', before={max(b, 20)}, after=0) earlier")
                if at_end:
                    lines.append("(end of source)")
                else:
                    more.append(f"context(at='{last.cite}', before=0, after={max(a, 20)}) later")
                if more:
                    lines.append("more: " + ", ".join(more))
                if len(recs) < b + a + 1 and anchor.pid is not None and len(ctx.sources) > 1:
                    lines.append(
                        f"few lines in source {s.id}; context(at='{anchor.cite}', scope='pid') shows "
                        f"pid {anchor.pid} in the other sources of the same file"
                    )
            if anchor.attrs.get("art") is not None:
                lines.append(
                    f"{anchor.cite} is the record of art:{anchor.attrs['art']}: "
                    f"read_artifact(id='art:{anchor.attrs['art']}')"
                )
            elif any(o.cut for o in shown):
                lines.append("messages cut with … are complete in get_records(ids=[…])")
            lines += [f"note: {n}" for n in note]
            data = ContextResult(
                case=case, at=anchor.cite, scope=scope, records=shown, note="; ".join(note) or None
            )
            return Output(_join(lines), data)

    def timeline(
        self,
        case: str,
        filter: Filter | None = None,
        query_text: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Output:
        out: Output = self.clock.timeline(case, filter, query_text, limit, cursor)
        return out

    def align(
        self,
        case: str,
        mode: str = "auto",
        source: str | None = None,
        seg: int | None = None,
        offset: str | None = None,
        at: str | None = None,
        ref: str | None = None,
    ) -> Output:
        out: Output = self.clock.align(case, mode, source, seg, offset, at, ref)
        return out

    def clock_summary(self, case: str) -> tuple[str, dict[str, str]]:
        """(case clock name, short clock text per source id) for list_sources; exact sources omitted."""
        with self._case(case) as ctx:
            cc = self.clock.model(ctx)
            out: dict[str, str] = {}
            for row in self.clock.align_rows(ctx, cc):
                if row.conf == "exact" or row.source in out:
                    continue
                if not row.placed:
                    out[row.source] = "not placed (no anchor; see align)"
                    continue
                out[row.source] = " ".join(p for p in ("~" + (row.conf or ""), row.error) if p)
            return self.clock.clock_name(ctx, cc), out

    def _anchor(self, ctx: _Ctx, at: str) -> int:
        at = at.strip()
        m = ART_ID.fullmatch(at)
        if m:
            a = query.artifact(ctx.conn, int(m[1]))
            if a is None or a.source_id not in ctx.by_id:
                raise ToolInputError(f"no artifact {at} in case '{ctx.case}' (see list_artifacts)")
            return a.record_id
        sid, seq = parse_log_cite(at)
        if sid not in ctx.by_id:
            raise ToolInputError(
                f"no source {sid} in case '{ctx.case}'; sources: {_id_ranges(list(ctx.by_id)) or 'none'}"
            )
        return (sid << SEQ_BITS) | seq

    # -- get_records ------------------------------------------------------------------------------

    def get_records(self, case: str, ids: list[str]) -> Output:
        with self._case(case) as ctx:
            wanted: list[tuple[str, int | None]] = []
            for cite in ids:
                sid, seq = parse_log_cite(cite)
                wanted.append((citation(sid, seq), (sid << SEQ_BITS) | seq if sid in ctx.by_id else None))
            recs = query.records_by_id(ctx.conn, [rid for _, rid in wanted if rid is not None])
            tmpl = query.template_texts(ctx.conn, [r.template_id for r in recs.values() if r.template_id])
            out: list[RecordOut] = []
            missing: list[str] = []
            not_shown: list[str] = []
            blocks: list[str] = []
            size = 0
            budget = self.caps.max_bytes - _reserve(self.caps.max_bytes, 700)
            seen: set[str] = set()
            for cite, rid in wanted:
                if cite in seen:
                    continue
                seen.add(cite)
                r = recs.get(rid) if rid is not None else None
                if r is None:
                    missing.append(cite)
                    continue
                o = self._rec_out(r, ctx, full=True)
                t = tmpl.get(r.template_id or -1)
                o.template, o.literal = t, fmt.template_literal(t)
                block = self._record_block(o)
                n = fmt.utf8_len(block) + 1
                if size + n > budget:
                    if not out:  # one record larger than the cap: cut its message
                        keep = max(budget - 600, 200)
                        o.msg = _cut_bytes(o.msg, keep)
                        o.cut = True
                        block = (
                            self._record_block(o)
                            + f"\n  … message cut at the output cap of {self.caps.max_bytes} bytes"
                        )
                        if o.art:
                            block += f"; the full text is in read_artifact(id='{o.art}')"
                        n = fmt.utf8_len(block) + 1
                    else:
                        not_shown.append(cite)
                        continue
                blocks.append(block)
                out.append(o)
                size += n
            if not out and missing:
                raise ToolInputError(
                    f"no such records in case '{case}': {', '.join(missing)} (citations log:<source>#<seq> "
                    "as other tools print them)"
                )
            lines = blocks
            if missing:
                lines.append(f"not found: {_some(missing)}")
            if not_shown:
                lines.append(
                    f"not shown (output cap of {self.caps.max_bytes} bytes): {_some(not_shown)}; "
                    "ask again for them"
                )
            return Output(
                _join(lines), GetRecordsResult(case=case, records=out, missing=missing, not_shown=not_shown)
            )

    @staticmethod
    def _record_block(o: RecordOut) -> str:
        who = []
        if o.pid is not None:
            who.append(f"pid {o.pid}")
        if o.tid is not None:
            who.append(f"tid {o.tid}")
        head = "  ".join(x for x in (o.cite, o.ts or "-", o.level or "-", " ".join(who), o.tag or "") if x)
        meta = []
        if o.ts_raw and o.ts_raw != o.ts:
            meta.append(f"as written: {o.ts_raw}")
        if o.attrs:
            meta.append("attrs: " + ", ".join(f"{k}={v}" for k, v in sorted(o.attrs.items())))
        if o.art:
            meta.append(f"artifact: {o.art}")
        if o.literal:
            meta.append(f'code search: "{o.literal}"')
        body = "\n".join("  " + x for x in o.msg.split("\n"))
        return "\n".join([head, *(["  " + " · ".join(meta)] if meta else []), body])

    # -- artifacts ------------------------------------------------------------------------------------

    def list_artifacts(self, case: str, type: str | None = None) -> Output:
        with self._case(case) as ctx:
            visible = [s.id for s in ctx.sources]
            every = query.artifacts(ctx.conn, None, visible)
            rows = [a for a in every if type is None or a.type == type]
            if not rows:
                if not every:
                    reason = (
                        f"No artifacts in case '{case}': no tombstone, ANR trace, Java crash or dropbox "
                        "entry "
                        "was found in its sources. Search the logs instead, e.g. "
                        "search(query='FATAL OR crash OR ANR', filter={'level': 'E'})."
                    )
                else:
                    types = Counter(a.type for a in every)
                    reason = f"No artifacts of type '{type}'; types in the case: " + ", ".join(
                        f"{t} ×{n}" for t, n in sorted(types.items())
                    )
                data = ListArtifactsResult(case=case, total=0, groups=[], empty_reason=reason)
                return Output(reason, data)
            groups = group_artifacts(rows)
            page = fmt.Page(
                self.caps.max_lines, self.caps.max_bytes, reserve=_reserve(self.caps.max_bytes, 500)
            )
            outs: list[ArtifactGroupOut] = []
            per_group = 3
            for g in groups:
                sig = g.signature or "(no signature)"
                n_copies = sum(1 for a in g.items if a.same_as)
                head = f"{g.type} ×{len(g.items)}" + (
                    f" ({n_copies} duplicate)"
                    if n_copies == 1
                    else f" ({n_copies} duplicates)"
                    if n_copies
                    else ""
                )
                if not page.add(f"{head}  {fmt.clip(sig, 300)[0]}"):
                    break
                go = ArtifactGroupOut(type=g.type, signature=g.signature, count=len(g.items), artifacts=[])
                for a in g.items[:per_group]:
                    ao = self._art_out(a)
                    when = fmt.short_ts(a.row.ts)
                    detail = (
                        f"same crash as {a.same_as} ({_origin(a.row)})"
                        if a.same_as
                        else fmt.clip(a.row.headline, 140)[0]
                    )
                    pid = f"pid {a.row.pid}" if a.row.pid is not None else "pid -"
                    if not page.add(
                        f"  {a.row.cite}  {when}  {pid}  {a.row.record_cite}  {a.row.lines} lines  {detail}"
                    ):
                        break
                    go.artifacts.append(ao)
                if not go.artifacts:
                    page.pop()  # no room for any of its artifacts: leave the group out
                    break
                rest = len(g.items) - len(go.artifacts)
                if rest and not page.full:
                    last = g.items[-1].row
                    page.add(
                        f"  … {rest} more, last {last.cite} at {fmt.short_ts(last.ts)}; "
                        f"list_artifacts(type='{g.type}')",
                        count_line=False,
                    )
                go.more = rest
                outs.append(go)
                if page.full:
                    break
            more_groups = len(groups) - len(outs)
            lines = [
                f"{fmt.plural(len(rows), 'artifact', 'artifacts')} in "
                f"{fmt.plural(len(groups), 'group', 'groups')} (same crash signature), by first time:",
                *page.lines,
            ]
            if more_groups > 0:
                lines.append(f"… {more_groups} more groups; narrow with list_artifacts(type=…)")
            lines.append(
                "read one: read_artifact(id='art:<id>') for its summary and sections; "
                "context(at='art:<id>') for the lines around its record"
            )
            data = ListArtifactsResult(
                case=case, total=len(rows), groups=outs, more_groups=max(more_groups, 0)
            )
            return Output(_join(lines), data)

    @staticmethod
    def _art_out(a: _GroupedArtifact) -> ArtifactOut:
        r = a.row
        return ArtifactOut(
            cite=r.cite,
            type=r.type,
            ts=fmt.full_ts(r.ts),
            pid=r.pid,
            process=r.process,
            record=r.record_cite,
            lines=r.lines,
            headline=fmt.clip(r.headline, 240)[0],
            same_as=a.same_as,
        )

    def read_artifact(
        self,
        case: str,
        id: str,
        section: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> Output:
        try:
            c = query.parse_art_citation(id)
        except ValueError as e:
            raise ToolInputError(str(e)) from None
        if section is not None:
            if c.section is not None and c.section != section:
                raise ToolInputError(f"section '{section}' conflicts with the citation {id}")
            c = query.ArtCitation(c.id, section, c.first, c.last)
        if start_line is not None or end_line is not None:
            if c.first is not None:
                raise ToolInputError(
                    "give lines either in the citation (:L<n>-L<m>) or as start_line/end_line"
                )
            first = start_line or None
            last = end_line
            c = query.ArtCitation(c.id, c.section, first, last)
        with self._case(case) as ctx:
            a = query.artifact(ctx.conn, c.id)
            if a is None or a.source_id not in ctx.by_id:
                raise ToolInputError(f"no artifact art:{c.id} in case '{case}' (see list_artifacts)")
            near = (
                query.artifacts_near(ctx.conn, a, SAME_CRASH_NS, [s.id for s in ctx.sources])
                if a.pid is not None and a.ts is not None
                else []
            )
            same = _same_crash(a, near)
            data = ReadArtifactResult(
                cite=a.cite,
                type=a.type,
                name=a.name,
                record=a.record_cite,
                ts=fmt.full_ts(a.ts),
                ts_raw=a.ts_raw,
                pid=a.pid,
                tid=a.tid,
                process=a.process,
                signature=a.signature,
                sections=[
                    SectionOut(name=s.name, cite=f"{a.cite}/{s.name}", first=s.first_line, last=s.last_line)
                    for s in a.sections
                ],
                same_crash=[o.cite for o in same],
            )
            if c.section is None and c.first is None and c.last is None:
                data.summary = a.summary
                return Output(self._artifact_summary(a, data, same), data)
            # Lines: a section, a range (start_line/end_line may give only one end).
            sec = next((s for s in a.sections if s.name == c.section), None) if c.section else None
            if c.section and sec is None:
                names = ", ".join(s.name for s in a.sections[:30])
                raise ToolInputError(f"{a.cite} has no section '{c.section}'; sections: {names}")
            lo_all, hi_all = (sec.first_line, sec.last_line) if sec else (1, a.lines)
            first = c.first if c.first is not None else lo_all
            last = c.last if c.last is not None else hi_all
            if c.first is not None and c.last is None and start_line is None:
                last = first
            try:
                lines = query.artifact_lines(ctx.conn, a, query.ArtCitation(a.id, c.section, first, last))
            except ValueError as e:
                raise ToolInputError(str(e)) from None
            single = len(lines) == 1
            cap = self.caps.max_lines * 2
            page = fmt.Page(cap, self.caps.max_bytes, reserve=_reserve(self.caps.max_bytes, 500))
            width = len(str(lines[-1][0])) if lines else 1
            shown: list[LineOut] = []
            for n, text in lines:
                t = fmt.one_line(text) if single else fmt.clip(text, self.caps.max_msg_chars)[0]
                if not page.add(f"L{n:<{width}}  {t}"):
                    break
                shown.append(LineOut(n=n, text=t))
            where = f"{a.cite}/{c.section}" if c.section else a.cite
            rng = (
                (f"L{shown[0].n}" + (f"-L{shown[-1].n}" if shown[-1].n != shown[0].n else ""))
                if shown
                else "-"
            )
            header = f"{where} {rng} ({a.type}, {fmt.clip(a.headline, 100)[0]}):"
            out = [header, *page.lines]
            if shown and shown[-1].n < last:
                nxt_first = shown[-1].n + 1
                rng_next = f"L{nxt_first}" + (f"-L{last}" if last > nxt_first else "")
                nxt = f"{a.cite}{'/' + c.section if c.section else ''}:{rng_next}"
                data.next = nxt
                out.append(f"… {rng_next} not shown ({fmt.cap_note(page)}); next: read_artifact(id='{nxt}')")
            if not single and any(len(t) > self.caps.max_msg_chars for _, t in lines[: len(shown)]):
                out.append(
                    "lines cut with … are complete when read one at a time: read_artifact(id='art:<id>:L<n>')"
                )
            out.append(f"cite lines as {a.cite}{'/' + c.section if c.section else ''}:L<n>")
            data.lines = shown
            return Output(_join(out), data)

    def _artifact_summary(self, a: ArtifactRow, data: ReadArtifactResult, same: list[ArtifactRow]) -> str:
        when = fmt.full_ts(a.ts) or "unknown"
        lines = [f"{a.cite}  {a.type}  record {a.record_cite}  ({a.lines} lines)"]
        lines.append(f"name: {a.name}")
        lines.append(
            f"time: {when}" + (f"  (as written: {a.ts_raw})" if a.ts_raw and a.ts_raw != when else "")
        )
        if a.process or a.pid is not None:
            lines.append(f"process: {a.process or '-'}  pid {_dash(a.pid)}  tid {_dash(a.tid)}")
        if a.signature:
            lines.append(f"signature: {a.signature}")
        if same:
            copies = ", ".join(f"{o.cite} ({o.lines} lines, {_origin(o)})" for o in same)
            lines.append(f"same crash, other copies: {copies}")
        lines.append("")
        summary = a.summary.split("\n")
        lines += [fmt.clip(x, 300)[0] for x in summary[:30]]
        lines.append("")
        lines.append(f"sections (cite lines as {a.cite}/<section>:L<n>):")
        width = max((len(s.name) for s in a.sections), default=0)
        page = fmt.Page(
            self.caps.max_lines, self.caps.max_bytes - fmt.utf8_len("\n".join(lines)), reserve=300
        )
        for s in a.sections:
            if not page.add(f"  {a.cite}/{s.name:<{width}}  L{s.first_line}-L{s.last_line}"):
                break
        lines += page.lines
        if len(page.lines) < len(a.sections):
            lines.append(f"  … {len(a.sections) - len(page.lines)} more sections")
        best = _main_section(a)
        if best:
            lines.append(f"next: read_artifact(id='{a.cite}/{best}')")
        return _join(lines)

    # -- count ------------------------------------------------------------------------------------

    def _query_arg(
        self, text: str | None, mode: str, warnings: list[str]
    ) -> tuple[str | None, re.Pattern[str] | None]:
        """A search/count query, redacted like the logs; the compiled regex in regex mode."""
        if text is None:
            return None, None
        q = self.app.redactor.redact(text.strip())
        if not q:
            raise ToolInputError("query is empty; omit it to count every record that passes the filter")
        if q != text.strip():
            warnings.append("raw values in the query were redacted to tokens like <MAC:…>, as in the logs")
        if mode == "regex":
            try:
                return q, re.compile(q)
            except re.error as e:
                raise ToolInputError(f"invalid regex {q!r}: {e}") from None
        if looks_like_regex(q):
            warnings.append(
                "the query looks like a regular expression, but mode='text' matches words (FTS5); "
                "pass mode='regex' to match it as one"
            )
        return q, None

    def _match_where(
        self,
        ctx: _Ctx,
        q: str | None,
        rx: re.Pattern[str] | None,
        where: list[str],
        params: list[Any],
        warnings: list[str],
    ) -> tuple[list[str], list[Any], int]:
        """Add the query's conditions. Returns where, params and the sample factor: above
        AGG_ROWS text matches only every k-th match (by rowid) is counted (estimates, ≈)."""
        if q is None:
            return where, params, 1
        if rx is not None:
            pw, pp = query.regex_prefilter(q)
            return [*pw, *where], [*pp, *params], 1
        fts = query.fts_query(q)
        try:
            n_fts = query.fts_count(ctx.conn, fts)
        except sqlite3.OperationalError as e:
            raise ToolInputError(_fts_error(q, e)) from None
        if n_fts > AGG_ROWS:
            k = -(-n_fts // AGG_ROWS)
            warnings.append(
                f"{n_fts:,} records contain the words: counts (≈) are estimated from every {k}th of them; "
                "narrow with level, tags, from/to or a longer query for exact counts"
            )
            return [query.FTS_SAMPLE, *where], [fts, k, *params], k
        return [query.FTS_MATCH, *where], [fts, *params], 1

    def _labels(self, ctx: _Ctx, by: str, keys: list[Any]) -> dict[Any, str]:
        if by == "tag":
            names = query.tag_names(ctx.conn, keys)
            return {k: names.get(k, "-") if k is not None else "-" for k in keys}
        if by == "pid":
            procs = query.process_names(ctx.conn, [k for k in keys if k is not None])
            return {k: f"{k} {procs[k]}" if k in procs else ("-" if k is None else str(k)) for k in keys}
        if by == "source":
            return {k: f"{k} {_tail(ctx.by_id[k].name, 70)}" if k in ctx.by_id else str(k) for k in keys}
        if by == "template":
            texts = query.template_texts(ctx.conn, [k for k in keys if k is not None])
            return {k: fmt.clip(texts.get(k, "-"), 120)[0] for k in keys}
        return {k: k if k is not None else "-" for k in keys}

    def count(
        self,
        case: str,
        by: str | None = "tag",
        text: str | None = None,
        mode: str = "text",
        filter: Filter | None = None,
        bucket: str | None = None,
        limit: int = 20,
    ) -> Output:
        warnings: list[str] = []
        q, rx = self._query_arg(text, mode, warnings)
        if by is not None and by not in query.COUNT_KEYS:
            raise ToolInputError(f"by must be one of {', '.join(query.COUNT_KEYS)}")
        limit_eff = min(limit, self.caps.max_lines)
        if limit > limit_eff:
            warnings.append(f"limit {limit} reduced to {limit_eff} (config caps.max_lines)")
        step: int | None = None
        if bucket is not None:
            if not re.fullmatch(_DUR, bucket.strip()):
                raise ToolInputError(f"invalid bucket {bucket!r}; use a duration such as '10s', '1m', '1h'")
            step = duration_ns(bucket)
            if step < NS // 1000:
                raise ToolInputError("bucket must be at least 1ms")
        with self._case(case) as ctx:
            rf = self.resolve_filter(ctx, filter, warnings)
            where, params = rf.where()
            where, params, sample = self._match_where(ctx, q, rx, where, params, warnings)
            mono = False
            lo = hi = 0
            if step:
                selected = [ctx.by_id[i] for i in (rf.sources if rf.sources is not None else list(ctx.by_id))]
                span = _span(selected)
                if rf.mono_from is not None or rf.mono_to is not None or span.wall is None:
                    mono = True
                    rng = span.mono
                    b_lo = rf.mono_from if rf.mono_from is not None else (rng[0] if rng else None)
                    b_hi = rf.mono_to if rf.mono_to is not None else (rng[1] if rng else None)
                else:
                    b_lo = rf.ts_from if rf.ts_from is not None else span.wall[0]
                    b_hi = rf.ts_to if rf.ts_to is not None else span.wall[1]
                    mono_only = [s.id for s in selected if s.ts_min is None and s.records]
                    if mono_only and rf.ts_from is None and rf.ts_to is None:
                        warnings.append(
                            f"records of {_sources_word(mono_only)} only a monotonic clock and are not in "
                            "the buckets; count them with filter sources and from='0.0'"
                        )
                if b_lo is None or b_hi is None:
                    raise ToolInputError("the selected sources have no timestamps to put into buckets")
                lo, hi = b_lo, b_hi
                n_b = hi // step - lo // step + 1
                max_b = min(MAX_BUCKETS, self.caps.max_lines)
                if n_b > max_b:
                    raise ToolInputError(
                        f"bucket {bucket!r} gives {n_b:,} buckets over {_dur_text(hi - lo)}; "
                        f"at most {max_b}: "
                        f"use bucket='{_nice_step_text(-(-(hi - lo) // (max_b - 1)))}' or narrow from/to"
                    )
            exact = sample == 1
            cells: dict[tuple[int | None, Any], list[int]]
            if rx is not None:
                cells, complete = query.regex_tally(
                    ctx.conn, where, params, rx, self.caps.regex_scan_rows, by, step, mono
                )
                if not complete:
                    exact = False
                    warnings.append(
                        f"regex scan stopped after {self.caps.regex_scan_rows:,} candidate records: "
                        "counts are "
                        "lower bounds; narrow with filter sources, from/to, level or tags"
                    )
            elif step:
                cells = {
                    (b, k): [n, i]
                    for b, k, n, i in query.bucket_counts(ctx.conn, step, where, params, by, mono)
                }
            elif by:
                cells = {(None, k): [n, i] for k, n, i in query.group_counts(ctx.conn, by, where, params)}
            else:
                n = query.count_candidates(ctx.conn, where, params)
                cells = {(None, None): [n, 0]} if n else {}
            if sample > 1:
                for cell in cells.values():
                    cell[0] *= sample
            per_key: Counter[Any] = Counter()
            first_of: dict[Any, int] = {}
            for (_, k), (n, i) in cells.items():
                per_key[k] += n
                first_of[k] = min(first_of.get(k, i), i)
            total = sum(per_key.values())
            approx = "≈" if sample > 1 else ("at least " if not exact else "")
            ranked = sorted(per_key.items(), key=lambda kv: (-kv[1], first_of[kv[0]]))
            labels = self._labels(ctx, by, [k for k, _ in ranked]) if by else {}

            def cite_of(i: int) -> str | None:
                return citation(i >> SEQ_BITS, i & SEQ_MASK) if i else None

            groups = [
                CountGroup(
                    key=k if isinstance(k, int | str) else None,
                    label=str(labels.get(k, "-")),
                    count=n,
                    example=cite_of(first_of[k]),
                )
                for k, n in ranked[:limit_eff]
            ]
            what = f" matching {q!r}" if q else ""
            head = f"{approx}{fmt.plural(total, 'record', 'records')}{what}"
            if by:
                head += f" by {by}, {fmt.plural(len(per_key), 'distinct value', 'distinct values')}"
            page = fmt.Page(
                self.caps.max_lines, self.caps.max_bytes, reserve=_reserve(self.caps.max_bytes, 700)
            )
            data = CountResult(
                case=case, by=by, query=q, total=total, exact=exact, distinct=len(per_key), warnings=warnings
            )
            footer: list[str] = []
            if step is None:
                lines = [head + (":" if total and by else ".")]
                shown = 0
                for g in groups if by else []:
                    if not page.add(
                        f"  {g.count:>11,}  {g.label}" + (f"  e.g. {g.example}" if g.example else "")
                    ):
                        break
                    shown += 1
                data.groups = groups[:shown]
                if by and len(per_key) > shown:
                    footer.append(
                        f"… {len(per_key) - shown:,} more values"
                        + (f" ({fmt.cap_note(page)})" if page.full else f"; raise limit (now {limit_eff})")
                        + " or narrow the filter"
                    )
            else:
                first_b, last_b = (lo // step) * step, (hi // step) * step
                by_bucket: dict[int, Counter[Any]] = {}
                for (b, k), (n, _) in cells.items():
                    if b is not None:
                        by_bucket.setdefault(b, Counter())[k] += n
                peak = max((sum(c.values()) for c in by_bucket.values()), default=0)
                clock = "seconds since boot" if mono else "wall clock"
                lines = [f"{head}; bucket {bucket} ({clock}), {(last_b - first_b) // step + 1} buckets:"]
                buckets: list[CountBucket] = []
                width = len(f"{peak:,}")
                for b in range(first_b, last_b + 1, step):
                    c = by_bucket.get(b, Counter())
                    n = sum(c.values())
                    short = fmt.mono_ts(b) if mono else fmt.short_ts(b)
                    top = (
                        [
                            CountGroup(
                                key=k if isinstance(k, int | str) else None,
                                label=str(labels.get(k, "-")),
                                count=m,
                            )
                            for k, m in sorted(c.items(), key=lambda kv: (-kv[1], str(kv[0])))[:3]
                        ]
                        if by
                        else []
                    )
                    bar = _bar(n, peak, 20)
                    tops = ", ".join(f"{t.label} {t.count:,}" for t in top)
                    if not page.add(
                        f"  {short}  {n:>{width},}  {bar:<20}  {fmt.clip(tops, 120)[0]}".rstrip()
                    ):
                        break
                    buckets.append(
                        CountBucket(ts=(fmt.mono_ts(b) if mono else fmt.full_ts(b)) or "", count=n, top=top)
                    )
                data.bucket = bucket
                data.buckets = buckets
                if by:
                    data.groups = groups
                    footer.append(
                        f"overall by {by}: "
                        + fmt.clip(", ".join(f"{g.label} {g.count:,}" for g in groups[:8]), 400)[0]
                    )
                if page.full:
                    footer.append(f"… later buckets not shown ({fmt.cap_note(page)}); narrow from/to")
            if total == 0:
                footer.append(
                    "No record matches; search(...) explains why for a query, list_sources for the case."
                )
            return Output(_join([*lines, *page.lines, *footer, *_warn_lines(warnings)]), data)

    # -- detect -------------------------------------------------------------------------------------

    def _run_rules(self, ctx: _Ctx, rf: RecordFilter, rules: list[Rule]) -> list[RuleResult]:
        where, params = rf.where()
        follow_where = RecordFilter(sources=rf.sources).where()
        selected = [ctx.by_id[i] for i in (rf.sources if rf.sources is not None else list(ctx.by_id))]
        ends = [s.ts_max for s in selected if s.ts_max is not None]
        case_end = max(ends) if ends else None
        redact = self.app.redactor.redact
        return [
            run_rule(ctx.conn, r, where, params, redact, self.caps.regex_scan_rows, follow_where, case_end)
            for r in rules
        ]

    def detect(
        self, case: str, rules: list[str] | None = None, filter: Filter | None = None, examples: int = 3
    ) -> Output:
        rs = self.app.rules
        if rules:
            unknown = [r for r in rules if r not in rs.rules]
            if unknown:
                raise ToolInputError(
                    f"unknown rules: {', '.join(unknown)}; rules: {', '.join(r.name for r in rs.ordered())}"
                )
            wanted = set(rules)
            chosen = [r for r in rs.ordered() if r.name in wanted]
        else:
            chosen = rs.ordered()
        warnings: list[str] = []
        with self._case(case) as ctx:
            rf = self.resolve_filter(ctx, filter, warnings)
            results = self._run_rules(ctx, rf, chosen)
            hit = [r for r in results if r.count]
            hit.sort(
                key=lambda r: (
                    SEVERITIES.index(r.rule.severity),
                    r.first_ts if r.first_ts is not None else 1 << 62,
                )
            )
            no_hits = [r.rule.name for r in results if not r.count]
            page = fmt.Page(
                self.caps.max_lines, self.caps.max_bytes, reserve=_reserve(self.caps.max_bytes, 800)
            )
            outs: list[RuleHits] = []
            left: list[RuleResult] = []
            for res in hit:
                if page.full:
                    left.append(res)
                    continue
                ro = self._rule_out(ctx, res)
                approx = "" if res.exact else "≥"
                span = ""
                if res.first_ts is not None:
                    span = f"  {fmt.short_ts(res.first_ts)}" + (
                        f" .. {fmt.short_ts(res.last_ts)}" if res.last_ts not in (None, res.first_ts) else ""
                    )
                if not page.add(
                    f"[{res.rule.severity}] {res.rule.name} ×{approx}{res.count:,}  "
                    f"{res.rule.description}{span}"
                ):
                    left.append(res)
                    continue
                for g in res.groups[:examples]:
                    o = self._rec_out(g.first, ctx)
                    if g.count > 1:
                        o.count = g.count
                        o.last = g.last.cite if g.last else None
                    text = "  " + self.line(o, fmt.short_ts(g.first.ts, g.first.ts_src))
                    if g.count > 1 and g.last is None:
                        text += f"  ×{g.count:,}"
                    elif g.count > 1 and g.last is not None:
                        text = "  " + self.line(
                            o,
                            fmt.short_ts(g.first.ts, g.first.ts_src),
                            fmt.short_ts(g.last.ts, g.last.ts_src),
                        )
                    extra = []
                    if g.follow is not None:
                        extra.append(
                            f"     followed by {g.follow.cite}{_delta(g.first, g.follow)}: "
                            f"{fmt.clip(g.follow.msg, 100)[0]}"
                        )
                    if g.open:
                        extra.append("     (the log ends before the window closes)")
                    if not page.add(text):
                        break
                    for e in extra:
                        page.add(e, count_line=False)
                    ro.hits.append(
                        HitOut(
                            record=o,
                            count=g.count,
                            last=g.last.cite if g.last else None,
                            followed_by=g.follow.cite if g.follow else None,
                            open=g.open or None,
                        )
                    )
                ro.more_groups = len(res.groups) - len(ro.hits)
                if ro.more_groups > 0:
                    how = f"detect(rules=['{res.rule.name}'], examples={min(10, len(res.groups))})"
                    if ro.search:
                        how += f" or {ro.search}"
                    page.add(
                        f"  … {ro.more_groups:,} more {'pids' if res.rule.group_by else 'messages'}: {how}",
                        count_line=False,
                    )
                if res.note:
                    page.add(f"  note: {res.note}", count_line=False)
                if res.rule.note:
                    page.add(f"  next: {res.rule.note}", count_line=False)
                outs.append(ro)
            header = (
                f"{len(hit)} of {fmt.plural(len(results), 'rule', 'rules')} with hits"
                + (" (filtered)" if rf.parts() and not (rf.parts() == ["sources"] and filter is None) else "")
                + (", by severity and time:" if hit else ".")
            )
            footer = []
            if left:
                footer.append(
                    "not shown (output cap): "
                    + ", ".join(f"{r.rule.name} ×{r.count:,}" for r in left)
                    + "; detect(rules=[...]) for them"
                )
                outs += [self._rule_out(ctx, r) for r in left]
            if no_hits:
                footer.append("no hits: " + ", ".join(no_hits))
            if not hit:
                footer.append(
                    "No known failure pattern matched. Try list_artifacts(case) and "
                    "search(query, filter={'level': 'E'})."
                )
            data = DetectResult(
                case=case, rules_run=len(results), results=outs, no_hits=no_hits, warnings=warnings
            )
            return Output(_join([header, *page.lines, *footer, *_warn_lines(warnings)]), data)

    def _rule_out(self, ctx: _Ctx, res: RuleResult) -> RuleHits:
        r = res.rule
        search = None
        if len(r.conds) == 1 and r.conds[0].match and r.follow is None:
            c = r.conds[0]
            flt: dict[str, Any] = {}
            if c.tags:
                flt["tags"] = list(c.tags)
            if c.level:
                flt["level"] = c.level
            search = f"search(query={c.match!r}, mode='regex'" + (f", filter={flt!r}" if flt else "") + ")"
        return RuleHits(
            rule=r.name,
            severity=r.severity,
            description=r.description,
            count=res.count,
            exact=res.exact,
            first_ts=fmt.full_ts(res.first_ts),
            last_ts=fmt.full_ts(res.last_ts),
            note=r.note,
            search=search,
        )

    # -- overview -----------------------------------------------------------------------------------

    def overview(self, case: str, filter: Filter | None = None) -> Output:
        warnings: list[str] = []
        with self._case(case) as ctx:
            conn = ctx.conn
            rf = self.resolve_filter(ctx, filter, warnings)
            where, params = rf.where()
            sel_ids = rf.sources if rf.sources is not None else list(ctx.by_id)
            selected = [ctx.by_id[i] for i in sel_ids]
            only_sources = set(rf.parts()) <= {"sources"}
            data = OverviewResult(case=case, sources=len(selected), records=0, warnings=warnings)
            if not selected or not any(s.records for s in selected):
                text = f"Case '{case}' has no records" + (
                    " in the selected sources" if filter and filter.sources else ""
                )
                return Output(text + "; add a log with add_source(case, path).", data)
            n_records = (
                sum(s.records for s in selected)
                if only_sources
                else query.count_candidates(conn, where, params)
            )
            data.records = n_records
            lo, hi = query.ts_bounds(conn, where, params)
            mlo, mhi = query.ts_bounds(conn, where, params, mono=True)
            data.ts_from, data.ts_to = fmt.full_ts(lo), fmt.full_ts(hi)
            data.mono_from = fmt.mono_ts(mlo) if mlo is not None else None
            data.mono_to = fmt.mono_ts(mhi) if mhi is not None else None
            lines: list[str] = []
            rng = (
                f"{fmt.short_ts(lo)} .. {fmt.short_ts(hi)} ({_dur_text(hi - lo)})"
                if lo is not None and hi is not None
                else "no wall-clock time"
            )
            if mlo is not None and mhi is not None:
                rng += f"; since boot {fmt.mono_ts(mlo)} .. {fmt.mono_ts(mhi)}"
            lines.append(
                f"case '{case}': {fmt.plural(len(selected), 'source', 'sources')}, "
                f"{fmt.plural(n_records, 'record', 'records')}"
                f"{' (filtered)' if not only_sources else ''}, {rng}"
            )
            # levels and histogram in one pass over the wall-clock records
            levels: Counter[str] = Counter()
            hist: list[TimeBucket] = []
            step = None
            if lo is not None and hi is not None:
                step = _nice_step(-(-(hi - lo + 1) // MAX_BUCKETS))
                per: dict[int, list[int]] = {}
                for b, lvl, n in query.level_bucket_counts(conn, step, where, params):
                    levels[lvl or "-"] += n
                    cell = per.setdefault(b, [0, 0])
                    cell[0] += n
                    if lvl in ("E", "F"):
                        cell[1] += n
                first_b, last_b = (lo // step) * step, (hi // step) * step
                for b in range(first_b, last_b + 1, step):
                    n, e = per.get(b, [0, 0])
                    hist.append(TimeBucket(ts=fmt.full_ts(b) or "", count=n, errors=e))
            if mlo is not None:
                for lvl, n, _ in query.group_counts(conn, "level", [*where, "r.ts IS NULL"], params):
                    levels[lvl or "-"] += n
            data.levels = {k: levels[k] for k in "FEWIDV-" if levels.get(k)}
            lines.append("levels: " + ", ".join(f"{k} {v:,}" for k, v in data.levels.items()))
            # top tags, processes, messages
            tags = query.group_counts(conn, "tag", where, params)[:TOP_N]
            tnames = query.tag_names(conn, [k for k, _, _ in tags])
            data.top_tags = [
                CountGroup(
                    key=k, label=tnames.get(k, "-") if k is not None else "-", count=n, example=_cite_id(i)
                )
                for k, n, i in tags
            ]
            lines.append("top tags: " + ", ".join(f"{g.label} {g.count:,}" for g in data.top_tags))
            pids = [(k, n, i) for k, n, i in query.group_counts(conn, "pid", where, params) if k is not None][
                :TOP_N
            ]
            pnames = query.process_names(conn, [k for k, _, _ in pids])
            data.top_processes = [
                CountGroup(
                    key=k, label=f"{k} {pnames[k]}" if k in pnames else str(k), count=n, example=_cite_id(i)
                )
                for k, n, i in pids
            ]
            if data.top_processes:
                lines.append("top pids: " + ", ".join(f"{g.label} {g.count:,}" for g in data.top_processes))
            tmpls = query.group_counts(conn, "template", where, params)[:TOP_N]
            texts = query.template_texts(conn, [k for k, _, _ in tmpls if k is not None])
            ex = query.records_by_id(conn, [i for _, _, i in tmpls])
            lines.append("top messages (templates, ×count, an example):")
            for k, n, i in tmpls:
                r = ex.get(i)
                label = fmt.clip(texts.get(k, r.msg if r else "-").split("\n", 1)[0], 110)[0]
                data.top_templates.append(CountGroup(key=k, label=label, count=n, example=_cite_id(i)))
                who = f"{r.level or '-'} {r.tag or ''}".strip() if r else ""
                lines.append(f"  ×{n:,}  {who}  {label}  e.g. {_cite_id(i)}")
            # segments
            all_segs = query.segments(conn, sel_ids)
            first_seg: dict[int, int] = {}
            for row in all_segs:
                first_seg.setdefault(row[0], row[1])
            segs = [row for row in all_segs if row[1] != first_seg[row[0]]]  # restarts after the first
            for sid, seg, reason, first_seq, count, ts_first, ts_src_first in segs:
                data.segments.append(
                    SegmentOut(
                        source=str(sid),
                        seg=seg,
                        reason=reason,
                        first=citation(sid, first_seq),
                        ts=fmt.full_ts(ts_first, ts_src_first),
                        records=count,
                    )
                )
            if data.segments:
                shown = data.segments[:8]
                lines.append(
                    "segments (clock restarts): "
                    + "; ".join(f"source {s.source} {s.reason} at {s.first} ({s.ts or '-'})" for s in shown)
                    + (f"; … {len(data.segments) - 8} more" if len(data.segments) > 8 else "")
                )
            # gaps
            if lo is not None and hi is not None:
                gaps, complete = query.gaps(conn, where, params, lo, hi, GAP_NS)
                data.gaps_complete = complete
                for g in gaps:
                    data.gaps.append(
                        GapOut(
                            before=_cite_id(g.before_id) or "",
                            after=_cite_id(g.after_id) or "",
                            from_ts=fmt.full_ts(g.before_ts) or "",
                            to_ts=fmt.full_ts(g.after_ts) or "",
                            seconds=round((g.after_ts - g.before_ts) / NS, 3),
                        )
                    )
                biggest = sorted(data.gaps, key=lambda g: -g.seconds)[:5]
                lines.append(
                    f"gaps > {GAP_NS // NS} s without a record: "
                    + (
                        "none"
                        if not gaps
                        else f"{len(gaps)}"
                        + ("+" if not complete else "")
                        + ", longest: "
                        + "; ".join(
                            f"{_dur_text(int(g.seconds * NS))} after {g.before} "
                            f"({fmt.short_ts(_ns(g.from_ts))}) until {g.after}"
                            for g in biggest
                        )
                    )
                )
                # error bursts
                data.bursts = self._bursts(ctx, where, params, lo, hi)
                if data.bursts:
                    lines.append(f"error bursts (E/F per 10 s above 5× the median, at least {BURST_MIN}):")
                    for bo in sorted(data.bursts, key=lambda x: -x.errors)[:5]:
                        lines.append(
                            f"  {fmt.short_ts(_ns(bo.from_ts))} .. {fmt.short_ts(_ns(bo.to_ts))}  "
                            f"E/F {bo.errors:,}"
                            f"  top tag {bo.top_tag or '-'}  first {bo.first}"
                        )
                else:
                    lines.append("error bursts (E/F per 10 s above 5× the median): none")
            if hist and step:
                data.histogram = hist
                data.histogram_step = _nice_step_text(step)
                peak = max(h.count for h in hist)
                epeak = max(h.errors for h in hist)
                lines.append(
                    f"histogram from {fmt.short_ts(_ns(hist[0].ts))}, {len(hist)} buckets of "
                    f"{data.histogram_step} (peak {peak:,} records, {epeak:,} E/F; "
                    f"count(case, bucket='{data.histogram_step}') for numbers):"
                )
                lines.append("  all |" + "".join(_spark(h.count, peak) for h in hist) + "|")
                lines.append("  E/F |" + "".join(_spark(h.errors, epeak) for h in hist) + "|")
            # detector counts and artifacts
            results = self._run_rules(ctx, rf, self.app.rules.ordered())
            data.detect = {r.rule.name: r.count for r in results if r.count}
            if any(not r.exact for r in results if r.count):
                data.exact = False
            lines.append(
                "detect: "
                + (
                    ", ".join(
                        f"{r.rule.name} {'≥' if not r.exact else ''}{r.count:,}" for r in results if r.count
                    )
                    or "no rule hits"
                )
                + " (detect(case) shows the lines)"
            )
            arts = query.artifacts(conn, None, sel_ids)
            if rf.ts_from is not None or rf.ts_to is not None:
                a0 = rf.ts_from if rf.ts_from is not None else -(1 << 62)
                a1 = rf.ts_to if rf.ts_to is not None else 1 << 62
                arts = [a for a in arts if a.ts is not None and a0 <= a.ts <= a1]
            types = Counter(a.type for a in arts)
            data.artifacts = dict(sorted(types.items()))
            lines.append(
                "artifacts: "
                + (
                    ", ".join(f"{t} {n}" for t, n in sorted(types.items())) + " (list_artifacts(case))"
                    if types
                    else "none"
                )
            )
            lines.append(
                "next: detect(case) for known failure patterns, list_artifacts(case) for crashes, then "
                "search(query, filter={'level': 'W', 'from': …, 'to': …}) around them"
            )
            page = fmt.Page(
                10 * self.caps.max_lines, self.caps.max_bytes, reserve=_reserve(self.caps.max_bytes, 300)
            )
            for line in lines:
                if not page.add(fmt.clip(line, 1200)[0]):
                    break
            out = page.lines
            if page.full:
                out.append(f"… ({fmt.cap_note(page)}); narrow with filter sources or from/to")
            return Output(_join([*out, *_warn_lines(warnings)]), data)

    def _bursts(self, ctx: _Ctx, where: list[str], params: list[Any], lo: int, hi: int) -> list[BurstOut]:
        rf_e = RecordFilter(min_level="E")
        ew, ep = rf_e.where()
        w, p = [*where, *ew], [*params, *ep]
        per = {b: n for b, _, n, _ in query.bucket_counts(ctx.conn, BURST_NS, w, p)}
        if not per:
            return []
        n_buckets = hi // BURST_NS - lo // BURST_NS + 1
        counts = sorted([*per.values(), *([0] * max(0, n_buckets - len(per)))])
        median = counts[len(counts) // 2]
        threshold = max(5 * median, BURST_MIN)
        hot = sorted(b for b, n in per.items() if n > threshold)
        runs: list[list[int]] = []
        for b in hot:
            if runs and b == runs[-1][-1] + BURST_NS:
                runs[-1].append(b)
            else:
                runs.append([b])
        out: list[BurstOut] = []
        for run in sorted(runs, key=lambda r: -sum(per[b] for b in r))[:10]:
            b0, b1 = run[0], run[-1] + BURST_NS - 1
            tw = [*w, "r.ts BETWEEN ? AND ?"]
            tp = [*p, b0, b1]
            first = query.first_by_time(ctx.conn, tw, tp)
            tags = query.group_counts(ctx.conn, "tag", tw, tp)[:1]
            top = (
                query.tag_names(ctx.conn, [tags[0][0]]).get(tags[0][0])
                if tags and tags[0][0] is not None
                else None
            )
            out.append(
                BurstOut(
                    from_ts=fmt.full_ts(b0) or "",
                    to_ts=fmt.full_ts(b1) or "",
                    errors=sum(per[b] for b in run),
                    first=_cite_id(first) or "",
                    top_tag=top,
                )
            )
        out.sort(key=lambda b: b.from_ts)
        return out

    # -- check_citations ------------------------------------------------------------------------------

    def check_citations(self, case: str, text: str) -> Output:
        paras = cit.paragraphs(text)
        found = [f for p in paras for f in p.cites]
        errors: list[CitationIssue] = []
        warns: list[CitationIssue] = []
        with self._case(case) as ctx:
            status: dict[str, str | None] = {}
            targets: dict[str, Any] = {}  # resolved citation -> record or (artifact, citation)
            log_wanted: dict[str, int] = {}
            for f in found:
                if not f.valid or f.text in status:
                    continue
                if f.kind == "log":
                    sid, seq = parse_log_cite(f.text)
                    if sid not in ctx.by_id:
                        status[f.text] = (
                            f"no source {sid} in case '{case}' "
                            f"(sources {_id_ranges(list(ctx.by_id)) or 'none'})"
                        )
                    else:
                        status[f.text] = None
                        log_wanted[f.text] = (sid << SEQ_BITS) | seq
                else:
                    status[f.text] = self._check_art(ctx, f.text, targets)
            recs = query.records_by_id(ctx.conn, list(log_wanted.values()))
            for c, rid in log_wanted.items():
                r = recs.get(rid)
                if r is None:
                    s = ctx.by_id[rid >> SEQ_BITS]
                    status[c] = f"source {s.id} has {fmt.plural(s.records, 'record', 'records')}"
                else:
                    targets[c] = r
            for f in found:
                if not f.valid:
                    errors.append(CitationIssue(line=f.line, cite=f.text, message=f"malformed: {f.problem}"))
                elif status.get(f.text):
                    errors.append(
                        CitationIssue(line=f.line, cite=f.text, message=f"does not resolve: {status[f.text]}")
                    )
            uncited = 0
            redact = self.app.redactor.redact
            for p in paras:
                if not p.cites:
                    if p.words >= cit.MIN_WORDS and not p.hedged() and not p.text.rstrip().endswith(":"):
                        uncited += 1
                        warns.append(
                            CitationIssue(
                                line=p.first_line,
                                message=f'no citation in: "{fmt.clip(p.text, 90)[0]}" '
                                "(cite the line it comes from, "
                                "or say it is a guess)",
                            )
                        )
                    continue
                good = [f.text for f in p.cites if f.valid and not status.get(f.text)]
                if not good:
                    continue
                hay = [cit.normalize(self._cited_text(ctx, targets[c])) for c in dict.fromkeys(good)]
                for qt in p.quotes():
                    needle = cit.normalize(redact(qt))
                    if needle and not any(needle in h for h in hay):
                        warns.append(
                            CitationIssue(
                                line=p.first_line,
                                cite=", ".join(dict.fromkeys(good)),
                                message=f'quote "{fmt.clip(qt, 70)[0]}" is not in the cited lines',
                            )
                        )
            distinct = len({f.text for f in found})
            resolved = sum(1 for c, s in status.items() if s is None)
            ok = not errors
            data = CheckCitationsResult(
                case=case,
                ok=ok,
                citations=len(found),
                distinct=distinct,
                resolved=resolved,
                paragraphs=len(paras),
                uncited=uncited,
                errors=errors,
                warnings=warns,
            )
            head = (
                f"{'PASS' if ok else 'FAIL'}: {fmt.plural(len(errors), 'error', 'errors')}, "
                f"{fmt.plural(len(warns), 'warning', 'warnings')}; "
                f"{fmt.plural(len(found), 'citation', 'citations')} "
                f"({distinct} distinct, {resolved} resolve); "
                f"{fmt.plural(len(paras), 'paragraph', 'paragraphs')}, "
                f"{uncited} without a citation"
            )
            if not found:
                head += (
                    ". The text cites nothing: every finding needs log:<source>#<seq> or "
                    "art:<id>[/section][:L<n>]"
                )
            page = fmt.Page(
                self.caps.max_lines, self.caps.max_bytes, reserve=_reserve(self.caps.max_bytes, 400)
            )
            for e in errors:
                if not page.add(f"error L{e.line}: {e.cite} {e.message}"):
                    break
            for w in warns:
                if not page.add(f"warning L{w.line}: " + (f"{w.cite}: " if w.cite else "") + w.message):
                    break
            tail = []
            if page.full:
                tail.append(f"… more issues not shown ({fmt.cap_note(page)}); fix these and check again")
            if not ok:
                tail.append(
                    "Fix the citations (copy them exactly as the tools print them) and run "
                    "check_citations again."
                )
            elif warns:
                tail.append(
                    "All citations resolve. Add citations to the paragraphs named above or mark "
                    "them as guesses."
                )
            else:
                tail.append("All citations resolve.")
            return Output(_join([head, *page.lines, *tail]), data)

    def _check_art(self, ctx: _Ctx, cite: str, targets: dict[str, Any]) -> str | None:
        c = query.parse_art_citation(cite)
        a = query.artifact(ctx.conn, c.id)
        if a is None or a.source_id not in ctx.by_id:
            return f"no artifact art:{c.id} in case '{ctx.case}' (see list_artifacts)"
        try:
            if c.section is not None or c.first is not None:
                query.artifact_lines(ctx.conn, a, c)
        except ValueError as e:
            return str(e)
        targets[cite] = (a, c)
        return None

    def _cited_text(self, ctx: _Ctx, target: Any) -> str:
        if isinstance(target, Rec):
            return f"{target.tag or ''} {target.msg}"
        a, c = target
        lines = query.artifact_lines(ctx.conn, a, c)
        return "\n".join([a.headline, a.summary, *(t for _, t in lines)])


# -- artifact grouping --------------------------------------------------------------------------------

SAME_CRASH_NS = 10 * NS  # copies of one crash (crash buffer, dropbox, ANR file and section) are this close


@dataclass
class _GroupedArtifact:
    row: ArtifactRow
    same_as: str | None = None


@dataclass
class _ArtifactGroup:
    type: str
    signature: str | None
    items: list[_GroupedArtifact]


def group_key(a: ArtifactRow) -> tuple[str, str]:
    """Artifacts of the same crash share type, kind, process and top frame.

    The full signature has up to three frames; a crash-buffer copy of a Java crash often has
    fewer frames than its dropbox copy, so grouping uses the signature up to the first frame."""
    if not a.signature:
        return (a.type, f"#{a.id}")
    return (a.type, a.signature.split(" < ", 1)[0])


def group_artifacts(rows: list[ArtifactRow]) -> list[_ArtifactGroup]:
    """Groups in order of their first artifact's time; within a group in time order. An artifact
    with the same pid within ``SAME_CRASH_NS`` of an earlier one in its group is marked as a copy."""
    groups: dict[tuple[str, str], _ArtifactGroup] = {}
    for a in rows:  # rows come in time order
        key = group_key(a)
        g = groups.get(key)
        if g is None:
            g = groups[key] = _ArtifactGroup(a.type, a.signature, [])
        same = next((x for x in g.items if not x.same_as and _same_event(x.row, a)), None)
        g.items.append(_GroupedArtifact(a, same.row.cite if same else None))
        if a.signature and (g.signature is None or len(a.signature) > len(g.signature)):
            g.signature = a.signature  # show the most detailed signature
    return list(groups.values())


def _same_event(a: ArtifactRow, b: ArtifactRow) -> bool:
    return (
        a.pid is not None
        and a.pid == b.pid
        and a.ts is not None
        and b.ts is not None
        and abs(a.ts - b.ts) <= SAME_CRASH_NS
    )


def _same_crash(a: ArtifactRow, others: list[ArtifactRow]) -> list[ArtifactRow]:
    key = group_key(a)
    return [o for o in others if o.id != a.id and group_key(o) == key and _same_event(a, o)]


def _origin(a: ArtifactRow) -> str:
    name = a.name.rsplit("!", 1)[-1]
    return name.rsplit("/", 1)[-1][:60]


def _main_section(a: ArtifactRow) -> str | None:
    names = [s.name for s in a.sections]
    for pref in ("backtrace", "exception"):
        if pref in names:
            return pref
    if a.pid is not None:
        main = next((n for n in names if n.startswith(f"p{a.pid}.t1.")), None)
        if main:
            return main
    for pref in ("body",):
        if pref in names:
            return pref
    return None


# -- helpers ---------------------------------------------------------------------------------------------

_PART_NAMES = {
    "sources": "sources filter",
    "time": "from/to filter",
    "level": "level filter",
    "tags": "tags filter",
    "not_tags": "not_tags filter",
    "pid": "pid filter",
    "attrs": "attrs filter",
}


def _some(items: list[str], n: int = 10) -> str:
    return ", ".join(items[:n]) + (f", … {len(items) - n} more" if len(items) > n else "")


def _reserve(max_bytes: int, want: int) -> int:
    """Bytes kept free for headers and footers: ``want``, but at most a quarter of the cap."""
    return min(want, max_bytes // 4)


def _dash(v: int | None) -> str:
    return "-" if v is None else str(v)


def _part_value(rf: RecordFilter, part: str) -> str:
    if part == "level":
        return f" (level {rf.min_level})"
    if part == "pid":
        return f" (pid {rf.pid})"
    return ""


def _sources_word(ids: list[int]) -> str:
    return f"source {ids[0]} have" if len(ids) == 1 else f"sources {_id_ranges(ids)} have"


def _span(rows: list[query.SourceRow]) -> Span:
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


def _id_ranges(ids: list[int]) -> str:
    ids = sorted(set(ids))
    out: list[str] = []
    i = 0
    while i < len(ids):
        j = i
        while j + 1 < len(ids) and ids[j + 1] == ids[j] + 1:
            j += 1
        out.append(str(ids[i]) if i == j else f"{ids[i]}-{ids[j]}")
        i = j + 1
    return ", ".join(out)


def _level_order(item: tuple[str, int]) -> int:
    return "FEWIDV-".index(item[0]) if item[0] in "FEWIDV-" else 9


def _fts_error(q: str, e: sqlite3.OperationalError) -> str:
    return (
        f'invalid text query {q!r} ({e}). Text queries are words (all must occur), "quoted phrases", '
        "'prefix*', and the upper-case operators AND, OR, NOT, NEAR(); use mode='regex' for patterns"
    )


def _scan_note(scan: query.ScanResult, cap: int) -> str:
    where = ""
    if scan.last_scanned is not None:
        where = f" (stopped at {citation(scan.last_scanned >> SEQ_BITS, scan.last_scanned & SEQ_MASK)})"
    return (
        f"regex scan stopped after {cap:,} candidate records{where}: counts are lower bounds; narrow with "
        "filter sources, from/to, level or tags, or add literal text to the regex"
    )


def _literal_lines(hits: list[RecordOut], n: int = 5) -> list[str]:
    seen: set[str] = set()
    picked: list[tuple[int, RecordOut]] = []
    for i, h in enumerate(hits):
        if h.literal and h.literal not in seen:
            seen.add(h.literal)
            picked.append((i, h))
    if not picked:
        return []
    picked.sort(key=lambda x: ("FEWIDV".find(x[1].level) if x[1].level else 6, x[0]))
    out = ["code search (constant text of the templates):"]
    for _, h in picked[:n]:
        out.append(f'  "{fmt.clip(h.literal or "", 100)[0]}"  {h.cite}')
    return out


def _warn_lines(warnings: list[str]) -> list[str]:
    return [f"warning: {w}" for w in warnings]


def _join(lines: list[str]) -> str:
    return "\n".join(lines)


def _cut_bytes(text: str, n: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= n:
        return text
    return data[:n].decode("utf-8", errors="ignore") + fmt.ELLIPSIS


_STEPS = [
    *(n * NS // 1000 for n in (1, 2, 5, 10, 20, 50, 100, 200, 500)),
    *(n * NS for n in (1, 2, 5, 10, 15, 30)),
    *(n * 60 * NS for n in (1, 2, 5, 10, 15, 30)),
    *(n * 3600 * NS for n in (1, 2, 3, 6, 12, 24)),
]


def _nice_step(min_ns: int) -> int:
    """The smallest round bucket size of at least ``min_ns`` (1 ms .. 1 d, then whole days)."""
    for s in _STEPS:
        if s >= min_ns:
            return s
    day = 86400 * NS
    return -(-min_ns // day) * day


def _nice_step_text(min_ns: int) -> str:
    s = _nice_step(min_ns)
    for unit, ns in (("d", 86400 * NS), ("h", 3600 * NS), ("m", 60 * NS), ("s", NS), ("ms", NS // 1000)):
        if s % ns == 0:
            return f"{s // ns}{unit}"
    return f"{s // (NS // 1000)}ms"


def _dur_text(ns: int) -> str:
    secs = ns / NS
    if secs < 1:
        return f"{secs * 1000:.0f} ms"
    if secs < 120:
        return f"{secs:.1f} s".replace(".0 s", " s")
    if secs < 7200:
        return f"{secs / 60:.1f} min".replace(".0 min", " min")
    if secs < 2 * 86400:
        return f"{secs / 3600:.1f} h".replace(".0 h", " h")
    return f"{secs / 86400:.1f} d".replace(".0 d", " d")


def _delta(a: Rec, b: Rec) -> str:
    if a.ts is not None and b.ts is not None:
        return f" (+{_dur_text(b.ts - a.ts)})"
    return ""


_SPARK = "▁▂▃▄▅▆▇█"


def _spark(n: int, peak: int) -> str:
    if n <= 0 or peak <= 0:
        return "·"
    return _SPARK[min(len(_SPARK) - 1, (n * len(_SPARK) - 1) // peak)]


def _bar(n: int, peak: int, width: int) -> str:
    if n <= 0 or peak <= 0:
        return ""
    return "█" * max(1, round(n * width / peak))


def _cite_id(rid: int | None) -> str | None:
    return citation(rid >> SEQ_BITS, rid & SEQ_MASK) if rid else None


def _ns(full: str) -> int:
    """A full timestamp as printed (``YYYY-MM-DD hh:mm:ss.mmm``) back to ns."""
    m = _FULL.fullmatch(full)
    assert m is not None, full
    return wall_ns(
        int(m[1]), int(m[2]), int(m[3]), int(m[4] or 0), int(m[5] or 0), int(m[6] or 0), m[7] or ""
    )


def _tail(name: str, n: int) -> str:
    """The distinguishing end of a source name (after the archive path), cut from the left."""
    name = name.rsplit("!", 1)[-1]
    return name if len(name) <= n else fmt.ELLIPSIS + name[-(n - 1) :]
