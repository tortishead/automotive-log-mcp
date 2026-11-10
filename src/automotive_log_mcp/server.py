"""MCP tools, server instructions and prompts.

Every tool is registered with its full parameter schema, an output schema and annotations; the
tool logic is in ``tools.py``. Every call passes through the policy (allowed tools, byte budget)
and is written to the call log with redacted arguments (D5); the text given to
``check_citations`` is logged only as its length and hash (it quotes log content), plus the
citation ids it contains. Each call-log line also holds sizes, counts and hashes that explain
token use (see ``calllog.py``).

Structured content: off by default. Clients that get both a text and ``structuredContent`` may
forward only the JSON to the model (Claude Code 2.1 does), which is 1.5-2x the bytes of the text
and drops its footer hints ('capped: narrow with …'). ``structured_output: true`` in the config
(or ``serve --structured``) restores the output schemas and structured content.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import re
import threading
import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, TypeVar

from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import __version__
from .app import App
from .calllog import CallLog
from .clocktools import AlignResult, TimelineResult
from .config import CASE_NAME_RE, Config
from .core import format as fmt
from .core.ingest import AddResult, IngestError
from .difftool import DiffResult
from .policy import ByteBudget, Policy
from .store import query
from .store.db import StoreError
from .tools import (
    CheckCitationsResult,
    ContextResult,
    CountResult,
    DetectResult,
    DiffSide,
    Filter,
    GetRecordsResult,
    ListArtifactsResult,
    Output,
    OverviewResult,
    ReadArtifactResult,
    SearchResult,
    ToolInputError,
    Tools,
)
from .usage import FixedCost, args_hashes, cite_ids, compact_cites, est_tokens, fixed_cost, json_bytes

SERVER_NAME = "automotive-log-mcp"

INSTRUCTIONS = """\
Tools for analysing a defect from automotive logs (Android bugreport, logcat, kernel logs, other \
formats from connectors). The \
server parses, indexes and searches; reasoning is yours. Each call re-sends the whole \
conversation, so make few, narrow calls and stop once the evidence answers the question.

Simple questions (what crashed, and why): 3-4 calls.
1. add_source(case, path) if the case is new; its reply lists the sources (no list_sources needed).
2. list_artifacts(case): tombstones, ANRs, Java crashes, dropbox entries, grouped by signature in \
time order; 'same crash as' marks copies, which need no second read.
3. read_artifact(case, id='art:<id>') for the crash that fits: its summary has the signal or \
exception, cause and top of the stack; read a section only if the summary is not enough.
4. Answer, citing each finding; check_citations(case, text) before answering when asked to cite.

Broader questions: detect(case) for known failure patterns (crash, ANR, watchdog, low memory \
kill, SELinux, binder, VHAL, service restart, boot not completed); overview(case) for where \
errors cluster; context(case, at='art:<id>' or 'log:<source>#<seq>') for the lines before an \
event (scope='pid' follows one process across buffers); search/count with filter level 'W' or \
'E' and from/to; get_records for messages cut with '…'.

Several files or devices (bugreport plus kernel log, ECU logs): timeline(case, filter) merges all \
sources on one case clock and context(case, at=..., scope='all') shows every source around an \
event; '~' marks times aligned from another clock, align(case) shows how well (and sets anchors). \
Say when an order across sources rests on '~' times within their error. A good and a bad run, \
or two boots: diff(case, a={'sources': [...]}, b=...) lists what only one side has.

Cite findings exactly as printed: log:<source>#<seq> or art:<id>[/section][:L<n>]; a statement \
without a citation is a guess. Outputs are capped (about 50 lines / 8 KB): narrow with level, \
from/to, tags or pid instead of paging. Printed timestamps work as from/to, as do '-5m', '+30s' \
and 'log:3#120-30s'. Redacted values are stable tokens (<MAC:3fa2c1d0>); ≈ marks estimated counts.
"""

TRIAGE_PROMPT = """\
Triage the defect in case '{case}' with the automotive-log-mcp tools.

