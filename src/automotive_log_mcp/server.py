"""MCP tools, server instructions and prompts.

Every tool is registered with its full parameter schema, an output schema and annotations; the
tool logic is in ``tools.py``. Every call passes through the policy (allowed tools, byte budget)
and is written to the call log with redacted arguments (D5); the text given to
``check_citations`` is logged only as its length and hash (it quotes log content).
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import re
import time
import uuid
from collections.abc import Callable
from typing import Annotated, Any, Literal, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from . import __version__
from .app import App
from .calllog import CallLog
from .config import CASE_NAME_RE, Config
from .core.ingest import AddResult, IngestError
from .policy import ByteBudget, Policy
from .store import query
from .store.db import StoreError
from .tools import (
    CheckCitationsResult,
    ContextResult,
    CountResult,
    DetectResult,
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

SERVER_NAME = "automotive-log-mcp"

INSTRUCTIONS = """\
Tools for analysing a defect from automotive logs (Android bugreport, logcat, kernel logs). \
The server parses, indexes and searches; it never interprets. Reasoning is your job.

Analysis order:
1. list_sources(case): what is in the case (sources, time ranges, build fingerprint). If it is \
empty, add_source(case, path) with the bugreport zip or log file.
2. overview(case): levels, top tags, processes and messages, gaps, error bursts, a histogram, \
detector and artifact counts: where to look first.
3. detect(case): known failure patterns (crashes, ANRs, watchdog, low memory kills, SELinux \
denials, binder failures, VHAL errors, service restarts, boot not completed) with cited lines.
4. list_artifacts(case): tombstones (native crashes), ANRs, Java crashes and dropbox entries, \
grouped by crash signature, in time order. Copies of one crash are marked 'same crash as'.
5. read_artifact(case, id='art:<id>') for the summary and section list, then the section it \
suggests (backtrace, exception, the main thread of an ANR) for the stack trace.
6. context(case, at='art:<id>' or 'log:<source>#<seq>') for the lines before a crash; \
scope='pid' follows one process across the buffers of a bugreport.
7. search(case, query, filter) for errors around the defect: start with filter level 'W' or 'E' \
and a time window (from/to), keep collapse on, narrow with tags or pid. Use mode='regex' only for \
patterns; text mode matches whole words. count(case, by, query, filter, bucket) counts records by \
tag, level, pid, source or template, optionally per time bucket.
8. get_records(case, ids) for full messages that other tools cut with '…'.
9. check_citations(case, text) with your answer before you give it: every citation must resolve.

Citation rule: every finding in your answer cites the line or artifact it comes from, exactly as \
the tools print it: log:<source>#<seq> for a log line, art:<id>[/section][:L<line>] for an \
artifact line. A statement without a citation is a guess; say so.

Outputs are capped (about 50 lines / 8 KB, messages cut at 200 characters with …). When a result \
says it was capped, narrow the query instead of paging through everything. Timestamps printed in \
outputs can be passed back as from/to unchanged; from/to also take '-5m' (before the end of the \
case), '+30s' (after from) and citations such as 'log:3#120-30s'. Values like MAC addresses and \
VINs are redacted to tokens (<MAC:3fa2c1d0>); the same value always has the same token. Counts \
marked ≈ are estimated from a sample of a very broad query.
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
        description="Case name, e.g. a ticket id. Letters, digits, '.', '_' and '-'; at most 64 characters.",
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
    error: str | None = None


class ListSourcesResult(BaseModel):
    case: str
    exists: bool = Field(description="Whether the case DB exists yet.")
    sources: list[SourceInfo]


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


def _describe_add(result: AddResult) -> str:
    lines = []
    for f in result.files:
        ids = ", ".join(map(str, f.source_ids)) or "none"
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


def _text(text: str, structured: BaseModel | None = None) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=structured.model_dump(mode="json", by_alias=True) if structured else None,
    )


def _output(run: Callable[[], Output]) -> CallToolResult:
    """Run a read tool; invalid arguments and unknown ids become tool errors the agent can read."""
    try:
        out = run()
    except (ToolInputError, StoreError) as e:
        raise ToolError(str(e)) from None
    return CallToolResult(
        content=[TextContent(type="text", text=out.text)],
        structured_content=out.data.model_dump(mode="json", by_alias=True, exclude_none=True),
    )


def _result_bytes(result: Any) -> int:
    """Bytes the agent reads: the text content of a tool result."""
    if isinstance(result, CallToolResult):
        return sum(len(c.text.encode("utf-8")) for c in result.content if isinstance(c, TextContent))
    return len(str(result).encode("utf-8"))


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


