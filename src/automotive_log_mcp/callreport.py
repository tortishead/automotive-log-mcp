"""Analyse agent sessions from the call log, optionally joined with a Claude Code transcript.

``automotive-log-mcp calls`` uses this to say, for a run that failed or used many tokens, where
the tokens went and what to change: calls in order with bytes and estimated tokens, the fixed
cost per model request, repeated calls, capped outputs, output never cited in the answer, errors,
and suggestions. The call log holds sizes, hashes and citation ids, never log content (D5); the
transcript, if given, is read locally and nothing from it is written anywhere.

Transcripts: a Claude Code session file (``~/.claude/projects/<project>/<session>.jsonl``) or the
output of ``claude -p --output-format stream-json --verbose``. Both have one JSON object per line
with ``type`` ``assistant`` (a model response with ``message.usage``; one response can span
several lines with the same ``message.id``) and ``user`` (tool results).
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
    "detect",
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
    seen_bytes: int | None = None  # bytes the client passed to the model (from a transcript)
    measured: int | None = None  # tokens the result added to the prompt (from a transcript)
    request: int | None = None  # index of the model request that made the call (from a transcript)

    def model_bytes(self, reads: str) -> int:
        """Bytes the model reads: as seen in the transcript, else per ``reads``."""
        if self.seen_bytes is not None:
            return self.seen_bytes
        s = self.structured_bytes or 0
        if reads == "text" or not s:
            return self.text_bytes
        if reads == "both":
            return self.text_bytes + s
        return s  # 'structured': what Claude Code sends when structured content is present

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


# -- transcript ------------------------------------------------------------------------------------------


@dataclass
class ToolUse:
    id: str
    name: str  # as the client named it, e.g. mcp__automotive-log__search
    input: dict[str, Any]
    request: int  # index of the model request that made the call
    result_bytes: int | None = None
    is_error: bool = False
    error: str | None = None
    measured: int | None = None  # share of the prompt growth after its request, by bytes

    @property
    def rejected(self) -> bool:
        """The client did not run the call (unknown tool name, permission denied, bad arguments)."""
        return self.is_error and bool(re.search(r"No such tool|tool_use_error|permission", self.error or ""))

    @property
    def meant(self) -> str | None:
        """This server's tool name the call was meant for (with or without the mcp__ prefix)."""
        base = self.name.rsplit("__", 1)[-1]
        return base if base in TOOLS else None

    @property
    def tool(self) -> str | None:
        """This server's tool, for a call the client actually sent to it."""
        return None if self.rejected else self.meant


@dataclass
class Request:
    id: str
    input: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0

    @property
    def prompt(self) -> int:
        return self.input + self.cache_write + self.cache_read


@dataclass
class Transcript:
    path: Path
    requests: list[Request] = field(default_factory=list)
    uses: list[ToolUse] = field(default_factory=list)
    answer: str = ""
    result: dict[str, Any] | None = None  # stream-json 'result' event
    model: str | None = None


def _content_bytes(content: Any) -> int:
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    if isinstance(content, list):
        total = 0
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                total += len(str(c.get("text", "")).encode("utf-8"))
            else:
                total += len(json.dumps(c, ensure_ascii=False).encode("utf-8"))
        return total
    return 0


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return ""


