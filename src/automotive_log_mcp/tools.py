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
from .core import format as fmt
from .core.timeparse import NS, ts_raw_of, wall_ns
from .model import SEQ_BITS, SEQ_MASK, citation
from .store import db, query
from .store.query import ArtifactRow, Rec, RecordFilter

Level = Literal["V", "D", "I", "W", "E", "F"]


class ToolInputError(ValueError):
    """Invalid arguments, or ids that do not exist; the message says how to fix the call."""


# -- argument and result models -------------------------------------------------------------------


class Filter(BaseModel):
    """Record filter shared by search and later tools. All fields are optional and combine with AND."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    sources: list[str] | None = Field(
        default=None, description="Source ids to include, as list_sources prints them."
    )
    from_: str | None = Field(
        default=None,
        alias="from",
        description="Start time (inclusive): a timestamp exactly as printed in outputs "
        "('01-01 00:00:01.000', '2026-01-01 00:00:01.000', or '123.456789' seconds since boot for "
        "kernel records), a relative time ('-5m': before the end of the case; '+30s': after its "
        "start), or a citation 'log:<source>#<seq>' / 'art:<id>', optionally with an offset "
        "('log:3#1-30s').",
    )
    to: str | None = Field(
        default=None,
        description="End time (inclusive, to the precision given: '08:14' includes all of 08:14), "
        "same forms as 'from'; '+30s' here means 30 s after 'from'.",
    )
    level: Level | None = Field(default=None, description="Minimum level: V < D < I < W < E < F.")
    tags: list[str] | None = Field(default=None, description="Tags to include; globs allowed, e.g. 'Car*'.")
    not_tags: list[str] | None = Field(default=None, description="Tags to exclude; globs allowed.")
    pid: int | None = Field(default=None, ge=0, description="Process id.")
    attrs: dict[str, str | int | float | bool] | None = Field(
        default=None,
        description="Format-specific attributes that must match exactly, e.g. {'uid': '1000'} or "
        "{'art': 3} (the record of artifact art:3), as get_records shows them.",
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
        default=True, description="False when a regex scan stopped early: total is a lower bound."
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
    records: list[RecordOut]
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
    signature: str | None
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
    def line(o: RecordOut, short: str, last_short: str | None = None) -> str:
        """``log:2#18233  10-11 08:12:03.412  E 1234/1250 HvacCtrl  message``"""
        who = (
            f"{o.pid}/{o.tid}"
            if o.pid is not None and o.tid is not None
            else (str(o.pid) if o.pid is not None else "")
        )
        head = " ".join(p for p in (o.level or "-", who, o.tag or "") if p)
        text = f"{o.cite}  {short}  {head}  {o.msg}"
        if o.count and o.count > 1:
            text += f"  ×{o.count} until {last_short} ({o.last})"
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
        """``text`` None (CLI only) matches every record that passes the filter."""
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
                lits = query.required_literals(q)
                for lit in lits:
                    where.append("(instr(r.msg, ?) > 0 OR instr(coalesce(t.name, ''), ?) > 0)")
                    params.extend([lit, lit])
                lit_fts = query.literals_fts(lits)
                if lit_fts:
                    where.insert(0, query.FTS_MATCH)
                    params.insert(0, lit_fts)
            exact = True
            scan: query.ScanResult | None = None
            try:
                if rx is None:
                    groups = query.aggregate(ctx.conn, where, params)
                else:
                    window = 0 if (collapse or count_only) else offset + limit_eff
                    scan = query.regex_scan(ctx.conn, where, params, rx, self.caps.regex_scan_rows, window)
                    groups, exact = scan.groups, scan.complete
            except sqlite3.OperationalError as e:
                raise ToolInputError(_fts_error(q, e)) from None
            total = sum(g.count for g in groups)
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
                    f"{'at least ' if not exact else ''}{fmt.plural(total, 'match', 'matches')}, "
                    f"{fmt.plural(len(groups), 'distinct message', 'distinct messages')}"
                )
                lines = [f"{head} ({levels})"]
                if scan and not exact:
                    lines.append(_scan_note(scan, self.caps.regex_scan_rows))
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
                    recs = query.select_ordered(ctx.conn, where, params, offset, limit_eff, n_wall)
                hits = [(r, self._rec_out(r, ctx), fmt.short_ts(r.ts, r.ts_src), None) for r in recs]
                n_items = total
            tmpl = query.template_texts(ctx.conn, [r.template_id for r, *_ in hits if r.template_id])
            shown: list[RecordOut] = []
            for r, o, short, last_short in hits:
                if not page.add(self.line(o, short, last_short)):
                    break
                t = tmpl.get(r.template_id) if r.template_id else None
                o.template = t
                # A synthetic artifact record's text is ours, not the program's: nothing to search for.
                o.literal = None if r.msg.startswith("[art:") else fmt.template_literal(t)
                shown.append(o)
            end = offset + len(shown)
            more = n_items - end
            header = (
                f"{'at least ' if not exact else ''}{fmt.plural(total, 'match', 'matches')}"
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
            else:
                recs = query.source_neighbours(ctx.conn, rid, b, a)
                head = f"source {anchor.source_id} around {anchor.cite}:"
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
            lines = [head, *texts[lo:hi]]
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
            budget = self.caps.max_bytes - 400
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
                lines.append(f"not found: {', '.join(missing)}")
            if not_shown:
                lines.append(
                    f"not shown (output cap of {self.caps.max_bytes} bytes): {', '.join(not_shown)}; "
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
            same = _same_crash(a, query.artifacts(ctx.conn, a.type, [s.id for s in ctx.sources]))
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