{symptom}Work in this order and stop as soon as the evidence is clear:
1. list_sources(case='{case}'). Note the build fingerprint and each source's time range. If the case \
has no sources, ask for the log files and add them with add_source.
2. overview(case='{case}') and detect(case='{case}'): where the errors are, and which known failure \
patterns (crash, ANR, watchdog, low memory kill, VHAL error, service restart, …) occur and when.
3. list_artifacts(case='{case}'). For each group whose time and process fit the symptom, \
read_artifact the first artifact, then the section it suggests: the backtrace of a tombstone, the \
exception of a Java crash, the main thread (and lock holder or binder peer) of an ANR.
4. context(case='{case}', at='art:<id>', before=30, after=10) for what happened just before the \
crash; scope='pid' to follow the crashing process across buffers.
5. search(case='{case}', query=..., filter={{'level': 'W', 'from': ..., 'to': ...}}) for errors \
and warnings in the minutes before the defect: start from the component or process in the \
symptom, then widen. Keep collapse on; repeated messages show ×count. count(..., bucket='1m') shows \
when a message starts or stops.
6. get_records for any message cut with '…' that matters.
7. Before you answer: check_citations(case='{case}', text=<your answer in Markdown>). Fix every \
error it reports (a citation that is malformed or does not resolve) and either cite or mark as a \
guess every paragraph it warns about; run it again until it passes. Only then give the answer.

