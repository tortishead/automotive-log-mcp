"""``detect``: hits of the detector rules, collapsed, with a hint what to read next."""

from __future__ import annotations

from typing import Any

from ..core import format as fmt
from ..detect.engine import SEVERITIES, RuleResult
from .base import CaseCtx, ToolBase
from .models import DetectResult, Filter, HitOut, Output, RuleHits, ToolInputError
from .text import delta_text, join_lines, reserve_bytes, warn_lines


class DetectTools(ToolBase):
    """``detect``."""

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
        with self.open_case(case) as ctx:
            rf = self.resolve_filter(ctx, filter, warnings)
            results = self.run_rules(ctx, rf, chosen)
            hit = [r for r in results if r.count]
            hit.sort(
                key=lambda r: (
                    SEVERITIES.index(r.rule.severity),
                    r.first_ts if r.first_ts is not None else 1 << 62,
                )
            )
            no_hits = [r.rule.name for r in results if not r.count]
            page = fmt.Page(
                self.caps.max_lines, self.caps.max_bytes, reserve=reserve_bytes(self.caps.max_bytes, 800)
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
                    o = self.rec_out(g.first, ctx)
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
                            f"     followed by {g.follow.cite}{delta_text(g.first, g.follow)}: "
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
            return Output(join_lines([header, *page.lines, *footer, *warn_lines(warnings)]), data)

    def _rule_out(self, ctx: CaseCtx, res: RuleResult) -> RuleHits:
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
