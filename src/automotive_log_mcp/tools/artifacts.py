"""``list_artifacts`` and ``read_artifact``: crash documents grouped by signature."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from ..core import format as fmt
from ..store import query
from ..store.query import ArtifactRow
from . import limits
from .base import ToolBase
from .models import (
    ArtifactGroupOut,
    ArtifactOut,
    LineOut,
    ListArtifactsResult,
    Output,
    ReadArtifactResult,
    SectionOut,
    ToolInputError,
)
from .text import dash, join_lines, reserve_bytes


@dataclass
class GroupedArtifact:
    row: ArtifactRow
    same_as: str | None = None


@dataclass
class ArtifactGroup:
    type: str
    signature: str | None
    items: list[GroupedArtifact]


def group_key(a: ArtifactRow) -> tuple[str, str]:
    """Artifacts of the same crash share type, kind, process and top frame.

    The full signature has up to three frames; a crash-buffer copy of a Java crash often has
    fewer frames than its dropbox copy, so grouping uses the signature up to the first frame."""
    if not a.signature:
        return (a.type, f"#{a.id}")
    return (a.type, a.signature.split(" < ", 1)[0])


def group_artifacts(rows: list[ArtifactRow]) -> list[ArtifactGroup]:
    """Groups in order of their first artifact's time; within a group in time order. An artifact
    with the same pid within ``limits.SAME_CRASH_NS`` of an earlier one in its group is marked as a copy."""
    groups: dict[tuple[str, str], ArtifactGroup] = {}
    for a in rows:  # rows come in time order
        key = group_key(a)
        g = groups.get(key)
        if g is None:
            g = groups[key] = ArtifactGroup(a.type, a.signature, [])
        same = next((x for x in g.items if not x.same_as and same_event(x.row, a)), None)
        g.items.append(GroupedArtifact(a, same.row.cite if same else None))
        if a.signature and (g.signature is None or len(a.signature) > len(g.signature)):
            g.signature = a.signature  # show the most detailed signature
    return list(groups.values())


def same_event(a: ArtifactRow, b: ArtifactRow) -> bool:
    return (
        a.pid is not None
        and a.pid == b.pid
        and a.ts is not None
        and b.ts is not None
        and abs(a.ts - b.ts) <= limits.SAME_CRASH_NS
    )


def same_crash(a: ArtifactRow, others: list[ArtifactRow]) -> list[ArtifactRow]:
    key = group_key(a)
    return [o for o in others if o.id != a.id and group_key(o) == key and same_event(a, o)]


def origin(a: ArtifactRow) -> str:
    name = a.name.rsplit("!", 1)[-1]
    return name.rsplit("/", 1)[-1][:60]


def main_section(a: ArtifactRow) -> str | None:
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


class ArtifactTools(ToolBase):
    """``list_artifacts`` and ``read_artifact``."""

    def list_artifacts(self, case: str, type: str | None = None) -> Output[ListArtifactsResult]:
        with self.open_case(case) as ctx:
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
                self.caps.max_lines, self.caps.max_bytes, reserve=reserve_bytes(self.caps.max_bytes, 500)
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
                        f"same crash as {a.same_as} ({origin(a.row)})"
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
            return Output(join_lines(lines), data)

    @staticmethod
    def _art_out(a: GroupedArtifact) -> ArtifactOut:
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
    ) -> Output[ReadArtifactResult]:
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
        with self.open_case(case) as ctx:
            a = query.artifact(ctx.conn, c.id)
            if a is None or a.source_id not in ctx.by_id:
                raise ToolInputError(f"no artifact art:{c.id} in case '{case}' (see list_artifacts)")
            near = (
                query.artifacts_near(ctx.conn, a, limits.SAME_CRASH_NS, [s.id for s in ctx.sources])
                if a.pid is not None and a.ts is not None
                else []
            )
            same = same_crash(a, near)
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
            page = fmt.Page(cap, self.caps.max_bytes, reserve=reserve_bytes(self.caps.max_bytes, 500))
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
            return Output(join_lines(out), data)

    def _artifact_summary(self, a: ArtifactRow, data: ReadArtifactResult, same: list[ArtifactRow]) -> str:
        when = fmt.full_ts(a.ts) or "unknown"
        lines = [f"{a.cite}  {a.type}  record {a.record_cite}  ({a.lines} lines)"]
        lines.append(f"name: {a.name}")
        lines.append(
            f"time: {when}" + (f"  (as written: {a.ts_raw})" if a.ts_raw and a.ts_raw != when else "")
        )
        if a.process or a.pid is not None:
            lines.append(f"process: {a.process or '-'}  pid {dash(a.pid)}  tid {dash(a.tid)}")
        if a.signature:
            lines.append(f"signature: {a.signature}")
        if same:
            copies = ", ".join(f"{o.cite} ({o.lines} lines, {origin(o)})" for o in same)
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
        best = main_section(a)
        if best:
            lines.append(f"next: read_artifact(id='{a.cite}/{best}')")
        return join_lines(lines)
