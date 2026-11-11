"""``overview``: a first look at a case (levels, top values, segments, gaps, bursts, histogram)."""

from __future__ import annotations

from collections import Counter
from typing import Any

from ..core import format as fmt
from ..core.timeparse import NS
from ..model import citation
from ..store import query
from ..store.query import RecordFilter
from . import limits
from .base import CaseCtx, ToolBase
from .models import BurstOut, CountGroup, Filter, GapOut, Output, OverviewResult, SegmentOut, TimeBucket
from .text import cite_id, dur_text, join_lines, nice_step, nice_step_text, reserve_bytes, spark, warn_lines
from .timespec import full_ts_ns


class OverviewTools(ToolBase):
    """``overview``."""

    def overview(self, case: str, filter: Filter | None = None) -> Output[OverviewResult]:
        warnings: list[str] = []
        with self.open_case(case) as ctx:
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
                f"{fmt.short_ts(lo)} .. {fmt.short_ts(hi)} ({dur_text(hi - lo)})"
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
                step = nice_step(-(-(hi - lo + 1) // limits.MAX_BUCKETS))
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
            tags = query.group_counts(conn, "tag", where, params)[: limits.TOP_N]
            tnames = query.tag_names(conn, [k for k, _, _ in tags])
            data.top_tags = [
                CountGroup(
                    key=k, label=tnames.get(k, "-") if k is not None else "-", count=n, example=cite_id(i)
                )
                for k, n, i in tags
            ]
            lines.append("top tags: " + ", ".join(f"{g.label} {g.count:,}" for g in data.top_tags))
            pids = [(k, n, i) for k, n, i in query.group_counts(conn, "pid", where, params) if k is not None][
                : limits.TOP_N
            ]
            pnames = query.process_names(conn, [k for k, _, _ in pids])
            data.top_processes = [
                CountGroup(
                    key=k, label=f"{k} {pnames[k]}" if k in pnames else str(k), count=n, example=cite_id(i)
                )
                for k, n, i in pids
            ]
            if data.top_processes:
                lines.append("top pids: " + ", ".join(f"{g.label} {g.count:,}" for g in data.top_processes))
            tmpls = query.group_counts(conn, "template", where, params)[: limits.TOP_N]
            texts = query.template_texts(conn, [k for k, _, _ in tmpls if k is not None])
            ex = query.records_by_id(conn, [i for _, _, i in tmpls])
            lines.append("top messages (templates, ×count, an example):")
            for k, n, i in tmpls:
                r = ex.get(i)
                label = fmt.clip(texts.get(k, r.msg if r else "-").split("\n", 1)[0], 110)[0]
                data.top_templates.append(CountGroup(key=k, label=label, count=n, example=cite_id(i)))
                who = f"{r.level or '-'} {r.tag or ''}".strip() if r else ""
                lines.append(f"  ×{n:,}  {who}  {label}  e.g. {cite_id(i)}")
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
                gaps, complete = query.gaps(conn, where, params, lo, hi, limits.GAP_NS)
                data.gaps_complete = complete
                for g in gaps:
                    data.gaps.append(
                        GapOut(
                            before=cite_id(g.before_id) or "",
                            after=cite_id(g.after_id) or "",
                            from_ts=fmt.full_ts(g.before_ts) or "",
                            to_ts=fmt.full_ts(g.after_ts) or "",
                            seconds=round((g.after_ts - g.before_ts) / NS, 3),
                        )
                    )
                biggest = sorted(data.gaps, key=lambda g: -g.seconds)[:5]
                lines.append(
                    f"gaps > {limits.GAP_NS // NS} s without a record: "
                    + (
                        "none"
                        if not gaps
                        else f"{len(gaps)}"
                        + ("+" if not complete else "")
                        + ", longest: "
                        + "; ".join(
                            f"{dur_text(int(g.seconds * NS))} after {g.before} "
                            f"({fmt.short_ts(full_ts_ns(g.from_ts))}) until {g.after}"
                            for g in biggest
                        )
                    )
                )
                # error bursts
                data.bursts = self._bursts(ctx, where, params, lo, hi)
                if data.bursts:
                    lines.append(
                        f"error bursts (E/F per 10 s above 5× the median, at least {limits.BURST_MIN}):"
                    )
                    for bo in sorted(data.bursts, key=lambda x: -x.errors)[:5]:
                        t0, t1 = fmt.short_ts(full_ts_ns(bo.from_ts)), fmt.short_ts(full_ts_ns(bo.to_ts))
                        lines.append(
                            f"  {t0} .. {t1}  E/F {bo.errors:,}"
                            f"  top tag {bo.top_tag or '-'}  first {bo.first}"
                        )
                else:
                    lines.append("error bursts (E/F per 10 s above 5× the median): none")
            if hist and step:
                data.histogram = hist
                data.histogram_step = nice_step_text(step)
                peak = max(h.count for h in hist)
                epeak = max(h.errors for h in hist)
                lines.append(
                    f"histogram from {fmt.short_ts(full_ts_ns(hist[0].ts))}, {len(hist)} buckets of "
                    f"{data.histogram_step} (peak {peak:,} records, {epeak:,} E/F; "
                    f"count(case, bucket='{data.histogram_step}') for numbers):"
                )
                lines.append("  all |" + "".join(spark(h.count, peak) for h in hist) + "|")
                lines.append("  E/F |" + "".join(spark(h.errors, epeak) for h in hist) + "|")
            # detector counts and artifacts
            results = self.run_rules(ctx, rf, self.app.rules.ordered())
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
                10 * self.caps.max_lines, self.caps.max_bytes, reserve=reserve_bytes(self.caps.max_bytes, 300)
            )
            for line in lines:
                if not page.add(fmt.clip(line, 1200)[0]):
                    break
            out = page.lines
            if page.full:
                out.append(f"… ({fmt.cap_note(page)}); narrow with filter sources or from/to")
            return Output(join_lines([*out, *warn_lines(warnings)]), data)

    def _bursts(self, ctx: CaseCtx, where: list[str], params: list[Any], lo: int, hi: int) -> list[BurstOut]:
        rf_e = RecordFilter(min_level="E")
        ew, ep = rf_e.where()
        w, p = [*where, *ew], [*params, *ep]
        per = {b: n for b, _, n, _ in query.bucket_counts(ctx.conn, limits.BURST_NS, w, p)}
        if not per:
            return []
        n_buckets = hi // limits.BURST_NS - lo // limits.BURST_NS + 1
        counts = sorted([*per.values(), *([0] * max(0, n_buckets - len(per)))])
        median = counts[len(counts) // 2]
        threshold = max(5 * median, limits.BURST_MIN)
        hot = sorted(b for b, n in per.items() if n > threshold)
        runs: list[list[int]] = []
        for b in hot:
            if runs and b == runs[-1][-1] + limits.BURST_NS:
                runs[-1].append(b)
            else:
                runs.append([b])
        out: list[BurstOut] = []
        for run in sorted(runs, key=lambda r: -sum(per[b] for b in r))[:10]:
            b0, b1 = run[0], run[-1] + limits.BURST_NS - 1
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
                    first=cite_id(first) or "",
                    top_tag=top,
                )
            )
        out.sort(key=lambda b: b.from_ts)
        return out