def read_transcript(path: Path) -> Transcript:
    t = Transcript(path)
    by_id: dict[str, ToolUse] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(e, dict) or e.get("isSidechain"):
                continue
            kind = e.get("type")
            msg = _dict(e.get("message"))
            if kind == "assistant":
                mid = str(msg.get("id") or len(t.requests))
                if not t.requests or t.requests[-1].id != mid:
                    t.requests.append(Request(mid))
                    t.model = t.model or msg.get("model")
                # One response spans several lines; usage grows while it streams: keep the largest.
                u, r = _dict(msg.get("usage")), t.requests[-1]
                r.input = max(r.input, int(u.get("input_tokens") or 0))
                r.cache_write = max(r.cache_write, int(u.get("cache_creation_input_tokens") or 0))
                r.cache_read = max(r.cache_read, int(u.get("cache_read_input_tokens") or 0))
                r.output = max(r.output, int(u.get("output_tokens") or 0))
                texts = []
                for c in msg.get("content") or []:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "tool_use":
                        use = ToolUse(
                            str(c.get("id")),
                            str(c.get("name")),
                            _dict(c.get("input")),
                            len(t.requests) - 1,
                        )
                        t.uses.append(use)
                        by_id[use.id] = use
                    elif c.get("type") == "text" and c.get("text"):
                        texts.append(str(c["text"]))
                if texts:
                    t.answer = "\n".join(texts)
            elif kind == "user":
                content = msg.get("content")
                for c in content if isinstance(content, list) else []:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        res = by_id.get(str(c.get("tool_use_id")))
                        if res is None:
                            continue
                        res.result_bytes = _content_bytes(c.get("content"))
                        res.is_error = bool(c.get("is_error"))
                        if res.is_error:
                            res.error = _content_text(c.get("content"))[:200]
            elif kind == "result":
                t.result = e
                if isinstance(e.get("result"), str) and e["result"]:
                    t.answer = e["result"]
    _measure(t)
    return t


def _measure(t: Transcript) -> None:
    """Real tokens per tool result: the next request's prompt minus this one's prompt and output is
    what the tool calls and results added; split it over the calls by result bytes."""
    for i, r in enumerate(t.requests[:-1]):
        uses = [u for u in t.uses if u.request == i]
        added = t.requests[i + 1].prompt - r.prompt - r.output
        total = sum(u.result_bytes or 0 for u in uses)
        if not uses or added <= 0 or r.prompt == 0:
            continue
        for u in uses:
            share = (u.result_bytes or 0) / total if total else 1 / len(uses)
            u.measured = round(added * share)


def _same_call(use: ToolUse, call: Call) -> bool:
    return use.tool == call.tool and use.input.get("case", call.args.get("case")) == call.args.get("case")


def match_score(t: Transcript, s: Session) -> int:
    """How many of the transcript's calls to this server line up with the session's calls."""
    i, n = 0, 0
    for use in t.uses:
        if use.tool is None:
            continue
        for j in range(i, len(s.calls)):
            if _same_call(use, s.calls[j]):
                n, i = n + 1, j + 1
                break
    return n


def join(t: Transcript, s: Session) -> list[ToolUse]:
    """Set ``seen_bytes`` on calls the transcript shows; return this server's tool uses that have
    no call-log line (rejected by the client or by argument validation before the tool ran)."""
    i = 0
    unlogged = []
    for use in t.uses:
        if use.meant is None:
            continue
        hit = None
        if use.tool is not None:
            for j in range(i, len(s.calls)):
                if _same_call(use, s.calls[j]):
                    hit = j
                    break
        if hit is None:
            unlogged.append(use)
            continue
        s.calls[hit].seen_bytes = use.result_bytes
        s.calls[hit].measured = use.measured
        s.calls[hit].request = use.request
        i = hit + 1
    return unlogged


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


