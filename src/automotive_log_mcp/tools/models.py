"""Argument and result models of the read tools, and :class:`Output`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Level = Literal["V", "D", "I", "W", "E", "F"]


class ToolInputError(ValueError):
    """Invalid arguments, or ids that do not exist; the message says how to fix the call."""


class Filter(BaseModel):
    """Record filter; all fields optional, combined with AND."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    sources: list[str] | None = Field(
        default=None, description="Source ids to include, as list_sources prints them."
    )
    from_: str | None = Field(
        default=None,
        alias="from",
        description="Start time, inclusive: a timestamp as printed, '-5m' (before the case end), "
        "'+30s' (after its start), or a citation with optional offset ('log:3#1-30s', 'art:2').",
    )
    to: str | None = Field(
        default=None,
        description="End time, inclusive to the precision given; as 'from' ('+30s': after 'from').",
    )
    level: Level | None = Field(default=None, description="Minimum level: V < D < I < W < E < F.")
    tags: list[str] | None = Field(default=None, description="Tags to include; globs allowed, e.g. 'Car*'.")
    not_tags: list[str] | None = Field(default=None, description="Tags to exclude; globs allowed.")
    pid: int | None = Field(default=None, ge=0, description="Process id.")
    attrs: dict[str, str | int | float | bool] | None = Field(
        default=None,
        description="Attributes that must match exactly, as get_records shows them, e.g. {'art': 3}.",
    )


class DiffSide(Filter):
    """One side of ``diff``: a record filter, optionally one segment of a single source."""

    seg: int | None = Field(
        default=None,
        ge=1,
        description="Segment (boot or clock reset) of the one source in 'sources'; overview lists them.",
    )


class RecordOut(BaseModel):
    cite: str = Field(description="Citation log:<source>#<seq>.")
    ts: str | None = Field(default=None, description="Time, accepted back as from/to.")
    level: str | None = None
    tag: str | None = None
    pid: int | None = None
    tid: int | None = None
    msg: str = Field(description="Message; cut at the output limit with … unless from get_records.")
    cut: bool | None = Field(default=None, description="True when msg was cut; get_records has it in full.")
    art: str | None = Field(default=None, description="The artifact this record belongs to (read_artifact).")
    count: int | None = Field(
        default=None, description="Collapsed: number of matches with this message template."
    )
    last: str | None = Field(default=None, description="Collapsed: citation of the last match.")
    last_ts: str | None = Field(default=None, description="Collapsed: time of the last match.")
    template: str | None = Field(default=None, description="Message template (variable parts as <*>).")
    literal: str | None = Field(
        default=None, description="Longest constant text of the template, for a code search."
    )
    ts_raw: str | None = Field(default=None, description="get_records: the timestamp as written in the log.")
    attrs: dict[str, Any] | None = Field(default=None, description="get_records: format-specific attributes.")
    source: str | None = Field(default=None, description="get_records: source id.")


class SearchResult(BaseModel):
    case: str
    mode: str
    query: str = Field(description="The query as run (raw values redacted, as in the logs).")
    total: int = Field(description="Matches.")
    exact: bool = Field(
        default=True,
        description="False when a regex scan stopped early (total is a lower bound) or when counts were "
        "estimated from a sample of a very broad text query (≈).",
    )
    groups: int = Field(description="Distinct messages (source, template, tag, level) among the matches.")
    hits: list[RecordOut]
    next_cursor: str | None = None
    warnings: list[str] = Field(default_factory=list)
    empty_reason: str | None = None


class TimelineRecord(BaseModel):
    cite: str
    ts: str | None = Field(default=None, description="Case-clock time; accepted back as timeline from/to.")
    approx: bool | None = Field(default=None, description="True: mapped from another clock (~ in the text).")
    level: str | None = None
    tag: str | None = None
    pid: int | None = None
    tid: int | None = None
    msg: str
    cut: bool | None = None
    art: str | None = None


class ContextResult(BaseModel):
    case: str
    at: str
    scope: str
    records: list[RecordOut | TimelineRecord]
    note: str | None = None


class ArtifactOut(BaseModel):
    cite: str
    type: str
    ts: str | None = None
    pid: int | None = None
    process: str | None = None
    record: str = Field(description="Its record on the timeline, for context().")
    lines: int
    headline: str
    same_as: str | None = Field(
        default=None, description="The same crash already listed (e.g. crash buffer and dropbox)."
    )


class ArtifactGroupOut(BaseModel):
    type: str
    signature: str | None = None
    count: int
    artifacts: list[ArtifactOut]
    more: int = 0


class ListArtifactsResult(BaseModel):
    case: str
    total: int
    groups: list[ArtifactGroupOut]
    more_groups: int = 0
    empty_reason: str | None = None


class SectionOut(BaseModel):
    name: str
    cite: str
    first: int
    last: int


class LineOut(BaseModel):
    n: int
    text: str


