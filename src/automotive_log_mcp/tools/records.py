"""``context`` and ``get_records``, and the entry points of the clock and diff tools."""

from __future__ import annotations

from typing import Any

from ..core import format as fmt
from ..model import SEQ_BITS, SEQ_MASK, citation
from ..store import query
from .base import ToolBase
from .clock import AlignResult, TimelineResult
from .diff import DiffResult
from .models import ContextResult, DiffSide, Filter, GetRecordsResult, Output, RecordOut, ToolInputError
from .text import cut_bytes, id_ranges, join_lines, list_some, reserve_bytes
from .timespec import parse_log_cite


class RecordTools(ToolBase):
    """``context``, ``get_records``, and the clock and diff tools by delegation."""

    def context(
        self, case: str, at: str, before: int = 20, after: int = 20, scope: str = "source"
    ) -> Output[ContextResult]:
        with self.open_case(case) as ctx:
            rid = self.resolve_at(ctx, at)
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
                head = f"pid {anchor.pid} around {anchor.cite}, sources {id_ranges(ids)} by time:"
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
                outs = [self.rec_out(r, ctx) for r in recs]
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
            return Output(join_lines(lines), data)

    def get_records(self, case: str, ids: list[str]) -> Output[GetRecordsResult]:
        with self.open_case(case) as ctx:
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
            budget = self.caps.max_bytes - reserve_bytes(self.caps.max_bytes, 700)
            seen: set[str] = set()
            for cite, rid in wanted:
                if cite in seen:
                    continue
                seen.add(cite)
                r = recs.get(rid) if rid is not None else None
                if r is None:
                    missing.append(cite)
                    continue
                o = self.rec_out(r, ctx, full=True)
                t = tmpl.get(r.template_id or -1)
                o.template, o.literal = t, fmt.template_literal(t)
                block = self._record_block(o)
                n = fmt.utf8_len(block) + 1
                if size + n > budget:
                    if not out:  # one record larger than the cap: cut its message
                        keep = max(budget - 600, 200)
                        o.msg = cut_bytes(o.msg, keep)
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
                lines.append(f"not found: {list_some(missing)}")
            if not_shown:
                lines.append(
                    f"not shown (output cap of {self.caps.max_bytes} bytes): {list_some(not_shown)}; "
                    "ask again for them"
                )
            return Output(
                join_lines(lines),
                GetRecordsResult(case=case, records=out, missing=missing, not_shown=not_shown),
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

    def timeline(
        self,
        case: str,
        filter: Filter | None = None,
        query_text: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Output[TimelineResult]:
        return self.clock.timeline(case, filter, query_text, limit, cursor)

    def align(
        self,
        case: str,
        mode: str = "auto",
        source: str | None = None,
        seg: int | None = None,
        offset: str | None = None,
        at: str | None = None,
        ref: str | None = None,
    ) -> Output[AlignResult]:
        return self.clock.align(case, mode, source, seg, offset, at, ref)

    def diff(
        self, case: str, a: DiffSide, b: DiffSide, min_ratio: float = 4.0, limit: int = 12
    ) -> Output[DiffResult]:
        return self.differ.diff(case, a, b, min_ratio, limit)

    def case_time(self, case: str, cite: str) -> int | None:
        """A record's time on the case clock (ns), as timeline orders it; None when its source is
        not placed. For the evaluation's order check."""
        with self.open_case(case) as ctx:
            cc = self.clock.model(ctx)
            rid = self.resolve_at(ctx, cite)
            r = query.records_by_id(ctx.conn, [rid]).get(rid)
            if r is None:
                raise ToolInputError(f"no record {cite} in case '{case}'")
            return cc.case_time(r.source_id, r.seg, r.ts, r.ts_src)

    def clock_summary(self, case: str) -> tuple[str, dict[str, str]]:
        """(case clock name, short clock text per source id) for list_sources; exact sources omitted."""
        with self.open_case(case) as ctx:
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