def analyse(
    s: Session,
    transcript: Transcript | None = None,
    reads: str = "structured",
    fixed_now: dict[str, int] | None = None,
) -> Report:
    """``fixed_now``: per-tool model bytes of the current server, for 'hide unused tools' advice
    when the session's log has no ``server`` line."""
    unlogged = join(transcript, s) if transcript else []
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
    answer: CiteSet | None = None
    check = s.last_check()
    if transcript and transcript.answer:
        answer = CiteSet.from_text(transcript.answer)
    elif check and check.answer_cites is not None:
        answer = check.answer_cites
    uncited: list[Call] = []
    if answer:
        for c in calls:
            if c.status == "ok" and c.tool not in NO_CITE_TOOLS and c.cites and not c.cites.overlaps(answer):
                uncited.append(c)
                flags.setdefault(c.seq, []).append("not cited")

    lines.append(
        f"{'#':>3} {'tool':<15} {'args':<34} {'text':>6} {'json':>6} {'~tok':>5} {'tok':>5} "
        f"{'items':>5}  flags"
    )
    for c in calls[:MAX_ROWS]:
        sb = "-" if not c.structured_bytes else f"{c.structured_bytes:,}"
        if c.structured_bytes is None:
            sb = "?"
        items = "" if c.results is None else str(c.results)
        meas = "-" if c.measured is None else f"{c.measured:,}"
        lines.append(
            f"{c.seq:>3} {c.tool:<15} {c.summary_args():<34} {c.text_bytes:>6,} {sb:>6} "
            f"{est_tokens(c.model_bytes(reads)):>5,} {meas:>5} {items:>5}  {', '.join(flags.get(c.seq, []))}"
        )
    if len(calls) > MAX_ROWS:
        lines.append(f"  … {len(calls) - MAX_ROWS} more calls (totals below include them)")

    out_tok = sum(est_tokens(c.model_bytes(reads)) for c in calls)
    text_tok = sum(est_tokens(c.text_bytes) for c in calls)
    if any(c.seen_bytes is not None for c in calls):
        how = "as the client passed them"
    elif any(c.structured_bytes for c in calls):
        how = f"reads={reads}"
    else:
        how = "text results"
    lines.append(
        f"tool output: ~{_tok(out_tok)} tokens est. (~tok: bytes/{BYTES_PER_TOKEN}, {how}); "
        f"as text only ~{_tok(text_tok)}"
    )
    measured = [c.measured for c in calls if c.measured is not None]
    if measured:
        lines.append(
            f"tool output measured (tok: prompt growth in the transcript): {_tok(sum(measured))} tokens; "
            "every result stays in the context and is re-read by each later request"
        )

    def cost(c: Call) -> int:
        """Tokens of a result: measured if the transcript has it, else the estimate."""
        return c.measured if c.measured is not None else est_tokens(c.model_bytes(reads))

    if transcript and transcript.requests:
        rq = transcript.requests
        prompt = sum(r.prompt for r in rq)
        cached = sum(r.cache_read for r in rq)
        output = sum(r.output for r in rq)
        lines.append(
            f"model (transcript): {len(rq)} requests, input {_tok(prompt)} tokens "
            f"({_tok(cached)} from cache), output {_tok(output)}; "
            f"first request {_tok(rq[0].prompt)}, largest {_tok(max(r.prompt for r in rq))}"
        )
        # A result stays in the context: every later request sends it again.
        carried = [
            (c.measured * (len(rq) - 1 - c.request), c)
            for c in calls
            if c.measured and c.request is not None and c.request < len(rq) - 1
        ]
        carried.sort(key=lambda x: -x[0])
        base = rq[0].prompt * len(rq)
        lines.append(
            f"first request re-sent: {len(rq)} x {_tok(rq[0].prompt)} = {_tok(base)} "
            f"({100 * base // max(prompt, 1)}% of input; client prompt, server definitions, question)"
        )
        if len(rq) >= 5:
            tips.append(
                f"{len(rq)} model requests each re-send at least the first request's {_tok(rq[0].prompt)} "
                "tokens: ask for independent calls in parallel (one request), stop when the evidence "
                "answers the question"
            )
        if carried:
            total = sum(n for n, _ in carried)
            lines.append(
                f"tool results re-sent with later requests: {_tok(total)} input tokens; most: "
                + ", ".join(f"#{c.seq} {c.tool} {_tok(n)}" for n, c in carried[:3])
            )
        res = transcript.result or {}
        if res:
            usd = res.get("total_cost_usd")
            lines.append(
                f"result: {res.get('subtype', '?')}, {res.get('num_turns', '?')} turns"
                + (f", ${usd:.3f} (client estimate)" if isinstance(usd, int | float) else "")
            )
            if res.get("is_error") or str(res.get("subtype", "")).startswith("error"):
                problems.append(f"the run ended with {res.get('subtype')}: the agent did not finish")
        if fixed_b is not None:
            share = len(rq) * est_tokens(fixed_b)
            est_out = sum(est_tokens(c.model_bytes(reads)) for c in calls if c.measured is not None)
            meas_out = sum(c.measured or 0 for c in calls)
            ratio = meas_out / est_out if est_out and meas_out else None
            real = (
                f"; ~{_tok(round(share * ratio))} with this run's measured ratio ({ratio:.1f}x)"
                if ratio
                else ""
            )
            lines.append(
                f"fixed cost: {len(rq)} requests x ~{_tok(est_tokens(fixed_b))} = ~{_tok(share)} of "
                f"{_tok(prompt)} input tokens are the server's definitions (bytes/{BYTES_PER_TOKEN}{real})"
            )

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
    rejected = Counter((u.name, (u.error or "no result").replace("\n", " ")[:110]) for u in unlogged)
    for (name, why), n in list(rejected.items())[:3]:
        problems.append(f"{n} call{'s' if n > 1 else ''} to {name} never reached the tool: {why}")
    if len(rejected) > 3:
        problems.append(f"… {len(rejected) - 3} more kinds of calls rejected before the tool ran")
    if any("No such tool" in (u.error or "") for u in unlogged):
        tips.append(
            "the model called tools by a name the client does not know (e.g. without the "
            "mcp__<server>__ prefix); each such call costs a model turn"
        )
    if check is None:
        problems.append("the answer was not checked with check_citations")
    elif check.ok is False:
        problems.append(
            f"the last check_citations (#{check.seq}) reported errors: the answer has bad citations"
        )
    if transcript and transcript.answer and not answer:
        problems.append("the final answer cites nothing (log:<source>#<seq> / art:<id>)")
    repeats = [c for c in calls if any(f.startswith("repeat") for f in flags.get(c.seq, []))]
    if repeats:
        wasted = sum(cost(c) for c in repeats)
        problems.append(f"{len(repeats)} exact repeats of earlier calls (~{_tok(wasted)} tokens)")

    # -- suggestions -----------------------------------------------------------------------------------
    struct_extra = sum((c.structured_bytes or 0) for c in calls)
    seen = [c for c in calls if c.seen_bytes is not None and c.status == "ok"]
    seen_more = sum(c.seen_bytes or 0 for c in seen) > 1.2 * sum(c.text_bytes for c in seen) + 200
    if structured or struct_extra or seen_more:
        saved = sum(est_tokens(c.model_bytes(reads)) - est_tokens(c.text_bytes) for c in calls)
        why = (
            "the client passed more bytes than the text (structured JSON)"
            if seen_more
            else "structured content is on: the model may read the JSON instead of (or with) the text"
        )
        tips.append(
            f"{why}; text only (config structured_output: false, the default now) "
            f"saves ~{_tok(max(saved, 0))} tokens (bytes/{BYTES_PER_TOKEN}) here"
        )
    per_tool = ((s.server or {}).get("fixed") or {}).get("per_tool") or fixed_now or {}
    used = {c.tool for c in calls}
    unused = {k: v for k, v in per_tool.items() if k not in used}
    if per_tool and unused and transcript and len(transcript.requests) > 3:
        b = sum(unused.values())
        tips.append(
            f"{len(unused)} tools were never called ({', '.join(sorted(unused))}): "
            f"policy allowed_tools without them saves ~{_tok(est_tokens(b))} tokens per request "
            f"(~{_tok(est_tokens(b) * len(transcript.requests))} in this run)"
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
            "context; a narrower filter or limit for search; detect/overview only for broad questions"
        )
    if any(c.seen_bytes is None for c in calls) and transcript:
        n = sum(c.seen_bytes is None for c in calls)
        problems.append(f"{n} call-log calls not found in the transcript (another session, or a subagent)")
    return Report(lines, problems, tips)


def session_line(s: Session, reads: str = "structured") -> str:
    tok = sum(est_tokens(c.model_bytes(reads)) for c in s.calls)
    err = sum(c.status != "ok" for c in s.calls)
    check = s.last_check()
    chk = "none" if check is None else ("pass" if check.ok else "FAIL" if check.ok is False else "?")
    return (
        f"{s.first[:16].replace('T', ' ')}  {s.id:<12}  {', '.join(s.cases) or '-':<16} "
        f"{len(s.calls):>4} calls  ~{_tok(tok):>6} tok  {err:>2} err  check {chk}"
    )
