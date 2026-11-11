"""``search``: FTS5 words or a regex over message and tag, collapsed by template, with cursors."""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from typing import Any

from ..core import format as fmt
from ..model import SEQ_BITS
from ..store import query
from ..store.query import Rec, RecordFilter
from . import limits
from .base import CaseCtx, ToolBase
from .models import Filter, Output, RecordOut, SearchResult, ToolInputError
from .text import (
    fts_error,
    join_lines,
    level_order,
    literal_lines,
    looks_like_regex,
    reserve_bytes,
    scan_note,
    warn_lines,
)

_PART_NAMES = {
    "sources": "sources filter",
    "time": "from/to filter",
    "level": "level filter",
    "tags": "tags filter",
    "not_tags": "not_tags filter",
    "pid": "pid filter",
    "attrs": "attrs filter",
}


def _part_value(rf: RecordFilter, part: str) -> str:
    if part == "level":
        return f" (level {rf.min_level})"
    if part == "pid":
        return f" (pid {rf.pid})"
    return ""


class SearchTools(ToolBase):
    """``search``."""

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
        with self.open_case(case) as ctx:
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
                    if fts and n_fts > limits.AGG_ROWS:
                        # Looking up every match costs ~1.3 µs; above AGG_ROWS group a 1/k sample.
                        sample = -(-n_fts // limits.AGG_ROWS)
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
                raise ToolInputError(fts_error(q, e)) from None
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
                return Output(join_lines(["No matches. " + reason, *warn_lines(warnings)]), data)
            if count_only:
                by_level = Counter[str]()
                for g in groups:
                    by_level[g.level or "-"] += g.count
                levels = ", ".join(f"{lvl} {n:,}" for lvl, n in sorted(by_level.items(), key=level_order))
                head = (
                    f"{approx}{fmt.plural(total, 'match', 'matches')}, "
                    f"{fmt.plural(len(groups), 'distinct message', 'distinct messages')}"
                )
                lines = [f"{head} ({levels})"]
                if scan and not exact:
                    lines.append(scan_note(scan, self.caps.regex_scan_rows))
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
                return Output(join_lines([*lines, *warn_lines(warnings)]), data)

            page = fmt.Page(
                self.caps.max_lines, self.caps.max_bytes, reserve=reserve_bytes(self.caps.max_bytes, 1400)
            )
            # (record, output, short time, short time of the last match)
            hits: list[tuple[Rec, RecordOut, str, str | None]] = []
            if collapse:
                window_groups = groups[offset : offset + limit_eff]
                firsts = query.records_by_id(ctx.conn, [g.first_id for g in window_groups])
                lasts = query.records_by_id(ctx.conn, [g.last_id for g in window_groups if g.count > 1])
                for g in window_groups:
                    r = firsts[g.first_id]
                    o = self.rec_out(r, ctx)
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
                        by_time_index=n_fts > limits.TIME_INDEX_ROWS,
                    )
                hits = [(r, self.rec_out(r, ctx), fmt.short_ts(r.ts, r.ts_src), None) for r in recs]
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
            lit_lines = literal_lines(shown)
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
                footer.append(scan_note(scan, self.caps.regex_scan_rows))
            footer.extend(self._other_sources(ctx, filter, rf, fts, rx))
            if busy:
                footer.append("An import is still running in this case; counts may grow.")
            text_out = join_lines([header, *page.lines, *lit_lines, *footer, *warn_lines(warnings)])
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
        ctx: CaseCtx,
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

    def _narrow_hint(self, ctx: CaseCtx, groups: list[query.Group], rf: RecordFilter) -> str:
        parts: list[str] = []
        by_level = Counter[str]()
        by_tag = Counter[int | None]()
        for g in groups:
            by_level[g.level or "-"] += g.count
            by_tag[g.tag_id] += g.count
        if not rf.min_level and len(by_level) > 1:
            parts.append(
                "level ("
                + ", ".join(f"{lvl} {n:,}" for lvl, n in sorted(by_level.items(), key=level_order))
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
        ctx: CaseCtx,
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