Answer with:
- What failed (component, process) and when (timestamp as printed).
- The chain of evidence, oldest first, each step with its citation (log:<source>#<seq> or \
art:<id>/<section>:L<n>) exactly as the tools printed it.
- The constant text from 'code search' for the lines a developer should look up in the code.
- What the logs do not show, and what you are guessing (without a citation it is a guess).
"""

CASE_RE = CASE_NAME_RE
ADD_WAIT_S = 10.0  # add_source waits this long for an import, then reports it as running
LOG_CITATION_RE = r"^log:[0-9]+#[1-9][0-9]*$"
AT_RE = r"^(log:[0-9]+#[1-9][0-9]*|art:[0-9]+)$"
ART_CITATION_RE = r"^art:[0-9]+(/[a-z0-9_.-]+)?(:L[1-9][0-9]*(-L?[1-9][0-9]*)?)?$"

Level = Literal["V", "D", "I", "W", "E", "F"]

Case = Annotated[
    str,
    Field(
        pattern=CASE_RE,
        description="Case name, e.g. a ticket id.",
    ),
]


class SourceInfo(BaseModel):
    id: str = Field(description="Source id used in citations log:<id>#<seq>.")
    name: str = Field(description="file[!member][#section][:stream], e.g. a bugreport buffer.")
    format: str
    status: str = Field(description="indexing | done | failed")
    path: str = Field(description="Path of the original file; originals are never copied or modified.")
    records: int
    ts_from: str | None = Field(None, description="First timestamp as printed in outputs.")
    ts_to: str | None = Field(None, description="Last timestamp as printed in outputs.")
    fingerprint: str | None = Field(None, description="Android build fingerprint, if known.")
    original_missing: bool = Field(False, description="The original file is no longer at its path.")
    stale: bool = Field(False, description="Indexed with an older connector or redaction rules.")
    segments: int = Field(0, description="Stretches with one continuous clock (boots, clock jumps).")
    clock: str | None = Field(
        default=None, description="How the source maps onto the case clock (see align); null when exact."
    )
    error: str | None = None


class ListSourcesResult(BaseModel):
    case: str
    exists: bool = Field(description="Whether the case DB exists yet.")
    sources: list[SourceInfo]
    case_clock: str | None = Field(
        default=None, description="The clock timeline and context(scope='all') use."
    )


def _source_info(s: query.SourceRow) -> SourceInfo:
    t0, t1 = query.time_range(s)
    return SourceInfo(
        id=str(s.id),
        name=s.name,
        format=s.format,
        status=s.status,
        path=s.path,
        records=s.records,
        ts_from=t0,
        ts_to=t1,
        fingerprint=s.fingerprint,
        original_missing=s.original_missing,
        stale=s.stale,
        segments=s.segments,
        error=s.error,
    )


def _describe_add(result: AddResult, case: str) -> str:
    lines = []
    for f in result.files:
        ids = ", ".join(map(str, f.source_ids)) or "none"
        if f.other_case:
            lines.append(
                f"{f.path} is already indexed in case '{f.other_case}' (sources {ids}); nothing was "
                f"indexed. Use case='{f.other_case}' for this file; only if the user wants a separate "
                f"copy in '{case}', call add_source again with reindex=true."
            )
            continue
        if f.existing:
            lines.append(f"{f.path} is already in the case: sources {ids}.")
        else:
            lines.append(f"Indexed {f.path}: {f.records} records in sources {ids}.")
        lines.extend(f"  note: {n}" for n in f.notes[:10])
        if len(f.notes) > 10:
            lines.append(f"  … {len(f.notes) - 10} more notes")
    lines.extend(f"  skipped: {s}" for s in result.skipped[:10])
    lines.append("Next: list_sources(case) for formats and time ranges.")
    return "\n".join(lines)


# Filled by _output during a call, read by the guard: counts for the call log (never content).
_STATS: ContextVar[dict[str, Any] | None] = ContextVar("_STATS", default=None)
_ITEM_FIELDS = ("hits", "records", "groups", "results", "lines", "sources", "buckets", "errors")


def _result_stats(data: BaseModel) -> dict[str, Any]:
    stats: dict[str, Any] = {}
    for name in _ITEM_FIELDS:
        items = getattr(data, name, None)
        if isinstance(items, list):
            stats["results"] = len(items)
            break
    total = getattr(data, "total", None)
    if isinstance(total, int) and not isinstance(total, bool):
        stats["total"] = total
    if getattr(data, "exact", True) is False:
        stats["exact"] = False
    if isinstance(data, CheckCitationsResult):
        stats["ok"] = data.ok
        stats["results"] = len(data.errors)
        stats["warnings"] = len(data.warnings)
    return stats


def _note(data: BaseModel) -> None:
    stats = _STATS.get()
    if stats is not None:
        stats.update(_result_stats(data))


def _text(text: str, structured: BaseModel | None = None, send_structured: bool = True) -> CallToolResult:
    if structured is not None:
        _note(structured)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=structured.model_dump(mode="json", by_alias=True)
        if structured is not None and send_structured
        else None,
    )


def _output(run: Callable[[], Output], send_structured: bool = True) -> CallToolResult:
    """Run a read tool; invalid arguments and unknown ids become tool errors the agent can read."""
    try:
        out = run()
    except (ToolInputError, StoreError) as e:
        raise ToolError(str(e)) from None
    _note(out.data)
    return CallToolResult(
        content=[TextContent(type="text", text=out.text)],
        structured_content=out.data.model_dump(mode="json", by_alias=True, exclude_none=True)
        if send_structured
        else None,
    )


def _result_text(result: Any) -> str:
    if isinstance(result, CallToolResult):
        return "\n".join(c.text for c in result.content if isinstance(c, TextContent))
    return str(result)


def _result_bytes(result: Any) -> int:
    """Bytes of the text content of a tool result."""
    if isinstance(result, CallToolResult):
        return sum(len(c.text.encode("utf-8")) for c in result.content if isinstance(c, TextContent))
    return len(str(result).encode("utf-8"))


def _call_extra(result: Any, text: str, caps: list[str]) -> dict[str, Any]:
    """Sizes, caps and citation ids of a result for the call log (no content)."""
    structured = result.structured_content if isinstance(result, CallToolResult) else None
    text_b = len(text.encode("utf-8"))
    struct_b = json_bytes(structured) if structured is not None else 0
    extra: dict[str, Any] = {
        "text_bytes": text_b,
        "structured_bytes": struct_b,
        "tokens_est": est_tokens(struct_b or text_b),
    }
    if "(config caps." in text:
        caps = [*caps, "limit"]
    if caps:
        extra["capped"] = sorted(set(caps))
    logs, arts = cite_ids(text)
    cites = compact_cites(logs, arts)
    if cites:
        extra["cites"] = cites[:MAX_LOG_CITES]
        if len(cites) > MAX_LOG_CITES:
            extra["cites_more"] = len(cites) - MAX_LOG_CITES
    return extra


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
# add_source writes the case DB (never the original file); adding the same file twice is a no-op.
WRITES_CASE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

_F = TypeVar("_F", bound=Callable[..., Any])
MAX_LOG_CITES = 300


class LogServer:
    """Builds the MCP server for one configuration and policy."""

    def __init__(
        self,
        config: Config,
        policy: Policy,
        session: str | None = None,
        token_verifier: TokenVerifier | None = None,
        auth: AuthSettings | None = None,
    ) -> None:
        self.config = config
        self.policy = policy
        self.app = App.create(config, policy)
        self.tools = Tools(self.app)
        self.session = session or uuid.uuid4().hex[:12]
        self.calllog = CallLog(
            config.call_log,
            self.session,
            redact=self.app.redactor.redact,
            rounds_dir=config.rounds_dir,
            round_gap=config.round_gap_minutes * 60.0,
            reporter=_round_report,
        )
        self.budget = ByteBudget(policy.byte_budget)
        self.structured = config.structured_output
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._fixed: FixedCost | None = None
        self.mcp: MCPServer = MCPServer(
            name=SERVER_NAME,
            title="Automotive log analysis",
            version=__version__,
            instructions=INSTRUCTIONS,
            token_verifier=token_verifier,
            auth=auth,
        )
        self._register_tools()
        self._register_prompts()

    # -- plumbing ---------------------------------------------------------------------------

    def fixed_cost(self) -> FixedCost:
        """What this server adds to every model request: instructions and the tool list."""
        if self._fixed is None:
            listing = _sync(self.mcp.list_tools())
            tools = [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in listing]
            self._fixed = fixed_cost(INSTRUCTIONS, tools)
        return self._fixed

    def _server_info(self) -> dict[str, Any]:
        return {"version": __version__, "structured": self.structured, "fixed": self.fixed_cost().as_log()}

    def _next_seq(self) -> int:
        with self._seq_lock:
            self._seq += 1
            return self._seq

    def _guard(self, fn: _F) -> _F:
        """Apply the byte budget and write one call-log line per call."""
        name = fn.__name__
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            seq = self._next_seq()
            bound = sig.bind_partial(*args, **kwargs)
            log_args = _jsonable(dict(bound.arguments))
            base: dict[str, Any] = {"seq": seq}
            if name == "check_citations" and isinstance(log_args.get("text"), str):
                text = log_args["text"]  # an answer quotes log content: keep only its size and hash
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
                log_args["text"] = f"<{len(text)} chars, sha256 {digest}>"
                logs, arts = cite_ids(text)
                base["answer_cites"] = compact_cites(logs, arts)[:MAX_LOG_CITES]
            base["args_hash"], base["near_hash"] = args_hashes(name, log_args)

            def log(
                status: Literal["ok", "error", "refused"],
                n: int,
                error: str | None = None,
                extra: dict[str, Any] | None = None,
            ) -> None:
                self.calllog.write(
                    name,
                    log_args,
                    status,
                    (time.perf_counter() - start) * 1000,
                    n,
                    error,
                    {**base, **(extra or {})},
                    self._server_info,
                )

            if self.budget.exhausted():
                msg = (
                    f"Byte budget of {self.budget.limit} bytes for this session is used up "
                    "(policy byte_budget). Answer with the evidence you have."
                )
                log("refused", 0, msg)
                raise ToolError(msg)
            stats: dict[str, Any] = {}
            caps: list[str] = []
            t_stats, t_caps = _STATS.set(stats), fmt.CAP_HITS.set(caps)
            try:
                result = fn(*args, **kwargs)
            except Exception as e:
                log("error", 0, str(e) or type(e).__name__)
                raise
            finally:
                _STATS.reset(t_stats)
                fmt.CAP_HITS.reset(t_caps)
            text = _result_text(result)
            n = _result_bytes(result)
            extra = _call_extra(result, text, caps)
            self.budget.add(n)
            log("ok", n, None, {**extra, **stats})
            return result

        return wrapper  # type: ignore[return-value]

    def _tool(self, fn: _F, annotations: ToolAnnotations, title: str) -> None:
        if self.policy.tool_allowed(fn.__name__):
            # structured_output=False: no output schema in the tool list, no structured content.
            structured = None if self.structured else False
            self.mcp.tool(title=title, annotations=annotations, structured_output=structured)(self._guard(fn))
            tool = self.mcp._tool_manager.get_tool(fn.__name__)
            if tool is not None:
                tool.fn_metadata.arg_model = _strict_args(tool.fn_metadata.arg_model, fn.__name__)
                tool.parameters = slim_schema({**tool.parameters, "additionalProperties": False})

    # -- tools ------------------------------------------------------------------------------

    def _register_tools(self) -> None:
        config = self.config
        tools = self.tools

        def add_source(
            case: Case,
            path: Annotated[str, Field(min_length=1, description="Path to a log file, directory or zip.")],
            format: Annotated[
                str | None,
                Field(description="Connector name to force, e.g. 'logcat'; omit to sniff the format."),
            ] = None,
            reindex: Annotated[
                bool,
                Field(description="Index again although already indexed; only when the user asks."),
            ] = False,
        ) -> CallToolResult:
            """Index a log file, directory or zip (e.g. a bugreport) into a case; lists the new sources.
            Tombstones, ANRs and crashes become artifacts. A file already indexed, in this case or
            another, is not indexed again: the reply names the case to use."""
            try:
                self.app.ingestor.check(path, format)
            except IngestError as e:
                raise ToolError(str(e)) from None
            future = self.app.workers.submit(case, config.case_db(case), path, format, reindex)
            try:
                result = future.result(timeout=ADD_WAIT_S)
            except TimeoutError:
                return _text(
                    f"Indexing {path} into case '{case}' continues in the background. "
                    "Call list_sources(case) to see progress; sources already show their committed records."
                )
            except (IngestError, ValueError) as e:
                raise ToolError(str(e)) from None
            return _text(_describe_add(result, case))

        def list_sources(case: Case) -> Annotated[CallToolResult, ListSourcesResult]:
            """List the sources of a case: format, record count, time range, build fingerprint, and how
            each maps onto the case clock that timeline uses."""
            rows = self.app.list_sources(case)
            exists = rows is not None
            infos = [_source_info(s) for s in rows or []]
            case_clock = None
            if infos and len(infos) > 1:
                try:
                    case_clock, clocks = tools.clock_summary(case)
                except (ToolInputError, StoreError):
                    clocks = {}
                for i in infos:
                    i.clock = clocks.get(i.id)
            result = ListSourcesResult(case=case, exists=exists, sources=infos, case_clock=case_clock)
            if not infos:
                why = "" if exists else " (the case does not exist yet)"
                return _text(
                    f"No sources in case '{case}'{why}. Add one with add_source(case, path).",
                    result,
                    self.structured,
                )
            lines = []
            for i in infos:
                flags = [i.status] if i.status != "done" else []
                if i.original_missing:
                    flags.append("original missing: relink with the CLI")
                if i.stale:
                    flags.append("stale: reindex recommended")
                if i.clock:
                    flags.append(f"clock {i.clock}")
                span = f"{i.ts_from or '-'} .. {i.ts_to or '-'}"
                lines.append(
                    f"{i.id}  {i.format}  {i.records} records  {span}  {i.name}"
                    + (f"  [{'; '.join(flags)}]" if flags else "")
                )
            if case_clock and any(i.clock for i in infos):
                lines.append(
                    f"case clock: {case_clock}; timeline(case) merges all sources, align(case) explains"
                )
            fps = sorted({i.fingerprint for i in infos if i.fingerprint})
            if fps:
                lines.append("fingerprint: " + ", ".join(fps))
            if self.app.workers.busy(case):
                lines.append("An import is still running in this case.")
            return _text("\n".join(lines), result, self.structured)

        def search(
            case: Case,
            query: Annotated[
                str,
                Field(
                    description="Text: words that must all occur (whole words, case-insensitive; "
                    'parts of CamelCase names count as words), "phrases", prefix*, AND/OR/NOT. '
                    "Regex mode: Python regex, case-sensitive. '' or '*': every record that passes "
                    "the filter (e.g. all lines of a tag or a time window).",
                ),
            ] = "",
            mode: Annotated[
                Literal["text", "regex"], Field(description="'text' (FTS5 over msg and tag) or 'regex'.")
            ] = "text",
            filter: Annotated[Filter | None, Field(description="Optional record filter.")] = None,
            collapse: Annotated[
                bool,
                Field(description="One line per repeated message, with ×count and last time."),
            ] = True,
            count_only: Annotated[
                bool, Field(description="Return only the number of matches, by level.")
            ] = False,
            limit: Annotated[int, Field(ge=1, le=500, description="Maximum lines to return.")] = 50,
            cursor: Annotated[
                str | None, Field(description="Cursor from a previous result's footer, to get the next page.")
            ] = None,
        ) -> Annotated[CallToolResult, SearchResult]:
            """Search log records by text or regex, in time order, each hit cited as log:<source>#<seq>.

            Repeats collapse to one line with ×count; hits of a crash or ANR name their artifact. The
            footer says how many more there are and how to narrow, and how many matches a sources
            filter left out. An empty query lists the records that pass the filter."""
            every = query.strip() in ("", "*")
            return _output(
                lambda: tools.search(
                    case,
                    None if every else query,
                    "text" if every else mode,
                    filter,
                    collapse,
                    count_only,
                    limit,
                    cursor,
                    self.app.workers.busy(case),
                ),
                self.structured,
            )

        def context(
            case: Case,
            at: Annotated[
                str,
                Field(
                    pattern=AT_RE,
                    description="Citation log:<source>#<seq>, or art:<id> for the record of an artifact.",
                ),
            ],
            before: Annotated[int, Field(ge=0, le=500, description="Lines before 'at'.")] = 20,
            after: Annotated[int, Field(ge=0, le=500, description="Lines after 'at'.")] = 20,
            scope: Annotated[
                Literal["source", "pid", "all"],
                Field(
                    description="'source': neighbouring lines of the same source; 'pid': lines of the same "
                    "process from all sources of the same file (e.g. main, system and crash buffers of a "
                    "bugreport), merged by time; 'all': lines of every source on the case clock (other "
                    "files, kernel logs), ~ marking aligned times."
                ),
            ] = "source",
        ) -> Annotated[CallToolResult, ContextResult]:
            """Show the lines around a record; the record itself is marked with '>'."""
            return _output(lambda: tools.context(case, at, before, after, scope), self.structured)

        def list_artifacts(
            case: Case,
            type: Annotated[
                Literal["tombstone", "anr", "java_crash", "dropbox"] | None,
                Field(description="Artifact type: 'tombstone', 'anr', 'java_crash' or 'dropbox'."),
            ] = None,
        ) -> Annotated[CallToolResult, ListArtifactsResult]:
            """List tombstones, ANRs, Java crashes and dropbox entries, grouped by crash signature in
            time order; copies of one crash are marked 'same crash as art:<id>'."""
            return _output(lambda: tools.list_artifacts(case, type), self.structured)

        def read_artifact(
            case: Case,
            id: Annotated[
                str,
                Field(
                    pattern=ART_CITATION_RE,
                    description="Artifact citation art:<id>[/section][:L<n>[-L<m>]] as printed.",
                ),
            ],
            section: Annotated[
                str | None,
                Field(description="Section to read, as listed in the summary; omit for the summary."),
            ] = None,
            start_line: Annotated[
                int | None,
                Field(
                    ge=1, description="First line to read (L<n>: line numbers count over the whole artifact)."
                ),
            ] = None,
            end_line: Annotated[int | None, Field(ge=1, description="Last line to read (L<n>).")] = None,
        ) -> Annotated[CallToolResult, ReadArtifactResult]:
            """Read an artifact: summary and section list first, then sections or line ranges.

            Lines are numbered L<n> over the whole artifact; cite them as art:<id>/<section>:L<n>.
            """
            return _output(
                lambda: tools.read_artifact(case, id, section, start_line, end_line), self.structured
            )

        def get_records(
            case: Case,
            ids: Annotated[
                list[Annotated[str, Field(pattern=LOG_CITATION_RE)]],
                Field(min_length=1, max_length=100, description="Citations log:<source>#<seq>."),
            ],
        ) -> Annotated[CallToolResult, GetRecordsResult]:
            """Get full records: messages not truncated (multi-line stack traces included), the timestamp
            as written, all attributes and the template's constant text for a code search."""
            return _output(lambda: tools.get_records(case, ids), self.structured)

        def overview(
            case: Case,
            filter: Annotated[Filter | None, Field(description="Optional record filter.")] = None,
        ) -> Annotated[CallToolResult, OverviewResult]:
            """Orientation for a case: level counts, top tags, processes and messages, clock segments,
            gaps, error bursts, a histogram, detector and artifact counts, each cited."""
            return _output(lambda: tools.overview(case, filter), self.structured)

        def count(
            case: Case,
            by: Annotated[
                Literal["tag", "level", "pid", "source", "template"] | None,
                Field(description="Field to group by; null for totals only (with bucket: a time series)."),
            ] = "tag",
            query: Annotated[
                str | None,
                Field(
                    min_length=1,
                    description="Optional: count only records matching this query (as in search: words, or a "
                    "regex with mode='regex').",
                ),
            ] = None,
            mode: Annotated[Literal["text", "regex"], Field(description="How to read 'query'.")] = "text",
            filter: Annotated[Filter | None, Field(description="Optional record filter.")] = None,
            bucket: Annotated[
                str | None,
                Field(
                    description="Optional time bucket, e.g. '10s', '1m', '1h': counts per bucket (at most 60 "
                    "buckets, empty ones included), each with its most frequent values."
                ),
            ] = None,
            limit: Annotated[int, Field(ge=1, le=100, description="Maximum values to list.")] = 20,
        ) -> Annotated[CallToolResult, CountResult]:
            """Count records by tag, level, pid, source or template, optionally per time bucket, each
            value with an example citation (≈: estimated from a sample)."""
            return _output(lambda: tools.count(case, by, query, mode, filter, bucket, limit), self.structured)

        def detect(
            case: Case,
            rules: Annotated[
                list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")]] | None,
                Field(description="Rule names to run (see the result's 'no hits' list); omit for all rules."),
            ] = None,
            filter: Annotated[Filter | None, Field(description="Optional record filter.")] = None,
            examples: Annotated[
                int, Field(ge=1, le=10, description="Example lines per rule (collapsed by message or pid).")
            ] = 3,
        ) -> Annotated[CallToolResult, DetectResult]:
            """Run detector rules for known failure patterns (crashes, ANRs, watchdog, low memory kills,
            SELinux, binder, VHAL errors, service restarts, boot not completed): cited hits by severity."""
            return _output(lambda: tools.detect(case, rules, filter, examples), self.structured)

        def check_citations(
            case: Case,
            text: Annotated[
                str,
                Field(
                    min_length=1,
                    max_length=200_000,
                    description="The answer in Markdown, with citations as the tools print them.",
                ),
            ],
        ) -> Annotated[CallToolResult, CheckCitationsResult]:
            """Check an answer's citations: each must be well-formed and resolve (errors); uncited
            findings and quotes not in the cited lines are warnings. 'PASS' when there are no errors."""
            return _output(lambda: tools.check_citations(case, text), self.structured)

        def timeline(
            case: Case,
            filter: Annotated[
                Filter | None,
                Field(
                    description="Optional record filter; from/to are case-clock times as timeline prints "
                    "them, or citations ('log:5#120-10s')."
                ),
            ] = None,
            query: Annotated[
                str | None,
                Field(min_length=1, description="Optional words that must occur (as in search, text mode)."),
            ] = None,
            limit: Annotated[int, Field(ge=1, le=500, description="Maximum lines to return.")] = 50,
            cursor: Annotated[
                str | None, Field(description="Cursor from a previous timeline footer.")
            ] = None,
        ) -> Annotated[CallToolResult, TimelineResult]:
            """Records of all sources (bugreport buffers, kernel logs, other devices' logs) merged on one
            case clock, in time order; '~' marks times aligned from another clock (align shows how)."""
            return _output(lambda: tools.timeline(case, filter, query, limit, cursor), self.structured)

        def align(
            case: Case,
            mode: Annotated[
                Literal["auto", "offset", "anchor", "reference", "reset"],
                Field(
                    description="'auto': show the alignment (computed automatically); 'offset': set a "
                    "source's offset to the case clock; 'anchor': at and ref happened at the same moment; "
                    "'reference': use a source's clock as the case clock; 'reset': drop manual settings."
                ),
            ] = "auto",
            source: Annotated[str | None, Field(description="Source id (offset, reference, reset).")] = None,
            seg: Annotated[
                int | None, Field(ge=1, description="Segment (boot) of a kernel source, for 'offset'.")
            ] = None,
            offset: Annotated[
                str | None,
                Field(
                    description="'offset': case time minus source time, e.g. '+1.5s', '-200ms'; 'anchor': "
                    "ref happened this long after at (default 0)."
                ),
            ] = None,
            at: Annotated[str | None, Field(pattern=AT_RE, description="'anchor': a citation.")] = None,
            ref: Annotated[
                str | None,
                Field(pattern=AT_RE, description="'anchor': a citation on another clock, same moment."),
            ] = None,
        ) -> Annotated[CallToolResult, AlignResult]:
            """How each source maps onto the case clock (offset, confidence, error, anchors), and manual
            alignment when the automatic one is missing or wrong."""
            return _output(lambda: tools.align(case, mode, source, seg, offset, at, ref), self.structured)

        def diff(
            case: Case,
            a: Annotated[
                DiffSide,
                Field(
                    description="Side A (e.g. the good run): a filter such as {'sources': ['2']}, "
                    "{'sources': ['2'], 'seg': 1} for one boot, or a from/to window."
                ),
            ],
            b: Annotated[DiffSide, Field(description="Side B (e.g. the bad run), as a.")],
            min_ratio: Annotated[
                float,
                Field(gt=1, le=1000, description="Report shares changed by this factor or more."),
            ] = 4.0,
            limit: Annotated[int, Field(ge=1, le=100, description="Maximum items per list.")] = 12,
        ) -> Annotated[CallToolResult, DiffResult]:
            """Compare two sets of records (two files or runs, two boots, before and after an event):
            messages only in B, only in A, and those whose rate changed; each item cited. Sides may be
            on different clocks."""
            return _output(lambda: tools.diff(case, a, b, min_ratio, limit), self.structured)

        self._tool(add_source, WRITES_CASE, "Add a log source")
        self._tool(list_sources, READ_ONLY, "List sources")
        self._tool(search, READ_ONLY, "Search records")
        self._tool(context, READ_ONLY, "Lines around a record")
        self._tool(list_artifacts, READ_ONLY, "List artifacts")
        self._tool(read_artifact, READ_ONLY, "Read an artifact")
        self._tool(get_records, READ_ONLY, "Get full records")
        self._tool(overview, READ_ONLY, "Case overview")
        self._tool(count, READ_ONLY, "Count records")
        self._tool(detect, READ_ONLY, "Detect known failure patterns")
        self._tool(check_citations, READ_ONLY, "Check the citations of an answer")
        self._tool(timeline, READ_ONLY, "Timeline of all sources")
        self._tool(align, WRITES_CASE, "Align source clocks")
        self._tool(diff, READ_ONLY, "Compare two sets of records")

    # -- prompts ----------------------------------------------------------------------------

    def _register_prompts(self) -> None:
        @self.mcp.prompt(title="Defect triage")
        def defect_triage(
            case: Annotated[str, Field(description="Case name.")],
            symptom: Annotated[str, Field(description="What went wrong, as the defect report says it.")] = "",
        ) -> str:
            """Analyse a defect from the logs of a case, citing every finding."""
            if not re.fullmatch(CASE_RE, case):
                raise ValueError(f"invalid case name {case!r}")
            symptom_line = f"Reported symptom: {symptom}\n\n" if symptom else ""
            return TRIAGE_PROMPT.format(case=case, symptom=symptom_line)


