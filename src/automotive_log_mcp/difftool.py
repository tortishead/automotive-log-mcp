"""``diff`` (M6): which messages occur only in one set of records, and which changed in rate.

Each side (``a``, ``b``) is a record filter plus an optional segment: one or more sources (two
bugreports, a good and a bad run), one boot of a kernel log (``seg``), or a time window of the
same source (before and after an event). A **message** is a tag, a level and a template (the
message with numbers, ids and paths masked, as ``collapse`` groups them); templates are shared by
all sources of a case, so the same line in two files is the same message.

The result has three lists, each item cited by an example record (its first occurrence):

- only in B, only in A: messages with no record on the other side, most severe first;
- changed: messages on both sides whose share of their side's records differs by ``min_ratio``
  or more (at least ``MIN_CHANGED`` records on the larger side), largest change first.

Ratios compare shares, not counts, so a side that covers ten times as many records does not make
every message look ten times more frequent. Times play no part in the comparison: each side's
``from``/``to`` are read on its own sources' clocks (as in ``search``), so the two sides may be on
different clocks (a dmesg boot against a logcat, two devices) without any alignment.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from .core import format as fmt
from .core.levels import LEVEL_RANK
from .model import SEQ_BITS, SEQ_MASK, citation
from .store import query
from .store.query import RecordFilter

if TYPE_CHECKING:
    from .tools import DiffSide, Filter, Output, Tools, _Ctx

MIN_CHANGED = 5  # a changed message has at least this many records on its larger side
DEFAULT_RATIO = 4.0


class DiffItem(BaseModel):
    tag: str | None = None
    level: str | None = None
    template: str = Field(description="The message with variable parts as <*>.")
    count_a: int
    count_b: int
    ratio: float | None = Field(
        default=None, description="Changed: B's share of its records over A's share (above 1: more in B)."
    )
    cite_a: str | None = Field(default=None, description="First record of the message in A.")
    cite_b: str | None = Field(default=None, description="First record of the message in B.")


class DiffSideOut(BaseModel):
    selects: str = Field(description="What the side selects, as given.")
    records: int
    messages: int = Field(description="Distinct messages (tag, level, template).")


class DiffResult(BaseModel):
    case: str
    a: DiffSideOut
    b: DiffSideOut
    same: int = Field(description="Messages on both sides.")
    min_ratio: float
    only_b_total: int
    only_a_total: int
    changed_total: int
    only_b: list[DiffItem]
    only_a: list[DiffItem]
    changed: list[DiffItem]
    warnings: list[str] = Field(default_factory=list)


Key = tuple[int | None, str | None, int | None]  # tag id, level, template id


@dataclass
class _Side:
    name: str
    selects: str
    rf: RecordFilter
    groups: dict[Key, tuple[int, int]]  # key -> (count, first record id)
    total: int
    kinds: set[str]  # clock kinds of the selected sources: wall, mono


def _rank(level: str | None) -> int:
    return LEVEL_RANK.get(level or "", -1)


def _cite(rid: int | None) -> str | None:
    return citation(rid >> SEQ_BITS, rid & SEQ_MASK) if rid else None


class DiffTool:
    def __init__(self, tools: Tools) -> None:
        self.t = tools
        self.caps = tools.caps

    def _side(self, ctx: _Ctx, name: str, side: DiffSide, warnings: list[str]) -> _Side:
        from .tools import ToolInputError

        seg = side.seg
        side_warnings: list[str] = []
        try:
            rf = self.t.resolve_filter(ctx, side, side_warnings)
        except ToolInputError as e:
            raise ToolInputError(f"{name}: {e}") from None
        warnings.extend(f"{name}: {w}" for w in side_warnings)
        where, params = rf.where()
        if seg is not None:
            if not side.sources or len(side.sources) != 1:
                raise ToolInputError(f"{name}.seg needs exactly one source in {name}.sources, e.g. ['2']")
            sid = rf.sources[0] if rf.sources else 0
            segs = [s[1] for s in query.segments(ctx.conn, [sid])]
            if seg not in segs:
                have = ", ".join(map(str, segs)) or "none"
                raise ToolInputError(
                    f"{name}.seg {seg}: source {sid} has segments {have} (overview lists them)"
                )
            where.append("r.seg = ?")
            params.append(seg)
        sql = (
            "SELECT r.tag_id, r.level, r.template_id, count(*), min(r.id) FROM records r"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " GROUP BY 1, 2, 3"
        )
        groups: dict[Key, tuple[int, int]] = {}
        total = 0
        for tag_id, level, tpl, n, first in ctx.conn.execute(sql, params):
            groups[(tag_id, level, tpl)] = (n, first)
            total += n
        if total == 0:
            raise ToolInputError(
                f"{name} selects no records; check its sources, seg, from/to and level "
                "(list_sources shows each source's time range, overview its segments)"
            )
        ids = rf.sources if rf.sources is not None else list(ctx.by_id)
        rows = [ctx.by_id[i] for i in ids if i in ctx.by_id]
        kinds = {"wall" if s.ts_min is not None else "mono" for s in rows if s.records}
        return _Side(
            name,
            _describe(side, seg),
            rf,
            groups,
            total,
            kinds,
        )

    def diff(
        self,
        case: str,
        a: DiffSide,
        b: DiffSide,
        min_ratio: float = DEFAULT_RATIO,
        limit: int = 12,
    ) -> Output:
        from .tools import Output, ToolInputError

        if not min_ratio > 1:
            raise ToolInputError("min_ratio must be above 1, e.g. 4 (four times as frequent)")
        warnings: list[str] = []
        limit_eff = min(limit, self.caps.max_lines)
        if limit > limit_eff:
            warnings.append(f"limit {limit} reduced to {limit_eff} (config caps.max_lines)")
        with self.t._case(case) as ctx:
            sa = self._side(ctx, "a", a, warnings)
            sb = self._side(ctx, "b", b, warnings)
            if sa.rf.where() == sb.rf.where() and a.seg == b.seg:
                raise ToolInputError(
                    "a and b select the same records; give them different sources, seg or from/to"
                )
            only_a = [k for k in sa.groups if k not in sb.groups]
            only_b = [k for k in sb.groups if k not in sa.groups]
            both = [k for k in sa.groups if k in sb.groups]
            changed: list[tuple[float, Key]] = []
            for k in both:
                na, nb = sa.groups[k][0], sb.groups[k][0]
                if max(na, nb) < MIN_CHANGED:
                    continue
                ratio = (nb / sb.total) / (na / sa.total)
                if ratio >= min_ratio or ratio <= 1 / min_ratio:
                    changed.append((ratio, k))

            def severity(side: _Side, k: Key) -> tuple[int, int, int]:
                n, first = side.groups[k]
                return (-_rank(k[1]), -n, first)

            only_b.sort(key=lambda k: severity(sb, k))
            only_a.sort(key=lambda k: severity(sa, k))
            changed.sort(key=lambda x: (-abs(math.log(x[0])), -_rank(x[1][1]), sb.groups[x[1]][1]))

            shown_keys = [*only_b[:limit_eff], *only_a[:limit_eff], *(k for _, k in changed[:limit_eff])]
            tag_ids: list[int | None] = [*sorted({k[0] for k in shown_keys if k[0] is not None})]
            tags = query.tag_names(ctx.conn, tag_ids)
            tpls = query.template_texts(ctx.conn, sorted({k[2] for k in shown_keys if k[2] is not None}))
            no_tpl = [
                g[1] for k in shown_keys if k[2] is None for g in (sa.groups.get(k), sb.groups.get(k)) if g
            ]
            msgs = {rid: r.msg for rid, r in query.records_by_id(ctx.conn, no_tpl).items()} if no_tpl else {}

            def item(k: Key, ratio: float | None = None) -> DiffItem:
                ga, gb = sa.groups.get(k), sb.groups.get(k)
                text = tpls.get(k[2]) if k[2] is not None else None
                if text is None:
                    text = msgs.get((ga or gb or (0, 0))[1], "")
                return DiffItem(
                    tag=tags.get(k[0]) if k[0] is not None else None,
                    level=k[1],
                    template=fmt.clip(text, self.caps.max_msg_chars)[0],
                    count_a=ga[0] if ga else 0,
                    count_b=gb[0] if gb else 0,
                    ratio=round(ratio, 2) if ratio is not None else None,
                    cite_a=_cite(ga[1]) if ga else None,
                    cite_b=_cite(gb[1]) if gb else None,
                )

            data = DiffResult(
                case=case,
                a=DiffSideOut(selects=sa.selects, records=sa.total, messages=len(sa.groups)),
                b=DiffSideOut(selects=sb.selects, records=sb.total, messages=len(sb.groups)),
                same=len(both),
                min_ratio=min_ratio,
                only_b_total=len(only_b),
                only_a_total=len(only_a),
                changed_total=len(changed),
                only_b=[],
                only_a=[],
                changed=[],
                warnings=warnings,
            )
            lines = [
                f"A: {sa.selects}: {sa.total:,} records, {fmt.plural(len(sa.groups), 'message', 'messages')}",
                f"B: {sb.selects}: {sb.total:,} records, {fmt.plural(len(sb.groups), 'message', 'messages')}",
                f"in both: {len(both):,}; only in B: {len(only_b):,}{_levels(only_b)}; only in A: "
                f"{len(only_a):,}{_levels(only_a)}; changed {_ratio_text(min_ratio)} or more: "
                f"{len(changed):,}",
                "(message = tag + level + template; changes compare each message's share of its side's "
                "records)",
            ]
            if sa.kinds != sb.kinds:
                lines.append(
                    "A and B are on different clocks: the diff compares messages, not times "
                    "(from/to of each side: its own sources' clocks)"
                )
            # Each list gets a third of the byte budget, plus what earlier lists left unused.
            budget = self.caps.max_bytes - fmt.utf8_len("\n".join(lines)) - 300
            sections: list[tuple[str, list[tuple[float | None, Key]], list[DiffItem]]] = [
                ("Only in B, most severe first", [(None, k) for k in only_b], data.only_b),
                ("Changed, largest change first", [(r, k) for r, k in changed], data.changed),
                ("Only in A, most severe first", [(None, k) for k in only_a], data.only_a),
            ]
            left = budget
            for i, (title, entries, out) in enumerate(sections):
                if not entries:
                    continue
                share = max(left // (len(sections) - i), 400)
                page = fmt.Page(limit_eff, share, reserve=120)
                page.force(f"{title} ({len(entries):,}):")
                for rt, k in entries:
                    it = item(k, rt)
                    if rt is not None:
                        line = (
                            f"  ×{it.count_a:,} → ×{it.count_b:,} ({_ratio_text(rt)})  {it.cite_a} / "
                            f"{it.cite_b}  {_head(it)}  {it.template}"
                        )
                    else:
                        cite = it.cite_b if it.count_b else it.cite_a
                        n = it.count_b or it.count_a
                        line = f"  {cite}  {_head(it)}  {it.template}" + (f"  ×{n:,}" if n > 1 else "")
                    if not page.add(line):
                        break
                    out.append(it)
                if len(out) < len(entries):
                    how = fmt.cap_note(page) if page.capped_by == "bytes" else f"limit {limit_eff}"
                    page.force(
                        f"  … {len(entries) - len(out):,} more ({how}); narrow a and b with level or tags, "
                        "or raise limit"
                    )
                lines.extend(page.lines)
                left -= page.size
            if not (only_a or only_b or changed):
                lines.append(
                    "No difference: every message occurs on both sides, none changed "
                    f"{_ratio_text(min_ratio)} or more; lower min_ratio to see smaller changes"
                )
            lines.extend(f"warning: {w}" for w in warnings)
            return Output("\n".join(lines), data)


def _head(it: DiffItem) -> str:
    return " ".join(p for p in (it.level or "-", it.tag or "") if p)


def _ratio_text(r: float) -> str:
    """4.0 -> '4×', 0.25 -> '1/4×'."""
    if r >= 1:
        return f"{r:.1f}".rstrip("0").rstrip(".") + "×"
    return "1/" + f"{1 / r:.1f}".rstrip("0").rstrip(".") + "×"


def _levels(keys: list[Key]) -> str:
    counts: dict[str, int] = {}
    for k in keys:
        if k[1] in ("F", "E", "W"):
            counts[k[1]] = counts.get(k[1], 0) + 1
    parts = [f"{lvl} {counts[lvl]:,}" for lvl in ("F", "E", "W") if lvl in counts]
    return f" ({', '.join(parts)})" if parts else ""


def _describe(f: Filter, seg: int | None) -> str:
    parts: list[str] = []
    if f.sources:
        parts.append(("source " if len(f.sources) == 1 else "sources ") + ", ".join(f.sources))
    else:
        parts.append("all sources")
    if seg is not None:
        parts.append(f"segment {seg}")
    if f.from_:
        parts.append(f"from {f.from_}")
    if f.to:
        parts.append(f"to {f.to}")
    if f.level:
        parts.append(f"level ≥ {f.level}")
    extra: dict[str, Any] = {"tags": f.tags, "not_tags": f.not_tags, "pid": f.pid, "attrs": f.attrs}
    parts.extend(f"{k} {v}" for k, v in extra.items() if v)
    return " ".join(parts)
