"""MCP tools, server instructions and prompts.

M0: every P0 tool is registered with its full parameter schema and annotations. ``list_sources``
answers; the other tools fail with a clear "not implemented yet" error naming the milestone that
brings them. Every call passes through the policy (byte budget) and is written to the call log.
"""

from __future__ import annotations

import functools
import inspect
import re
import time
import uuid
from collections.abc import Callable
from typing import Annotated, Any, Literal, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .calllog import CallLog
from .config import Config
from .policy import ByteBudget, Policy

SERVER_NAME = "automotive-log-mcp"

INSTRUCTIONS = """\
Tools for analysing a defect from automotive logs (Android bugreport, logcat, kernel logs). \
The server parses, indexes and searches; it never interprets. Reasoning is your job.

Analysis order:
1. list_sources(case) to see what is in the case; add_source(case, path) if it is empty.
2. list_artifacts(case) for tombstones, ANRs and crashes; read_artifact for the stack trace.
3. search(case, query, filter) for errors around the defect; narrow with level, tags, from/to.
4. context(case, at) to read the lines around a hit; get_records(case, ids) for full messages.

Citation rule: every finding in your answer cites the line or artifact it comes from, exactly as \
the tools print it: log:<source>#<seq> for a log line, art:<id>[/section][:L<line>] for an \
artifact. A statement without a citation is a guess; say so.

Outputs are capped (about 50 lines / 8 KB, messages cut at 200 characters with …). When a result \
says it was capped, narrow the query instead of paging through everything. Timestamps printed in \
outputs can be passed back as from/to unchanged.
"""

CASE_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
LOG_CITATION_RE = r"^log:[A-Za-z0-9._-]+#[1-9][0-9]*$"
ART_CITATION_RE = r"^art:[A-Za-z0-9._-]+(/[^:\s]+)?(:L[1-9][0-9]*)?$"

Level = Literal["V", "D", "I", "W", "E", "F"]

Case = Annotated[
    str,
    Field(
        pattern=CASE_RE,
        description="Case name, e.g. a ticket id. Letters, digits, '.', '_' and '-'; at most 64 characters.",
    ),
]