TOOL_NAMES = (
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
    "timeline",
    "align",
    "diff",
)


_FILTER_KEYS = frozenset(f.alias or n for n, f in Filter.model_fields.items()) | set(Filter.model_fields)


class _RejectUnknownArgs(BaseModel):
    """Mixin for a tool's argument model: unknown arguments are an error with a hint. MCPServer's
    models ignore them, so a misplaced ``level`` (top level instead of inside ``filter``) silently
    widened a search."""

    _tool: ClassVar[str] = ""

    @model_validator(mode="before")
    @classmethod
    def _reject_unknown(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        names = {f.alias or n for n, f in cls.model_fields.items()}
        unknown = sorted(k for k in data if k not in names and k not in cls.model_fields)
        if not unknown:
            return data
        hints = [
            f"'{k}' belongs inside filter, e.g. filter={{'{k}': ...}}"
            if k in _FILTER_KEYS and "filter" in names
            else f"'{k}' belongs inside a and b, e.g. a={{'sources': ['2'], '{k}': ...}}"
            if k in _FILTER_KEYS and "a" in names
            else f"'{k}' is not a parameter"
            for k in unknown
        ]
        raise ValueError(f"{cls._tool}: {'; '.join(hints)}. Parameters: {', '.join(sorted(names))}.")


_M = TypeVar("_M", bound=BaseModel)


def _strict_args(model: type[_M], tool: str) -> type[_M]:
    """The tool's argument model, rejecting unknown arguments (see ``_RejectUnknownArgs``)."""
    strict: type[_M] = type(
        model.__name__,
        (_RejectUnknownArgs, model),
        {"model_config": ConfigDict(**{**model.model_config, "extra": "forbid"}), "_tool": tool},
    )
    return strict


def slim_schema(node: Any) -> Any:
    """A smaller input schema with the same meaning for a model: no generated titles, optional
    fields as their plain type (``anyOf [X, null]`` and ``default: null`` dropped; the server still
    accepts null, since arguments are validated against the pydantic models, not this schema).
    Clients send these schemas with every model request, so every byte counts many times."""
    if isinstance(node, list):
        return [slim_schema(v) for v in node]
    if not isinstance(node, dict):
        return node
    out = {k: slim_schema(v) for k, v in node.items() if k != "title" or not isinstance(v, str)}
    any_of = out.get("anyOf")
    if isinstance(any_of, list) and len(any_of) == 2 and {"type": "null"} in any_of:
        other = next(v for v in any_of if v != {"type": "null"})
        del out["anyOf"]
        out = {**other, **out}
    if "default" in out and out["default"] is None:
        del out["default"]
    return out


def _round_report(path: Path) -> str:
    """The report for one round file (the same as ``calls --log <file>``)."""
    from .callreport import analyse, read_calllog

    sessions = read_calllog(path)
    head = f"# automotive-log-mcp round report, rewritten after every call; analysed from {path.name}"
    return "\n\n".join([head, *(analyse(s).text() for s in sessions if s.calls)])


def _sync(coro: Any) -> Any:
    """Result of a coroutine that never awaits (MCPServer.list_tools), without an event loop."""
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    coro.close()
    raise RuntimeError("coroutine awaited")


def build_server(config: Config, policy: Policy, session: str | None = None) -> MCPServer:
    policy.check_tools(TOOL_NAMES)
    return LogServer(config, policy, session).mcp