class ReadArtifactResult(BaseModel):
    cite: str
    type: str
    name: str
    record: str
    ts: str | None = None
    ts_raw: str | None = None
    pid: int | None = None
    tid: int | None = None
    process: str | None = None
    signature: str | None = None
    summary: str | None = None
    sections: list[SectionOut] = Field(default_factory=list)
    same_crash: list[str] = Field(default_factory=list)
    lines: list[LineOut] | None = None
    next: str | None = Field(default=None, description="Citation of the lines not shown, to read next.")


class GetRecordsResult(BaseModel):
    case: str
    records: list[RecordOut]
    missing: list[str] = Field(default_factory=list)
    not_shown: list[str] = Field(default_factory=list, description="Left out at the byte cap; ask again.")


class CountGroup(BaseModel):
    key: str | int | None = Field(
        default=None, description="The value: tag, level, pid, source id or template id (null: none)."
    )
    label: str = Field(description="As printed: tag name, level, pid and process, source name, template.")
    count: int
    example: str | None = Field(default=None, description="Citation of one record with this value.")


class CountBucket(BaseModel):
    ts: str = Field(description="Start of the bucket, accepted back as from.")
    count: int
    top: list[CountGroup] = Field(default_factory=list, description="Most frequent values in the bucket.")


class CountResult(BaseModel):
    case: str
    by: str | None = None
    query: str | None = None
    total: int
    exact: bool = Field(
        default=True, description="False for a capped regex scan (lower bounds) or a sampled broad query (≈)."
    )
    distinct: int = Field(default=0, description="Distinct values of 'by'.")
    groups: list[CountGroup] = Field(default_factory=list)
    bucket: str | None = None
    buckets: list[CountBucket] | None = None
    warnings: list[str] = Field(default_factory=list)


class HitOut(BaseModel):
    record: RecordOut
    count: int = Field(description="Hits in this group (same template, or same pid).")
    last: str | None = Field(default=None, description="Citation of the last hit in the group.")
    followed_by: str | None = Field(default=None, description="followed_by rules: the second event.")
    open: bool | None = Field(default=None, description="Absence rules: the log ends inside the window.")


class RuleHits(BaseModel):
    rule: str
    severity: str
    description: str
    count: int
    exact: bool = True
    first_ts: str | None = None
    last_ts: str | None = None
    hits: list[HitOut] = Field(default_factory=list)
    more_groups: int = 0
    note: str | None = None
    search: str | None = Field(default=None, description="A search call that lists every hit.")


class DetectResult(BaseModel):
    case: str
    rules_run: int
    results: list[RuleHits] = Field(description="Rules with hits, by severity and first time.")
    no_hits: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class TimeBucket(BaseModel):
    ts: str
    count: int
    errors: int = Field(description="Records of level E or F.")


class GapOut(BaseModel):
    before: str = Field(description="Last record before the gap.")
    after: str = Field(description="First record after it.")
    from_ts: str
    to_ts: str
    seconds: float


class BurstOut(BaseModel):
    from_ts: str
    to_ts: str
    errors: int
    first: str = Field(description="First E/F record of the burst.")
    top_tag: str | None = None


class SegmentOut(BaseModel):
    source: str
    seg: int
    reason: str
    first: str
    ts: str | None = None
    records: int


class OverviewResult(BaseModel):
    case: str
    sources: int
    records: int
    ts_from: str | None = None
    ts_to: str | None = None
    mono_from: str | None = Field(default=None, description="Records with only a monotonic clock: first.")
    mono_to: str | None = None
    levels: dict[str, int] = Field(default_factory=dict, description="Records per level ('-': unknown).")
    top_tags: list[CountGroup] = Field(default_factory=list)
    top_processes: list[CountGroup] = Field(default_factory=list)
    top_templates: list[CountGroup] = Field(default_factory=list)
    segments: list[SegmentOut] = Field(default_factory=list, description="Clock restarts, boots, jumps.")
    gaps: list[GapOut] = Field(default_factory=list, description="More than 30 s without a record.")
    gaps_complete: bool = True
    bursts: list[BurstOut] = Field(default_factory=list, description="E/F per 10 s above 5× the median.")
    histogram_step: str | None = None
    histogram: list[TimeBucket] = Field(default_factory=list)
    detect: dict[str, int] = Field(default_factory=dict, description="Detector rules with hits.")
    artifacts: dict[str, int] = Field(default_factory=dict, description="Artifacts per type.")
    exact: bool = True
    warnings: list[str] = Field(default_factory=list)


class CitationIssue(BaseModel):
    line: int = Field(description="Line of the checked text.")
    cite: str | None = None
    message: str


class CheckCitationsResult(BaseModel):
    case: str
    ok: bool = Field(description="True when every citation is well-formed and resolves (warnings allowed).")
    citations: int
    distinct: int
    resolved: int
    paragraphs: int
    uncited: int = Field(description="Paragraphs with a finding but no citation (warnings).")
    errors: list[CitationIssue] = Field(default_factory=list)
    warnings: list[CitationIssue] = Field(default_factory=list)


@dataclass
class Output:
    text: str
    data: BaseModel