class LogServer:
    """Builds the MCP server for one configuration and policy."""

    def __init__(self, config: Config, policy: Policy, session: str | None = None) -> None:
        self.config = config
        self.policy = policy
        self.app = App.create(config, policy)
        self.tools = Tools(self.app)
        self.session = session or uuid.uuid4().hex[:12]
        self.calllog = CallLog(config.call_log, self.session, redact=self.app.redactor.redact)
        self.budget = ByteBudget(policy.byte_budget)
        self.mcp: MCPServer = MCPServer(
            name=SERVER_NAME,
            title="Automotive log analysis",
            version=__version__,
            instructions=INSTRUCTIONS,
        )
        self._register_tools()
        self._register_prompts()

    # -- plumbing ---------------------------------------------------------------------------

    def _guard(self, fn: _F) -> _F:
        """Apply the byte budget and write one call-log line per call."""
        name = fn.__name__
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            bound = sig.bind_partial(*args, **kwargs)
            log_args = _jsonable(dict(bound.arguments))
            if name == "check_citations" and isinstance(log_args.get("text"), str):
                text = log_args["text"]  # an answer quotes log content: keep only its size and hash
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
                log_args["text"] = f"<{len(text)} chars, sha256 {digest}>"

            def log(status: Literal["ok", "error", "refused"], n: int, error: str | None = None) -> None:
                self.calllog.write(name, log_args, status, (time.perf_counter() - start) * 1000, n, error)

            if self.budget.exhausted():
                msg = (
                    f"Byte budget of {self.budget.limit} bytes for this session is used up "
                    "(policy byte_budget). Answer with the evidence you have."
                )
                log("refused", 0, msg)
                raise ToolError(msg)
            try:
                result = fn(*args, **kwargs)
            except Exception as e:
                log("error", 0, str(e) or type(e).__name__)
                raise
            n = _result_bytes(result)
            self.budget.add(n)
            log("ok", n)
            return result

        return wrapper  # type: ignore[return-value]

    def _tool(self, fn: _F, annotations: ToolAnnotations, title: str) -> None:
        if self.policy.tool_allowed(fn.__name__):
            self.mcp.tool(title=title, annotations=annotations)(self._guard(fn))

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
        ) -> CallToolResult:
            """Register a log file or archive in a case and index it. Returns the new source ids.

            A bugreport zip expands into several sources (main, system, events, crash, kernel) and its
            tombstones, ANRs and crashes become artifacts. The original file is referenced, not copied.
            """
            try:
                self.app.ingestor.check(path, format)
            except IngestError as e:
                raise ToolError(str(e)) from None
            future = self.app.workers.submit(case, config.case_db(case), path, format)
            try:
                result = future.result(timeout=ADD_WAIT_S)
            except TimeoutError:
                return _text(
                    f"Indexing {path} into case '{case}' continues in the background. "
                    "Call list_sources(case) to see progress; sources already show their committed records."
                )
            except (IngestError, ValueError) as e:
                raise ToolError(str(e)) from None
            return _text(_describe_add(result))

        def list_sources(case: Case) -> Annotated[CallToolResult, ListSourcesResult]:
            """List the sources of a case: format, record count, time range and build fingerprint."""
            rows = self.app.list_sources(case)
            exists = rows is not None
            infos = [_source_info(s) for s in rows or []]
            result = ListSourcesResult(case=case, exists=exists, sources=infos)
            if not infos:
                why = "" if exists else " (the case does not exist yet)"
                return _text(
                    f"No sources in case '{case}'{why}. Add one with add_source(case, path).", result
                )
            lines = []
            for i in infos:
                flags = [i.status] if i.status != "done" else []
                if i.original_missing:
                    flags.append("original missing: relink with the CLI")
                if i.stale:
                    flags.append("stale: reindex recommended")
                span = f"{i.ts_from or '-'} .. {i.ts_to or '-'}"
                lines.append(
                    f"{i.id}  {i.format}  {i.records} records  {span}  {i.name}"
                    + (f"  [{'; '.join(flags)}]" if flags else "")
                )
            fps = sorted({i.fingerprint for i in infos if i.fingerprint})
            if fps:
                lines.append("fingerprint: " + ", ".join(fps))
            if self.app.workers.busy(case):
                lines.append("An import is still running in this case.")
            return _text("\n".join(lines), result)

        def search(
            case: Case,
            query: Annotated[
                str,
                Field(
                    min_length=1,
                    description="Text: words that must all occur (case-insensitive, whole words), "
                    "\"quoted phrases\", prefix*, upper-case AND/OR/NOT. Regex (mode='regex'): a Python "
                    "regular expression over message and tag, case-sensitive.",
                ),
            ],
            mode: Annotated[
                Literal["text", "regex"], Field(description="'text' (FTS5 over msg and tag) or 'regex'.")
            ] = "text",
            filter: Annotated[Filter | None, Field(description="Optional record filter.")] = None,
            collapse: Annotated[
                bool,
                Field(
                    description="Collapse repeated messages (same source, template, tag and level) into "
                    "one line with count and last time; lines are in time order of first occurrence."
                ),
            ] = True,
            count_only: Annotated[
                bool, Field(description="Return only the number of matches, by level.")
            ] = False,
            limit: Annotated[int, Field(ge=1, le=500, description="Maximum lines to return.")] = 50,
            cursor: Annotated[
                str | None, Field(description="Cursor from a previous result's footer, to get the next page.")
            ] = None,
        ) -> Annotated[CallToolResult, SearchResult]:
            """Search log records by text or regex, in time order. Every hit is cited as log:<source>#<seq>.

            Collapsed hits show ×count and the last occurrence. Hits that belong to a crash, tombstone or
            ANR name their artifact (art:<id>). A footer says how many more there are and how to narrow;
            an empty result says why. 'code search' lists the constant text of the messages' templates.
            """
            return _output(
                lambda: tools.search(
                    case,
                    query,
                    mode,
                    filter,
                    collapse,
                    count_only,
                    limit,
                    cursor,
                    self.app.workers.busy(case),
                )
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
                Literal["source", "pid"],
                Field(
                    description="'source': neighbouring lines of the same source; 'pid': lines of the same "
                    "process from all sources of the same file (e.g. main, system and crash buffers of a "
                    "bugreport), merged by time."
                ),
            ] = "source",
        ) -> Annotated[CallToolResult, ContextResult]:
            """Show the lines around a record; the record itself is marked with '>'."""
            return _output(lambda: tools.context(case, at, before, after, scope))

        def list_artifacts(
            case: Case,
            type: Annotated[
                Literal["tombstone", "anr", "java_crash", "dropbox"] | None,
                Field(description="Artifact type: 'tombstone', 'anr', 'java_crash' or 'dropbox'."),
            ] = None,
        ) -> Annotated[CallToolResult, ListArtifactsResult]:
            """List tombstones, ANRs, Java crashes and dropbox entries, grouped by crash signature.

            Groups are in time order of their first artifact. Copies of one crash (crash buffer and dropbox,
            an ANR file and the bugreport section) are marked 'same crash as art:<id>'.
            """
            return _output(lambda: tools.list_artifacts(case, type))

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
            return _output(lambda: tools.read_artifact(case, id, section, start_line, end_line))

        def get_records(
            case: Case,
            ids: Annotated[
                list[Annotated[str, Field(pattern=LOG_CITATION_RE)]],
                Field(min_length=1, max_length=100, description="Citations log:<source>#<seq>."),
            ],
        ) -> Annotated[CallToolResult, GetRecordsResult]:
            """Get full records: messages not truncated (multi-line stack traces included), the timestamp
            as written, all attributes and the template's constant text for a code search."""
            return _output(lambda: tools.get_records(case, ids))

        def overview(
            case: Case,
            filter: Annotated[Filter | None, Field(description="Optional record filter.")] = None,
        ) -> Annotated[CallToolResult, OverviewResult]:
            """Orientation for a case: record and level counts, top tags, processes and messages
            (templates), clock segments, gaps over 30 s, error bursts (E/F per 10 s above 5× the median),
            a histogram of up to 60 buckets, detector hit counts and artifact counts. Each item is cited.
            """
            return _output(lambda: tools.overview(case, filter))

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
            """Count records by tag, level, pid, source or message template, optionally per time bucket.

            Each value comes with an example citation. Broad text queries are counted from a sample (≈);
            a capped regex scan gives lower bounds ('at least').
            """
            return _output(lambda: tools.count(case, by, query, mode, filter, bucket, limit))

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
            """Run detector rules for known failure patterns: Java and native crashes, ANRs, watchdog, low
            memory kills, SELinux denials, binder failures, VHAL errors, service restarts, boot not completed
            (plus rules from the configured rule directories). Hits by severity and time, with citations and
            a hint what to read next."""
            return _output(lambda: tools.detect(case, rules, filter, examples))

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
            """Check an answer before giving it: every log:<source>#<seq> and art:<id>[/section][:L<n>]
            citation must be well-formed and resolve in the case (errors); paragraphs with findings but no
            citation, and quoted text not found in the cited lines, are warnings. 'PASS' when there are no
            errors."""
            return _output(lambda: tools.check_citations(case, text))

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
)


def build_server(config: Config, policy: Policy, session: str | None = None) -> MCPServer:
    policy.check_tools(TOOL_NAMES)
    return LogServer(config, policy, session).mcp
