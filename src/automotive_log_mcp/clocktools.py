"""Multi-source tools on the case clock (M5): ``timeline``, ``align`` and ``context(scope='all')``.

The case clock and the per-segment mappings are in :mod:`core.clock`; :mod:`core.align` computes
them. Times printed here are case times; ``~`` marks a time mapped from another clock (not
``exact``). ``timeline`` accepts its own printed times back as ``from``/``to``; the other tools
keep reading each source's times as written.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from .core import align as al
from .core import clock as ck
from .core import format as fmt
from .core.clock import CaseClock, SegClock
from .core.timeparse import NS, fmt_wall
from .model import SEQ_BITS, SEQ_MASK
from .store import query
from .store.query import Rec

if TYPE_CHECKING:
    from .tools import Filter, Tools, _Ctx

SLACK = 50  # extra rows per query for records at the cursor's time


class TimelineRecord(BaseModel):
    cite: str
    ts: str | None = Field(default=None, description="Case-clock time; accepted back as timeline from/to.")
    approx: bool | None = Field(default=None, description="True: mapped from another clock (~ in the text).")
    level: str | None = None
    tag: str | None = None
    pid: int | None = None
    tid: int | None = None
    msg: str
    cut: bool | None = None
    art: str | None = None


class TimelineResult(BaseModel):
    case: str
    clock: str = Field(description="The case clock: which source's clock the times are on.")
    records: list[TimelineRecord]
    next_cursor: str | None = None
    unplaced: list[str] = Field(default_factory=list, description="Sources not on the case clock.")
    warnings: list[str] = Field(default_factory=list)


class SourceClockOut(BaseModel):
    source: str
    name: str
    seg: int | None = None
    conf: str | None = Field(
        default=None, description="exact | manual | paired | marker | estimate | assumed"
    )
    offset: str | None = Field(default=None, description="Offset to the case clock, or boot time.")
    error: str | None = None
    method: str | None = None
    placed: bool = True


class AlignResult(BaseModel):
    case: str
    clock: str
    sources: list[SourceClockOut]
    notes: list[str] = Field(default_factory=list)


@dataclass
class _Pos:
    t: int
    id: int


def _dur(ns: int | None) -> str:
    if ns is None:
        return "?"
    a = abs(ns)
    if a < 1_000_000:
        return f"{a / 1000:.0f} µs" if a >= 1000 else f"{a} ns"
    if a < NS:
        return f"{a / 1e6:.0f} ms"
    if a < 120 * NS:
        return f"{a / NS:.3f}".rstrip("0").rstrip(".") + " s"
    if a < 7200 * NS:
        return f"{a / 60 / NS:.1f} min"
    return f"{a / 3600 / NS:.1f} h"


def _signed(ns: int) -> str:
    return ("-" if ns < 0 else "+") + _dur(ns)


class ClockTools:
    def __init__(self, tools: Tools) -> None:
        self.t = tools
        self.app = tools.app

    # -- the model -------------------------------------------------------------------------------

    def model(self, ctx: _Ctx, force: bool = False) -> CaseClock:
        """The case clock, recomputed first when sources, manual entries or marker rules changed."""
        rows = query.sources(ctx.conn)
        al.refresh(ctx.conn, rows, self.app.markers, force=force)
        return ck.load(ctx.conn)

    def clock_name(self, ctx: _Ctx, cc: CaseClock) -> str:
        if cc.reference is None:
            return "no case clock (no source with timestamps)"
        s = ctx.by_id.get(cc.reference)
        what = "wall clock" if cc.kind == "wall" else "monotonic clock (seconds since boot)"
        from .tools import _tail

        name = _tail(s.name, 48) if s else f"source {cc.reference}"
        return f"{what} of source {cc.reference} ({name})"

    def fmt_case(self, cc: CaseClock, t: int, short: bool = True) -> str:
        if cc.kind == "mono":
            return fmt.mono_ts(t)
        return fmt_wall(t)[5:] if short else fmt_wall(t)

    def describe(self, sc: SegClock, cc: CaseClock) -> tuple[str, str | None]:
        """(offset text, error text) of one segment clock."""
        p = sc.pieces[0]
        if sc.native == "mono" and cc.kind == "wall":
            text = f"boot at {fmt_wall(p.map(0))}"
        else:
            text = _signed(p.offset_ns)
        if len(sc.pieces) > 1:
            text += f" ({len(sc.pieces)} pieces: clock stopped or jumped)"
        if p.drift_ppm:
            text += f", drift {p.drift_ppm:+.1f} ppm"
        err = None if sc.exact else ("±" + _dur(sc.error_ns) if sc.error_ns is not None else "error unknown")
        return text, err

    # -- scanning on the case clock -----------------------------------------------------------------

    def scan(
        self,
        conn: sqlite3.Connection,
        cc: CaseClock,
        units: Sequence[SegClock],
        where: list[str],
        params: list[Any],
        lo: int | None,
        hi: int | None,
        limit: int,
        after: _Pos | None = None,
        before: _Pos | None = None,
        desc: bool = False,
    ) -> list[tuple[int, Rec]]:
        """Records of ``units`` passing ``where`` with case time in [lo, hi], in case-time order
        (descending with ``desc``), strictly after ``after`` / before ``before``."""
        groups: dict[tuple[Any, ...], list[SegClock]] = {}
        for sc in units:
            key = ("wall", tuple(sc.pieces)) if sc.native == "wall" else ("mono", sc.source_id, sc.seg)
            groups.setdefault(key, []).append(sc)
        if after is not None:
            lo = after.t if lo is None else max(lo, after.t)
        if before is not None:
            hi = before.t if hi is None else min(hi, before.t)
        order = "DESC" if desc else ""
        found: list[tuple[int, Rec]] = []
        cond0 = " AND ".join(where) if where else "1"
        for (native, *_), scs in groups.items():
            ranges = " OR ".join("r.id BETWEEN ? AND ?" for _ in scs)
            rparams: list[Any] = [x for sc in scs for x in sc.id_range]
            for n_lo, n_hi in scs[0].native_windows(lo, hi):
                if native == "wall":
                    tcond, col = "r.ts BETWEEN ? AND ?", "r.ts"
                else:
                    tcond = f"r.ts IS NULL AND r.source_id = {scs[0].source_id} AND r.ts_src BETWEEN ? AND ?"
                    col = "r.ts_src"
                tparams = [
                    n_lo if n_lo is not None else -query.UPPER_BOUND,
                    n_hi if n_hi is not None else query.UPPER_BOUND,
                ]
                sql = (
                    f"SELECT {query.REC_COLS} FROM {query.REC_FROM} WHERE ({ranges}) AND {tcond} "
                    f"AND {cond0} ORDER BY {col} {order}, r.id {order} LIMIT ?"
                )
                for row in conn.execute(sql, [*rparams, *tparams, *params, limit + SLACK]):
                    r = Rec(*row)
                    t = cc.case_time(r.source_id, r.seg, r.ts, r.ts_src)
                    if t is None or (lo is not None and t < lo) or (hi is not None and t > hi):
                        continue
                    if after is not None and (t, r.id) <= (after.t, after.id):
                        continue
                    if before is not None and (t, r.id) >= (before.t, before.id):
                        continue
                    found.append((t, r))
        found.sort(key=lambda x: (x[0], x[1].id), reverse=desc)
        return found[:limit]

    def _out(self, ctx: _Ctx, cc: CaseClock, t: int, r: Rec) -> TimelineRecord:
        o = self.t._rec_out(r, ctx)
        sc = cc.get(r.source_id, r.seg)
        approx = sc is not None and not sc.exact
        return TimelineRecord(
            cite=o.cite,
            ts=self.fmt_case(cc, t, short=False),
            approx=approx or None,
            level=o.level,
            tag=o.tag,
            pid=o.pid,
            tid=o.tid,
            msg=o.msg,
            cut=o.cut,
            art=o.art,
        )

    def _line(self, cc: CaseClock, t: int, o: TimelineRecord, mark: str = "") -> str:
        from .tools import RecordOut

        ro = RecordOut(cite=o.cite, level=o.level, tag=o.tag, pid=o.pid, tid=o.tid, msg=o.msg, art=o.art)
        short = ("~" if o.approx else "") + self.fmt_case(cc, t)
        return mark + self.t.line(ro, short)

    def _legend(self, ctx: _Ctx, cc: CaseClock, shown: Sequence[Rec]) -> list[str]:
        lines = []
        seen: set[tuple[int, int]] = set()
        for r in shown:
            sc = cc.get(r.source_id, r.seg)
            if sc is None or sc.exact or (r.source_id, sc.seg) in seen:
                continue
            seen.add((r.source_id, sc.seg))
            off, err = self.describe(sc, cc)
            lines.append(
                f"~ source {r.source_id}"
                + (f" seg {sc.seg}" if len(cc.of_source(r.source_id)) > 1 else "")
                + f": {sc.conf} {err}, {off} ({sc.method})"
            )
            if len(lines) >= 6:
                break
        return lines

    def _case_time_of(self, ctx: _Ctx, cc: CaseClock, cite: str, role: str) -> int:
        rid = self.t._anchor(ctx, cite)
        r = query.records_by_id(ctx.conn, [rid]).get(rid)
        if r is None:
            from .tools import ToolInputError

            raise ToolInputError(f"{role}: no record {cite} in case '{ctx.case}'")
        t = cc.case_time(r.source_id, r.seg, r.ts, r.ts_src)
        if t is None:
            from .tools import ToolInputError

            raise ToolInputError(
                f"{role}: {r.cite} is not on the case clock (its source has no anchor); see align(case)"
            )
        return t

    def _resolve_case_time(
        self, ctx: _Ctx, cc: CaseClock, spec: str, role: str, span: tuple[int, int], other: int | None
    ) -> tuple[int, int]:
        """(case ns, granularity) for a timeline from/to."""
        from .tools import _CITE_OFFSET, Bound, Span, ToolInputError, duration_ns

        text = spec.strip()
        m = _CITE_OFFSET.fullmatch(text)
        if m:
            t = self._case_time_of(ctx, cc, m[1], role)
            if m[2]:
                d = duration_ns(m[3])
                t = t + d if m[2] == "+" else t - d
            return t, 1
        if text.startswith("+") and role == "to" and other is not None:
            return other + duration_ns(text[1:]), 1
        sp = Span(span if cc.kind == "wall" else None, span if cc.kind == "mono" else None)
        b: Bound = self.t.resolve_time(ctx, text, role, sp, None)
        if b.clock != cc.kind:
            kind = "wall-clock times" if cc.kind == "wall" else "seconds since boot"
            raise ToolInputError(
                f"{role}={spec!r}: the case clock uses {kind}; give {role} as timeline prints it"
            )
        return b.ns, b.gran

    def _span(self, ctx: _Ctx, cc: CaseClock, units: Sequence[SegClock]) -> tuple[int, int] | None:
        keys = {(u.source_id, u.seg): u for u in units}
        ts: list[int] = []
        for sid, seg, t0, t1, m0, m1 in ctx.conn.execute(
            "SELECT source_id, seg, ts_first, ts_last, ts_src_first, ts_src_last FROM segments"
        ):
            sc = keys.get((sid, seg))
            if sc is None:
                continue
            for v in (t0, t1) if sc.native == "wall" else (m0, m1):
                if v is not None:
                    ts.append(sc.map(v))
        return (min(ts), max(ts)) if ts else None

    # -- timeline ---------------------------------------------------------------------------------

    def timeline(
        self,
        case: str,
        filter: Filter | None = None,
        query_text: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Any:
        from .tools import Output, ToolInputError, _id_ranges

        warnings: list[str] = []
        key = fmt.query_key(
            {"q": query_text, "f": filter.model_dump(by_alias=True, exclude_none=True) if filter else None}
        )
        after = _read_cursor(cursor, key) if cursor else None
        limit = min(limit, self.t.caps.max_lines)
        with self.t._case(case) as ctx:
            cc = self.model(ctx)
            base = filter.model_copy(update={"from_": None, "to": None}) if filter else None
            rf = self.t.resolve_filter(ctx, base, warnings)
            selected = set(rf.sources) if rf.sources is not None else set(ctx.by_id)
            units = [sc for (sid, _), sc in cc.segs.items() if sid in selected]
            unplaced = sorted(
                s.id for s in ctx.sources if s.id in selected and s.records and not cc.of_source(s.id)
            )
            where, params = rf.where(frozenset({"sources"}))
            if query_text:
                where.append(query.FTS_MATCH)
                params.append(query.fts_query(self.app.redactor.redact(query_text)))
            span = self._span(ctx, cc, units)
            lo = hi = None
            if filter is not None and span is not None:
                if filter.from_:
                    lo, _ = self._resolve_case_time(ctx, cc, filter.from_, "from", span, None)
                if filter.to:
                    t, g = self._resolve_case_time(ctx, cc, filter.to, "to", span, lo)
                    hi = t + g - 1
            if lo is not None and hi is not None and lo > hi:
                raise ToolInputError(f"'from' ({filter.from_ if filter else ''}) is after 'to'")
            try:
                found = self.scan(ctx.conn, cc, units, where, params, lo, hi, limit + 1, after=after)
            except sqlite3.OperationalError as e:
                from .tools import _fts_error

                raise ToolInputError(_fts_error(query_text or "", e)) from None
            more = len(found) > limit
            found = found[:limit]
            outs = [self._out(ctx, cc, t, r) for t, r in found]
            head = f"timeline on the case clock: {self.clock_name(ctx, cc)}"
            ids = sorted({sc.source_id for sc in units})
            head += f"; sources {_id_ranges(ids) or 'none'}"
            lines = [head]
            page = fmt.Page(limit, self.t.caps.max_bytes, reserve=900)
            shown: list[Rec] = []
            shown_out: list[TimelineRecord] = []
            for (t, r), o in zip(found, outs, strict=True):
                if not page.add(self._line(cc, t, o)):
                    more = True
                    break
                shown.append(r)
                shown_out.append(o)
            lines += page.lines
            next_cursor = None
            if not shown:
                lines.append(
                    "no records on the case clock pass the filter" + (" after the cursor" if after else "")
                )
            lines += self._legend(ctx, cc, shown)
            if more and shown:
                last_t = cc.case_time(shown[-1].source_id, shown[-1].seg, shown[-1].ts, shown[-1].ts_src)
                next_cursor = _make_cursor(_Pos(last_t or 0, shown[-1].id), key)
                lines.append(f'more: cursor="{next_cursor}", or narrow with level, tags, from/to or query')
            if unplaced:
                lines.append(
                    f"not on the case clock (no anchor): sources {_id_ranges(unplaced)}; "
                    "align(case, mode='anchor', at=..., ref=...) places them"
                )
            if any(o.cut for o in shown_out):
                lines.append("messages cut with … are complete in get_records(ids=[…])")
            lines += [f"warning: {w}" for w in warnings]
            data = TimelineResult(
                case=case,
                clock=self.clock_name(ctx, cc),
                records=shown_out,
                next_cursor=next_cursor,
                unplaced=[str(i) for i in unplaced],
                warnings=warnings,
            )
            return Output("\n".join(lines), data)

    # -- context(scope='all') -------------------------------------------------------------------------

    def context_all(
        self, ctx: _Ctx, anchor: Rec, b: int, a: int
    ) -> tuple[list[str], list[Any], str, list[str]]:
        """(text lines with '>' on the anchor, records out, head, extra lines) on the case clock."""
        from .tools import ToolInputError

        cc = self.model(ctx)
        t = cc.case_time(anchor.source_id, anchor.seg, anchor.ts, anchor.ts_src)
        if t is None:
            raise ToolInputError(
                f"{anchor.cite} is not on the case clock (its source has no anchor to the other sources); "
                "use scope='source', or align(case, mode='anchor', ...) first"
            )
        visible = set(ctx.by_id)
        units = [sc for (sid, _), sc in cc.segs.items() if sid in visible]
        pos = _Pos(t, anchor.id)
        prev = self.scan(ctx.conn, cc, units, [], [], None, None, b, before=pos, desc=True)
        nxt = self.scan(ctx.conn, cc, units, [], [], None, None, a, after=pos)
        rows = [*reversed(prev), (t, anchor), *nxt]
        outs = [self._out(ctx, cc, tt, r) for tt, r in rows]
        texts = [
            self._line(cc, tt, o, "> " if r.id == anchor.id else "  ")
            for (tt, r), o in zip(rows, outs, strict=True)
        ]
        head = f"all sources around {anchor.cite} on the case clock ({self.clock_name(ctx, cc)}):"
        extra = self._legend(ctx, cc, [r for _, r in rows])
        unplaced = sorted(s.id for s in ctx.sources if s.records and not cc.of_source(s.id))
        if unplaced:
            from .tools import _id_ranges

            extra.append(f"not on the case clock (no anchor): sources {_id_ranges(unplaced)}")
        return texts, outs, head, extra

    # -- align ------------------------------------------------------------------------------------------

    def align(
        self,
        case: str,
        mode: str = "auto",
        source: str | None = None,
        seg: int | None = None,
        offset: str | None = None,
        at: str | None = None,
        ref: str | None = None,
    ) -> Any:
        from .tools import ToolInputError

        with self.t._case(case) as ctx:
            sid = self._source_arg(ctx, source) if source is not None else None
            force = False
            if mode == "offset":
                if sid is None or offset is None:
                    raise ToolInputError("mode='offset' needs source and offset (e.g. offset='+1.5s')")
                cc = self.model(ctx)
                off = self._offset_arg(ctx, cc, sid, offset)
                if cc.reference is not None and sid in self._ref_sources(ctx, cc):
                    raise ToolInputError(
                        f"source {sid} is on the reference clock; set another reference first "
                        "(mode='reference') or give the offset for another source"
                    )
                mono = [sc for sc in cc.of_source(sid) if sc.native == "mono"]
                s = ctx.by_id[sid]
                if seg is None and s.ts_min is None and s.segments > 1:
                    raise ToolInputError(
                        f"source {sid} has {s.segments} segments (boots) with their own clocks; give seg"
                    )
                if mono and cc.kind == "wall" and seg is None and len(mono) == 1:
                    seg = mono[0].seg
                al.add_manual(ctx.conn, "offset", sid, seg, offset_ns=off)
                force = True
            elif mode == "anchor":
                if at is None or ref is None:
                    raise ToolInputError(
                        "mode='anchor' needs at and ref: two citations of events that happened at the same "
                        "moment, e.g. at='log:5#120', ref='log:1#3400'"
                    )
                ra, rb = self._record(ctx, at, "at"), self._record(ctx, ref, "ref")
                if ra.source_id == rb.source_id and ra.seg == rb.seg:
                    raise ToolInputError("at and ref are on the same clock (same source and segment)")
                delta = 0
                if offset is not None:
                    try:
                        delta = al.parse_duration(offset.strip().lstrip("+"), "offset")
                    except ValueError as e:
                        raise ToolInputError(str(e)) from None
                al.add_manual(
                    ctx.conn, "anchor", ra.source_id, ra.seg, offset_ns=-delta, at_id=ra.id, ref_id=rb.id
                )
                force = True
            elif mode == "reference":
                if sid is None:
                    raise ToolInputError(
                        "mode='reference' needs source: the source whose clock becomes the case clock"
                    )
                al.add_manual(ctx.conn, "reference", sid)
                force = True
            elif mode == "reset":
                al.reset_manual(ctx.conn, sid)
                force = True
            elif mode != "auto":
                raise ToolInputError("mode must be auto, offset, anchor, reference or reset")
            cc = self.model(ctx, force=force)
            return self._align_output(ctx, cc)

    def _offset_arg(self, ctx: _Ctx, cc: CaseClock, sid: int, offset: str) -> int:
        """A duration (case time minus source time), or for a monotonic source on a wall-clock case
        clock the boot time as a case-clock timestamp ('2026-01-01 07:59:58.000')."""
        from .core.timeparse import wall_ns
        from .tools import _FULL, ToolInputError

        text = offset.strip()
        try:
            return al.parse_duration(text.lstrip("+"), "offset")
        except ValueError as e:
            s = ctx.by_id[sid]
            m = _FULL.fullmatch(text)
            if m and cc.kind == "wall" and s.ts_min is None:
                try:
                    return wall_ns(
                        int(m[1]),
                        int(m[2]),
                        int(m[3]),
                        int(m[4] or 0),
                        int(m[5] or 0),
                        int(m[6] or 0),
                        m[7] or "",
                    )
                except ValueError:
                    pass
            hint = (
                " or, for this source's monotonic clock, its boot time on the case clock "
                "('2026-01-01 07:59:58.000')"
                if s.ts_min is None
                else ""
            )
            raise ToolInputError(f"{e}{hint}") from None

    def _ref_sources(self, ctx: _Ctx, cc: CaseClock) -> set[int]:
        return {sid for (sid, _), sc in cc.segs.items() if sc.exact}

    def _source_arg(self, ctx: _Ctx, source: str) -> int:
        from .tools import ToolInputError, _id_ranges

        text = source.strip().removeprefix("log:")
        if not text.isdigit() or int(text) not in ctx.by_id:
            raise ToolInputError(
                f"unknown source {source!r}; sources: {_id_ranges(list(ctx.by_id)) or 'none'}"
            )
        return int(text)

    def _record(self, ctx: _Ctx, cite: str, role: str) -> Rec:
        from .tools import ToolInputError

        rid = self.t._anchor(ctx, cite)
        r = query.records_by_id(ctx.conn, [rid]).get(rid)
        if r is None:
            raise ToolInputError(f"{role}: no record {cite}")
        if r.ts is None and r.ts_src is None:
            raise ToolInputError(f"{role}: {r.cite} has no timestamp")
        return r

    def align_rows(self, ctx: _Ctx, cc: CaseClock) -> list[SourceClockOut]:
        segs: dict[int, list[int]] = {}
        for sid, seg, n, t0, m0 in ctx.conn.execute(
            "SELECT source_id, seg, count, ts_first, ts_src_first FROM segments ORDER BY source_id, seg"
        ):
            if n and (t0 is not None or m0 is not None):
                segs.setdefault(sid, []).append(seg)
        out: list[SourceClockOut] = []
        for s in ctx.sources:
            if not s.records or s.id not in segs:
                continue
            entries = [(seg, cc.get(s.id, seg)) for seg in segs[s.id]]
            kinds = {None if sc is None else (sc.conf, tuple(sc.pieces)) for _, sc in entries}
            multi = len(kinds) > 1
            for seg, sc in entries if multi else entries[:1]:
                if sc is None:
                    out.append(
                        SourceClockOut(
                            source=str(s.id), name=s.name, seg=seg if multi else None, placed=False
                        )
                    )
                    continue
                off, err = self.describe(sc, cc)
                out.append(
                    SourceClockOut(
                        source=str(s.id),
                        name=s.name,
                        seg=seg if multi else None,
                        conf=sc.conf,
                        offset=None if sc.exact else off,
                        error=err,
                        method=sc.method + (f" (source {sc.via})" if sc.via is not None else ""),
                    )
                )
        return out

    def _align_output(self, ctx: _Ctx, cc: CaseClock) -> Any:
        from .tools import Output, _id_ranges, _tail

        rows = self.align_rows(ctx, cc)
        lines = [f"case clock: {self.clock_name(ctx, cc)}"]
        exact = [int(r.source) for r in rows if r.conf == "exact"]
        if exact:
            what = "wall-clock sources of the reference file" if cc.kind == "wall" else "the reference clock"
            lines.append(f"{_id_ranges(exact)}  exact  [{what}]")
        for r in rows:
            if r.conf == "exact":
                continue
            name = _tail(r.name, 48)
            seg = f" seg {r.seg}" if r.seg is not None else ""
            if not r.placed:
                lines.append(
                    f"{r.source}{seg}  not placed: no anchor to another clock  {name}; "
                    f"align(case, mode='anchor', at='log:{r.source}#<seq>', ref=<same moment elsewhere>)"
                )
                continue
            parts = [p for p in (r.conf, r.offset, r.error) if p]
            lines.append(f"{r.source}{seg}  {'  '.join(parts)}  {name}  [{r.method}]")
        row = ctx.conn.execute("SELECT value FROM meta WHERE key = 'clock_notes'").fetchone()
        notes = [n for n in (row[0].split("\n") if row and row[0] else []) if n]
        manual = al.manual_entries(ctx.conn)
        if manual:
            lines.append(
                f"{len(manual)} manual entr{'y' if len(manual) == 1 else 'ies'}; mode='reset' removes them"
            )
        lines += [f"note: {n}" for n in notes]
        lines.append("timeline(case) lists all sources merged on this clock; ~ marks mapped times")
        return Output(
            "\n".join(lines),
            AlignResult(case=ctx.case, clock=self.clock_name(ctx, cc), sources=rows, notes=notes),
        )


def _make_cursor(pos: _Pos, key: str) -> str:
    raw = json.dumps({"t": pos.t, "i": pos.id, "k": key}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _read_cursor(cursor: str, key: str) -> _Pos:
    from .tools import ToolInputError

    try:
        pad = "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(cursor + pad))
        t, rid, ckey = data["t"], data["i"], data["k"]
    except (ValueError, KeyError, TypeError):
        raise ToolInputError(
            "invalid cursor; pass the cursor string exactly as timeline printed it"
        ) from None
    if not isinstance(t, int) or not isinstance(rid, int) or rid >> SEQ_BITS <= 0 or not rid & SEQ_MASK:
        raise ToolInputError("invalid cursor")
    if ckey != key:
        raise ToolInputError(
            "this cursor belongs to a different timeline; repeat the same filter and query with the cursor"
        )
    return _Pos(t, rid)