class Filter(BaseModel):
    """Record filter shared by search and later tools. All fields are optional and combine with AND."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    sources: list[str] | None = Field(None, description="Source ids to include, as list_sources prints them.")
    from_: str | None = Field(
        None,
        alias="from",
        description="Start time: a timestamp as printed in outputs, a relative time like '-5m' "
        "(relative to the end of the case), or a citation log:<source>#<seq>.",
    )
    to: str | None = Field(None, description="End time, same forms as 'from'.")
    level: Level | None = Field(None, description="Minimum level: V < D < I < W < E < F.")
    tags: list[str] | None = Field(None, description="Tags to include; globs allowed, e.g. 'Car*'.")
    not_tags: list[str] | None = Field(None, description="Tags to exclude; globs allowed.")
    pid: int | None = Field(None, ge=0, description="Process id.")
    attrs: dict[str, str | int | float | bool] | None = Field(
        None, description="Format-specific attributes that must match exactly, e.g. {'buffer': 'crash'}."
    )


class SourceInfo(BaseModel):
    id: str = Field(description="Source id used in citations log:<id>#<seq>.")
    format: str
    path: str = Field(description="Path of the original file; originals are never copied or modified.")
    records: int
    ts_from: str | None = Field(None, description="First timestamp as printed in outputs.")
    ts_to: str | None = Field(None, description="Last timestamp as printed in outputs.")
    fingerprint: str | None = Field(None, description="Android build fingerprint, if known.")
    original_missing: bool = Field(False, description="The original file is no longer at its path.")
    stale: bool = Field(False, description="Indexed with an older connector or redaction rules.")


class ListSourcesResult(BaseModel):
    case: str
    exists: bool = Field(description="Whether the case DB exists yet.")
    sources: list[SourceInfo]


def _not_implemented(tool: str, milestone: str) -> ToolError:
    return ToolError(f"{tool} is not implemented yet in this version (planned for {milestone}).")


def _text(text: str, structured: BaseModel | None = None) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=structured.model_dump(mode="json", by_alias=True) if structured else None,
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
        self.session = session or uuid.uuid4().hex[:12]
        self.calllog = CallLog(config.call_log, self.session)
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
            raise _not_implemented("add_source", "M1")

        def list_sources(case: Case) -> Annotated[CallToolResult, ListSourcesResult]:
            """List the sources of a case: format, record count, time range and build fingerprint."""
            exists = config.case_db(case).exists()
            sources: list[SourceInfo] = []  # the case schema arrives in M1
            result = ListSourcesResult(case=case, exists=exists, sources=sources)
            if not sources:
                why = "" if exists else " (the case does not exist yet)"
                return _text(
                    f"No sources in case '{case}'{why}. Add one with add_source(case, path).", result
                )
            return _text("\n".join(s.id for s in sources), result)  # pragma: no cover - M1

        def search(
            case: Case,
            query: Annotated[
                str, Field(min_length=1, description="FTS5 text query, or a regex if mode='regex'.")
            ],
            mode: Annotated[
                Literal["text", "regex"], Field(description="'text' (FTS5 over msg and tag) or 'regex'.")
            ] = "text",
            filter: Annotated[Filter | None, Field(description="Optional record filter.")] = None,
            collapse: Annotated[
                bool,
                Field(description="Collapse repeated messages into one line with count and first/last time."),
            ] = True,
            count_only: Annotated[bool, Field(description="Return only the number of matches.")] = False,
            limit: Annotated[int, Field(ge=1, le=500, description="Maximum lines to return.")] = 50,
            cursor: Annotated[
                str | None, Field(description="Cursor from a previous result's footer, to get the next page.")
            ] = None,
        ) -> CallToolResult:
            """Search log records by text or regex. Every hit is cited as log:<source>#<seq>."""
            raise _not_implemented("search", "M3")

        def context(
            case: Case,
            at: Annotated[str, Field(pattern=LOG_CITATION_RE, description="Citation log:<source>#<seq>.")],
            before: Annotated[int, Field(ge=0, le=500, description="Lines before 'at'.")] = 20,
            after: Annotated[int, Field(ge=0, le=500, description="Lines after 'at'.")] = 20,
            scope: Annotated[
                Literal["source", "pid"],
                Field(
                    description="'source': neighbouring lines of the same source; 'pid': of the same process."
                ),
            ] = "source",
        ) -> CallToolResult:
            """Show the lines around a record."""
            raise _not_implemented("context", "M3")

        def list_artifacts(
            case: Case,
            type: Annotated[
                str | None,
                Field(description="Artifact type, e.g. 'tombstone', 'anr', 'java_crash', 'dropbox'."),
            ] = None,
        ) -> CallToolResult:
            """List tombstones, ANRs, Java crashes and dropbox entries, grouped by crash signature."""
            raise _not_implemented("list_artifacts", "M3")

        def read_artifact(
            case: Case,
            id: Annotated[
                str,
                Field(
                    pattern=ART_CITATION_RE,
                    description="Artifact citation art:<id>[/section][:L<line>] as printed.",
                ),
            ],
            section: Annotated[
                str | None,
                Field(description="Section to read, as listed in the summary; omit for the summary."),
            ] = None,
            start_line: Annotated[
                int | None, Field(ge=1, description="First line of the section to read.")
            ] = None,
            end_line: Annotated[
                int | None, Field(ge=1, description="Last line of the section to read.")
            ] = None,
        ) -> CallToolResult:
            """Read an artifact: summary and section list first, then sections or line ranges."""
            raise _not_implemented("read_artifact", "M3")

        def get_records(
            case: Case,
            ids: Annotated[
                list[Annotated[str, Field(pattern=LOG_CITATION_RE)]],
                Field(min_length=1, max_length=100, description="Citations log:<source>#<seq>."),
            ],
        ) -> CallToolResult:
            """Get full records, with messages not truncated and all attributes."""
            raise _not_implemented("get_records", "M3")

        self._tool(add_source, WRITES_CASE, "Add a log source")
        self._tool(list_sources, READ_ONLY, "List sources")
        self._tool(search, READ_ONLY, "Search records")
        self._tool(context, READ_ONLY, "Lines around a record")
        self._tool(list_artifacts, READ_ONLY, "List artifacts")
        self._tool(read_artifact, READ_ONLY, "Read an artifact")
        self._tool(get_records, READ_ONLY, "Get full records")

    # -- prompts ----------------------------------------------------------------------------

    def _register_prompts(self) -> None:
        @self.mcp.prompt(title="Defect triage")
        def defect_triage(
            case: Annotated[str, Field(description="Case name.")],
            symptom: Annotated[str, Field(description="What went wrong, as the defect report says it.")] = "",
        ) -> str:
            """Analyse a defect from the logs of a case, citing every finding."""
            # The P0 workflow is filled in with M3; the checks before answering with M4.
            if not re.fullmatch(CASE_RE, case):
                raise ValueError(f"invalid case name {case!r}")
            symptom_line = f"\nReported symptom: {symptom}\n" if symptom else ""
            return (
                f"Triage the defect in case '{case}' using the automotive-log-mcp tools.{symptom_line}\n"
                "Follow the analysis order and the citation rule from the server instructions."
            )


TOOL_NAMES = (
    "add_source",
    "list_sources",
    "search",
    "context",
    "list_artifacts",
    "read_artifact",
    "get_records",
)


def build_server(config: Config, policy: Policy, session: str | None = None) -> MCPServer:
    policy.check_tools(TOOL_NAMES)
    return LogServer(config, policy, session).mcp
