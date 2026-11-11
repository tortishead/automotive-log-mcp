"""``count``: records by tag, level, pid, source or template, optionally per time bucket."""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from typing import Any

from ..core import format as fmt
from ..core.timeparse import NS
from ..model import SEQ_BITS, SEQ_MASK, citation
from ..store import query
from . import limits
from .base import CaseCtx, ToolBase
from .models import CountBucket, CountGroup, CountResult, Filter, Output, ToolInputError
from .text import (
    bar_text,
    dur_text,
    fts_error,
    join_lines,
    looks_like_regex,
    nice_step_text,
    reserve_bytes,
    sources_word,
    tail_text,
    warn_lines,
)
from .timespec import DUR, duration_ns, sources_span


class CountTools(ToolBase):
    """``count``."""

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
        ctx: CaseCtx,
        q: str | None,
        rx: re.Pattern[str] | None,
        where: list[str],
        params: list[Any],
        warnings: list[str],
    ) -> tuple[list[str], list[Any], int]:
        """Add the query's conditions. Returns where, params and the sample factor: above
        ``limits.AGG_ROWS`` text matches only every k-th match (by rowid) is counted (estimates, ≈)."""
        if q is None:
            return where, params, 1
        if rx is not None:
            pw, pp = query.regex_prefilter(q)
            return [*pw, *where], [*pp, *params], 1
        fts = query.fts_query(q)
        try:
            n_fts = query.fts_count(ctx.conn, fts)
        except sqlite3.OperationalError as e:
            raise ToolInputError(fts_error(q, e)) from None
        if n_fts > limits.AGG_ROWS:
            k = -(-n_fts // limits.AGG_ROWS)
            warnings.append(
                f"{n_fts:,} records contain the words: counts (≈) are estimated from every {k}th of them; "
                "narrow with level, tags, from/to or a longer query for exact counts"
            )
            return [query.FTS_SAMPLE, *where], [fts, k, *params], k
        return [query.FTS_MATCH, *where], [fts, *params], 1

    def _labels(self, ctx: CaseCtx, by: str, keys: list[Any]) -> dict[Any, str]:
        if by == "tag":
            names = query.tag_names(ctx.conn, keys)
            return {k: names.get(k, "-") if k is not None else "-" for k in keys}
        if by == "pid":
            procs = query.process_names(ctx.conn, [k for k in keys if k is not None])
            return {k: f"{k} {procs[k]}" if k in procs else ("-" if k is None else str(k)) for k in keys}
        if by == "source":
            return {k: f"{k} {tail_text(ctx.by_id[k].name, 70)}" if k in ctx.by_id else str(k) for k in keys}
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
    ) -> Output[CountResult]:
        warnings: list[str] = []
        q, rx = self._query_arg(text, mode, warnings)
        if by is not None and by not in query.COUNT_KEYS:
            raise ToolInputError(f"by must be one of {', '.join(query.COUNT_KEYS)}")
        limit_eff = min(limit, self.caps.max_lines)
        if limit > limit_eff:
            warnings.append(f"limit {limit} reduced to {limit_eff} (config caps.max_lines)")
        step: int | None = None
        if bucket is not None:
            if not re.fullmatch(DUR, bucket.strip()):
                raise ToolInputError(f"invalid bucket {bucket!r}; use a duration such as '10s', '1m', '1h'")
            step = duration_ns(bucket)
            if step < NS // 1000:
                raise ToolInputError("bucket must be at least 1ms")
        with self.open_case(case) as ctx:
            rf = self.resolve_filter(ctx, filter, warnings)
            where, params = rf.where()
            where, params, sample = self._match_where(ctx, q, rx, where, params, warnings)
            mono = False
            lo = hi = 0
            if step:
                selected = [ctx.by_id[i] for i in (rf.sources if rf.sources is not None else list(ctx.by_id))]
                span = sources_span(selected)
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
                            f"records of {sources_word(mono_only)} only a monotonic clock and are not in "
                            "the buckets; count them with filter sources and from='0.0'"
                        )
                if b_lo is None or b_hi is None:
                    raise ToolInputError("the selected sources have no timestamps to put into buckets")
                lo, hi = b_lo, b_hi
                n_b = hi // step - lo // step + 1
                max_b = min(limits.MAX_BUCKETS, self.caps.max_lines)
                if n_b > max_b:
                    raise ToolInputError(
                        f"bucket {bucket!r} gives {n_b:,} buckets over {dur_text(hi - lo)}; "
                        f"at most {max_b}: "
                        f"use bucket='{nice_step_text(-(-(hi - lo) // (max_b - 1)))}' or narrow from/to"
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
                self.caps.max_lines, self.caps.max_bytes, reserve=reserve_bytes(self.caps.max_bytes, 700)
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
                    bar = bar_text(n, peak, 20)
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
            return Output(join_lines([*lines, *page.lines, *footer, *warn_lines(warnings)]), data)
