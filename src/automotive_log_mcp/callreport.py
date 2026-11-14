"""Analyse agent sessions from the call log.

The round reports use this to say, for a run that failed or used many tokens, where
the tokens went and what to change: calls in order with bytes and estimated tokens, the fixed
cost per model request, repeated calls, capped outputs, output never cited in the answer, errors,
and suggestions. The call log holds sizes, hashes and citation ids, never log content (D5).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .usage import BYTES_PER_TOKEN, CiteSet, args_hashes, est_tokens


def _dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


TOOLS = (
    "add_source",
    "list_sources",
    "search",
    "context",
    "list_artifacts",
    "read_artifact",
    "get_records",
    "overview",
    "count",
    "check_citations",
)
NO_CITE_TOOLS = frozenset({"add_source", "list_sources", "check_citations"})
BIG_CALL_TOKENS = 1500  # a single result above this is worth a look
MANY_CALLS = 12  # a simple question should need far fewer
MAX_ROWS = 40


# -- call log -------------------------------------------------------------------------------------------


@dataclass
class Call:
    seq: int
    ts: str
    tool: str
    args: dict[str, Any]
    status: str
    error: str | None
    ms: float
    text_bytes: int
    structured_bytes: int | None  # None: logged before these fields existed
    capped: list[str]
    results: int | None
    total: int | None
    cites: CiteSet
    answer_cites: CiteSet | None
    ok: bool | None
    args_hash: str
    near_hash: str

    def model_bytes(self) -> int:
        """Bytes the model reads: Claude Code sends only the structured content when present."""
        return self.structured_bytes or self.text_bytes

    def summary_args(self) -> str:
        parts = []
        for k, v in self.args.items():
            if k == "case" or v is None or v == {} or v == []:
                continue
            if k == "path" and isinstance(v, str):
                v = re.split(r"[\\/]", v)[-1]
            text = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
            parts.append(f"{k}={text}")
        out = " ".join(parts)
        return out if len(out) <= 34 else out[:33] + "…"


@dataclass
class Session:
    id: str
    calls: list[Call] = field(default_factory=list)
    server: dict[str, Any] | None = None

    @property
    def first(self) -> str:
        return self.calls[0].ts if self.calls else ""

    @property
    def last(self) -> str:
        return self.calls[-1].ts if self.calls else ""

    @property
    def cases(self) -> list[str]:
        return sorted({c.args["case"] for c in self.calls if isinstance(c.args.get("case"), str)})

    def fixed_bytes(self) -> int | None:
        fixed = (self.server or {}).get("fixed") or {}
        if "instructions_bytes" not in fixed:
            return None
        return int(fixed["instructions_bytes"]) + int(fixed.get("tools_model_bytes", 0))

    def last_check(self) -> Call | None:
        checks = [c for c in self.calls if c.tool == "check_citations" and c.status == "ok"]
        return checks[-1] if checks else None


def _call(e: dict[str, Any], n: int) -> Call:
    args = _dict(e.get("args"))
    tool = str(e.get("tool"))
    h, near = (e.get("args_hash"), e.get("near_hash"))
    if not h or not near:
        h, near = args_hashes(tool, args)
    ac = e.get("answer_cites")
    sb = e.get("structured_bytes")
    return Call(
        seq=n,  # position in the round (the logged seq counts over the whole server session)
        ts=str(e.get("ts", "")),
        tool=tool,
        args=args,
        status=str(e.get("status", "?")),
        error=e.get("error"),
        ms=float(e.get("duration_ms") or 0),
        text_bytes=int(e.get("text_bytes", e.get("bytes_out")) or 0),
        structured_bytes=int(sb) if isinstance(sb, int) else None,
        capped=list(e.get("capped") or []),
        results=e.get("results") if isinstance(e.get("results"), int) else None,
        total=e.get("total") if isinstance(e.get("total"), int) else None,
        cites=CiteSet.parse(e.get("cites") or []),
        answer_cites=CiteSet.parse(ac) if isinstance(ac, list) else None,
        ok=e.get("ok") if isinstance(e.get("ok"), bool) else None,
        args_hash=str(h),
        near_hash=str(near),
    )


def read_calllog(path: Path) -> list[Session]:
    """Sessions in file order (old lines without the newer fields are read too)."""
    sessions: dict[str, Session] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(e, dict) or "tool" not in e:
                continue
            sid = str(e.get("session", "?"))
            if isinstance(e.get("round"), int):
                sid = f"{sid}-r{e['round']}"  # one report per round
            s = sessions.setdefault(sid, Session(sid))
            if isinstance(e.get("server"), dict):
                s.server = e["server"]
            s.calls.append(_call(e, len(s.calls) + 1))
    return list(sessions.values())


# -- the report -----------------------------------------------------------------------------------------


def _kb(n: int) -> str:
    return f"{n / 1024:.1f} KB" if n >= 1024 else f"{n} B"


def _tok(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


@dataclass
class Report:
    lines: list[str]
    problems: list[str]
    suggestions: list[str]

    def text(self) -> str:
        out = list(self.lines)
        if self.problems:
            out.append("problems:")
            out.extend(f"- {p}" for p in self.problems)
        if self.suggestions:
            out.append("suggestions:")
            out.extend(f"- {p}" for p in self.suggestions)
        return "\n".join(out)


def analyse(s: Session) -> Report:
    calls = s.calls
    lines: list[str] = []
    problems: list[str] = []
    tips: list[str] = []

    structured = (s.server or {}).get("structured")
    head = f"session {s.id}  {s.first[:19].replace('T', ' ')} .. {s.last[11:19]} UTC"
    head += f"  case {', '.join(s.cases) or '-'}  {len(calls)} calls"
    if s.server:
        head += f"  server {s.server.get('version', '?')}, structured {'on' if structured else 'off'}"
    lines.append(head)

    fixed_b = s.fixed_bytes()
    if fixed_b is not None:
        fixed = (s.server or {}).get("fixed") or {}
        lines.append(
            f"fixed per model request: ~{_tok(est_tokens(fixed_b))} tokens "
            f"(instructions {_kb(int(fixed.get('instructions_bytes', 0)))}, "
            f"{len(fixed.get('per_tool') or {})} tool definitions "
            f"{_kb(int(fixed.get('tools_model_bytes', 0)))}; bytes/{BYTES_PER_TOKEN}, Claude Code "
            "counted about 2x this)"
        )

    # repeated and near-duplicate calls
    seen_exact: dict[str, int] = {}
    seen_near: dict[str, int] = {}
    flags: dict[int, list[str]] = {}
    for c in calls:
        if c.args_hash in seen_exact:
            flags.setdefault(c.seq, []).append(f"repeat of #{seen_exact[c.args_hash]}")
        elif c.near_hash in seen_near:
            flags.setdefault(c.seq, []).append(f"like #{seen_near[c.near_hash]}")
        seen_exact.setdefault(c.args_hash, c.seq)
        seen_near.setdefault(c.near_hash, c.seq)
        if c.capped:
            flags.setdefault(c.seq, []).append("capped " + "+".join(c.capped))
        if c.status != "ok":
            flags.setdefault(c.seq, []).append(c.status)

    # what the answer cites
    check = s.last_check()
    answer = check.answer_cites if check else None
    uncited: list[Call] = []
    if answer:
        for c in calls:
            if c.status == "ok" and c.tool not in NO_CITE_TOOLS and c.cites and not c.cites.overlaps(answer):
                uncited.append(c)
                flags.setdefault(c.seq, []).append("not cited")

    lines.append(
        f"{'#':>3} {'tool':<15} {'args':<34} {'text':>6} {'json':>6} {'~tok':>5} {'items':>5}  flags"
    )
    for c in calls[:MAX_ROWS]:
        sb = "-" if not c.structured_bytes else f"{c.structured_bytes:,}"
        if c.structured_bytes is None:
            sb = "?"
        items = "" if c.results is None else str(c.results)
        lines.append(
            f"{c.seq:>3} {c.tool:<15} {c.summary_args():<34} {c.text_bytes:>6,} {sb:>6} "
            f"{est_tokens(c.model_bytes()):>5,} {items:>5}  {', '.join(flags.get(c.seq, []))}"
        )
    if len(calls) > MAX_ROWS:
        lines.append(f"  … {len(calls) - MAX_ROWS} more calls (totals below include them)")

    out_tok = sum(est_tokens(c.model_bytes()) for c in calls)
    text_tok = sum(est_tokens(c.text_bytes) for c in calls)
    how = "structured content where sent" if any(c.structured_bytes for c in calls) else "text results"
    lines.append(
        f"tool output: ~{_tok(out_tok)} tokens est. (~tok: bytes/{BYTES_PER_TOKEN}, {how}); "
        f"as text only ~{_tok(text_tok)}"
    )

    def cost(c: Call) -> int:
        return est_tokens(c.model_bytes())

    # -- problems ---------------------------------------------------------------------------------------
    errors = [c for c in calls if c.status == "error"]
    if errors:
        problems.append(
            f"{len(errors)} tool errors: "
            + "; ".join(f"#{c.seq} {c.tool}: {(c.error or '')[:90]}" for c in errors[:3])
        )
    refused = [c for c in calls if c.status == "refused"]
    if refused:
        problems.append(
            f"{len(refused)} calls refused: the policy byte_budget was used up at #{refused[0].seq}"
        )
    if check is None:
        problems.append("the answer was not checked with check_citations")
    elif check.ok is False:
        problems.append(
            f"the last check_citations (#{check.seq}) reported errors: the answer has bad citations"
        )
    repeats = [c for c in calls if any(f.startswith("repeat") for f in flags.get(c.seq, []))]
    if repeats:
        wasted = sum(cost(c) for c in repeats)
        problems.append(f"{len(repeats)} exact repeats of earlier calls (~{_tok(wasted)} tokens)")

    # -- suggestions -----------------------------------------------------------------------------------
    struct_extra = sum((c.structured_bytes or 0) for c in calls)
    if structured or struct_extra:
        saved = sum(est_tokens(c.model_bytes()) - est_tokens(c.text_bytes) for c in calls)
        tips.append(
            "structured content is on: the model may read the JSON instead of (or with) the text; "
            f"text only (config structured_output: false, the default now) "
            f"saves ~{_tok(max(saved, 0))} tokens (bytes/{BYTES_PER_TOKEN}) here"
        )
    if len(calls) >= MANY_CALLS:
        top = Counter(c.tool for c in calls).most_common(3)
        tips.append(
            f"{len(calls)} calls: every call re-sends the whole conversation; for a simple question "
            "list_artifacts → read_artifact(section) → context is usually enough "
            f"(most calls here: {', '.join(f'{t} {n}x' for t, n in top)})"
        )
    capped = [c for c in calls if c.capped]
    if capped:
        tips.append(
            f"{len(capped)} capped outputs (#{', #'.join(str(c.seq) for c in capped[:6])}): narrow with "
            "filter level/from/to/tags/pid, or count(...) first, instead of paging"
        )
    pages = [c for c in calls if c.args.get("cursor")]
    if len(pages) >= 2:
        tips.append(f"{len(pages)} calls paged with a cursor: narrow the query instead")
    if uncited:
        tok = sum(cost(c) for c in uncited)
        top = Counter(c.tool for c in uncited).most_common(3)
        tips.append(
            f"{len(uncited)} calls (~{_tok(tok)} tokens) gave output the answer never cites "
            f"({', '.join(f'{t} {n}x' for t, n in top)}): candidates to skip or narrow"
        )
    big = sorted((c for c in calls if cost(c) > BIG_CALL_TOKENS), key=lambda c: -cost(c))
    if big:
        tips.append(
            "largest results: "
            + ", ".join(f"#{c.seq} {c.tool} {_tok(cost(c))}" for c in big[:4])
            + ": read an artifact's summary or one section, not all lines; smaller before/after for "
            "context; a narrower filter or limit for search; overview only for broad questions"
        )
    return Report(lines, problems, tips)
