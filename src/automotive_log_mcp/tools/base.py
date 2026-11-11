"""The shared core of the tools: a case view per call, record output lines, filters and times."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from ..app import App
from ..core import format as fmt
from ..core.timeparse import NS, ts_raw_of, wall_ns
from ..detect.engine import Rule, RuleResult, run_rule
from ..model import SEQ_BITS
from ..store import db, query
from ..store.query import Rec, RecordFilter
from .clock import ClockTools
from .diff import DiffTool
from .models import Filter, RecordOut, ToolInputError
from .text import id_ranges, sources_word
from .timespec import (
    ART_ID,
    CITE_OFFSET,
    CLOCK_TS,
    FULL_TS,
    MONO_TS,
    REL_TIME,
    SHORT_TS,
    TIME_FORMS,
    Bound,
    Span,
    duration_ns,
    granularity,
    parse_log_cite,
    sources_span,
)


@dataclass
class CaseCtx:
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


class ToolBase:
    """What every tool needs: the app, the caps, one call's case view, record lines, filters and
    times. The clock and diff tools hold a reference to it."""

    def __init__(self, app: App) -> None:
        self.app = app
        self.caps = app.config.caps
        self.clock = ClockTools(self)
        self.differ = DiffTool(self)

    @contextmanager
    def open_case(self, case: str) -> Iterator[CaseCtx]:
        path = self.app.config.case_db(case)
        if not path.exists():
            raise ToolInputError(f"case '{case}' does not exist; add a log with add_source(case, path) first")
        with db.opened(path) as conn:
            all_rows = query.sources(conn, self.app.versions(), self.app.redactor.rules_hash)
            visible = [s for s in all_rows if not self.app.blocked(s.name, s.path)]
            yield CaseCtx(case, conn, visible, len(visible) != len(all_rows))

    def rec_out(self, r: Rec, ctx: CaseCtx, *, full: bool = False, chars: int | None = None) -> RecordOut:
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

    def resolve_filter(self, ctx: CaseCtx, f: Filter | None, warnings: list[str]) -> RecordFilter:
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
                        f"{id_ranges(visible_ids) or 'none'} (see list_sources)"
                    )
                ids.append(int(text))
            rf.sources = sorted(set(ids))
        elif ctx.hidden:
            rf.sources = visible_ids
        selected = [ctx.by_id[i] for i in (rf.sources if rf.sources is not None else visible_ids)]
        span = sources_span(selected)
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
                    f"records of {sources_word(mono_only)} only a monotonic clock (seconds since boot) "
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

    def resolve_time(self, ctx: CaseCtx, spec: str, role: str, span: Span, other: Bound | None) -> Bound:
        """A from/to value as a bound on the wall clock or on the monotonic clock."""
        text = spec.strip()
        m = FULL_TS.fullmatch(text)
        if m:
            y, mo, d = int(m[1]), int(m[2]), int(m[3])
            try:
                ns = wall_ns(y, mo, d, int(m[4] or 0), int(m[5] or 0), int(m[6] or 0), m[7] or "")
            except ValueError:
                raise ToolInputError(f"invalid date or time in {role}={spec!r}") from None
            return Bound("wall", ns, granularity(m[6], m[7], m[4]))
        m = SHORT_TS.fullmatch(text)
        if m:
            mo, d = int(m[1]), int(m[2])
            year = self._year_for(span, mo, d, role, spec)
            try:
                ns = wall_ns(year, mo, d, int(m[3]), int(m[4]), int(m[5] or 0), m[6] or "")
            except ValueError:
                raise ToolInputError(f"invalid date or time in {role}={spec!r}") from None
            return Bound("wall", ns, granularity(m[5], m[6]))
        m = CLOCK_TS.fullmatch(text)
        if m:
            return self._clock_time(span, m, role, spec)
        m = MONO_TS.fullmatch(text)
        if m:
            return Bound("mono", int(m[1]) * NS + int(m[2].ljust(9, "0")), 10 ** (9 - len(m[2])))
        m = REL_TIME.fullmatch(text)
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
        m = CITE_OFFSET.fullmatch(text)
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
        gran = granularity(m[3], m[4])
        days = range(lo // day, hi // day + 1)
        fits = [dd * day + tod for dd in days if lo <= dd * day + tod + gran - 1 and dd * day + tod <= hi]
        if len(fits) == 1:
            return Bound("wall", fits[0], gran)
        if not fits:
            raise ToolInputError(f"{role}={spec!r} is outside the case's time range; give the date")
        raise ToolInputError(
            f"{role}={spec!r} matches several days of the case; give the date (MM-DD hh:mm:ss)"
        )

    def _cite_time(self, ctx: CaseCtx, cite: str, role: str) -> Bound:
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

    def resolve_at(self, ctx: CaseCtx, at: str) -> int:
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
                f"no source {sid} in case '{ctx.case}'; sources: {id_ranges(list(ctx.by_id)) or 'none'}"
            )
        return (sid << SEQ_BITS) | seq

    def run_rules(self, ctx: CaseCtx, rf: RecordFilter, rules: list[Rule]) -> list[RuleResult]:
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
