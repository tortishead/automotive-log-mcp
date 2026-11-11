"""``check_citations``: every citation in an answer must be well-formed and resolve."""

from __future__ import annotations

from typing import Any

from ..core import citations as cit
from ..core import format as fmt
from ..model import SEQ_BITS
from ..store import query
from ..store.query import Rec
from .base import CaseCtx, ToolBase
from .models import CheckCitationsResult, CitationIssue, Output
from .text import id_ranges, join_lines, reserve_bytes
from .timespec import parse_log_cite


class CitationTools(ToolBase):
    """``check_citations``."""

    def check_citations(self, case: str, text: str) -> Output[CheckCitationsResult]:
        paras = cit.paragraphs(text)
        found = [f for p in paras for f in p.cites]
        errors: list[CitationIssue] = []
        warns: list[CitationIssue] = []
        with self.open_case(case) as ctx:
            status: dict[str, str | None] = {}
            targets: dict[str, Any] = {}  # resolved citation -> record or (artifact, citation)
            log_wanted: dict[str, int] = {}
            for f in found:
                if not f.valid or f.text in status:
                    continue
                if f.kind == "log":
                    sid, seq = parse_log_cite(f.text)
                    if sid not in ctx.by_id:
                        status[f.text] = (
                            f"no source {sid} in case '{case}' "
                            f"(sources {id_ranges(list(ctx.by_id)) or 'none'})"
                        )
                    else:
                        status[f.text] = None
                        log_wanted[f.text] = (sid << SEQ_BITS) | seq
                else:
                    status[f.text] = self._check_art(ctx, f.text, targets)
            recs = query.records_by_id(ctx.conn, list(log_wanted.values()))
            for c, rid in log_wanted.items():
                r = recs.get(rid)
                if r is None:
                    s = ctx.by_id[rid >> SEQ_BITS]
                    status[c] = f"source {s.id} has {fmt.plural(s.records, 'record', 'records')}"
                else:
                    targets[c] = r
            for f in found:
                if not f.valid:
                    errors.append(CitationIssue(line=f.line, cite=f.text, message=f"malformed: {f.problem}"))
                elif status.get(f.text):
                    errors.append(
                        CitationIssue(line=f.line, cite=f.text, message=f"does not resolve: {status[f.text]}")
                    )
            uncited = 0
            redact = self.app.redactor.redact
            for p in paras:
                if not p.cites:
                    if p.words >= cit.MIN_WORDS and not p.hedged() and not p.text.rstrip().endswith(":"):
                        uncited += 1
                        warns.append(
                            CitationIssue(
                                line=p.first_line,
                                message=f'no citation in: "{fmt.clip(p.text, 90)[0]}" '
                                "(cite the line it comes from, "
                                "or say it is a guess)",
                            )
                        )
                    continue
                good = [f.text for f in p.cites if f.valid and not status.get(f.text)]
                if not good:
                    continue
                hay = [cit.normalize(self._cited_text(ctx, targets[c])) for c in dict.fromkeys(good)]
                for qt in p.quotes():
                    needle = cit.normalize(redact(qt))
                    if needle and not any(needle in h for h in hay):
                        warns.append(
                            CitationIssue(
                                line=p.first_line,
                                cite=", ".join(dict.fromkeys(good)),
                                message=f'quote "{fmt.clip(qt, 70)[0]}" is not in the cited lines',
                            )
                        )
            distinct = len({f.text for f in found})
            resolved = sum(1 for c, s in status.items() if s is None)
            ok = not errors
            data = CheckCitationsResult(
                case=case,
                ok=ok,
                citations=len(found),
                distinct=distinct,
                resolved=resolved,
                paragraphs=len(paras),
                uncited=uncited,
                errors=errors,
                warnings=warns,
            )
            head = (
                f"{'PASS' if ok else 'FAIL'}: {fmt.plural(len(errors), 'error', 'errors')}, "
                f"{fmt.plural(len(warns), 'warning', 'warnings')}; "
                f"{fmt.plural(len(found), 'citation', 'citations')} "
                f"({distinct} distinct, {resolved} resolve); "
                f"{fmt.plural(len(paras), 'paragraph', 'paragraphs')}, "
                f"{uncited} without a citation"
            )
            if not found:
                head += (
                    ". The text cites nothing: every finding needs log:<source>#<seq> or "
                    "art:<id>[/section][:L<n>]"
                )
            page = fmt.Page(
                self.caps.max_lines, self.caps.max_bytes, reserve=reserve_bytes(self.caps.max_bytes, 400)
            )
            for e in errors:
                if not page.add(f"error L{e.line}: {e.cite} {e.message}"):
                    break
            for w in warns:
                if not page.add(f"warning L{w.line}: " + (f"{w.cite}: " if w.cite else "") + w.message):
                    break
            tail = []
            if page.full:
                tail.append(f"… more issues not shown ({fmt.cap_note(page)}); fix these and check again")
            if not ok:
                tail.append(
                    "Fix the citations (copy them exactly as the tools print them) and run "
                    "check_citations again."
                )
            elif warns:
                tail.append(
                    "All citations resolve. Add citations to the paragraphs named above or mark "
                    "them as guesses."
                )
            else:
                tail.append("All citations resolve.")
            return Output(join_lines([head, *page.lines, *tail]), data)

    def _check_art(self, ctx: CaseCtx, cite: str, targets: dict[str, Any]) -> str | None:
        c = query.parse_art_citation(cite)
        a = query.artifact(ctx.conn, c.id)
        if a is None or a.source_id not in ctx.by_id:
            return f"no artifact art:{c.id} in case '{ctx.case}' (see list_artifacts)"
        try:
            if c.section is not None or c.first is not None:
                query.artifact_lines(ctx.conn, a, c)
        except ValueError as e:
            return str(e)
        targets[cite] = (a, c)
        return None

    def _cited_text(self, ctx: CaseCtx, target: Any) -> str:
        if isinstance(target, Rec):
            return f"{target.tag or ''} {target.msg}"
        a, c = target
        lines = query.artifact_lines(ctx.conn, a, c)
        return "\n".join([a.headline, a.summary, *(t for _, t in lines)])
