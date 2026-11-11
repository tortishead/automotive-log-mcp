"""The read tools: argument resolution, queries, compact text and structured output.

``server.py`` registers these with MCP and turns :class:`ToolInputError` into tool errors; the
CLI uses the same methods, so both print the same lines. Each method returns an :class:`Output`:
the text the agent reads (capped by ``config.caps``) and a pydantic model for structured content
holding the same items.

Layout, from the bottom up (a module imports only those above it):

- :mod:`.models`: argument and result models, :class:`Output`, :class:`ToolInputError`;
- :mod:`.timespec`: the ``from``/``to`` forms, durations and citations;
- :mod:`.text`: small text helpers;
- :mod:`.clock`, :mod:`.diff`: ``timeline``/``align`` and ``diff``, on top of a :class:`ToolBase`;
- :mod:`.base`: :class:`ToolBase`, one call's case view, record lines, filters and times;
- one module per tool family (:mod:`.search`, :mod:`.records`, :mod:`.artifacts`, :mod:`.count`,
  :mod:`.detect`, :mod:`.overview`, :mod:`.citecheck`), each a mixin over :class:`ToolBase`.

Filter times (``from``/``to``) accept every timestamp the tools print, plus relative times and
citations; see :meth:`ToolBase.resolve_time`.
"""

from __future__ import annotations

from .artifacts import ArtifactTools
from .base import CaseCtx, ToolBase
from .citecheck import CitationTools
from .count import CountTools
from .detect import DetectTools
from .models import (
    CaseNotFoundError,
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
    RecordOut,
    SearchResult,
    ToolInputError,
)
from .overview import OverviewTools
from .records import RecordTools
from .search import SearchTools
from .text import looks_like_regex

__all__ = [
    "CaseCtx",
    "CaseNotFoundError",
    "CheckCitationsResult",
    "ContextResult",
    "CountResult",
    "DetectResult",
    "DiffSide",
    "Filter",
    "GetRecordsResult",
    "ListArtifactsResult",
    "Output",
    "OverviewResult",
    "ReadArtifactResult",
    "RecordOut",
    "SearchResult",
    "ToolBase",
    "ToolInputError",
    "Tools",
    "looks_like_regex",
]


class Tools(
    SearchTools,
    RecordTools,
    ArtifactTools,
    CountTools,
    DetectTools,
    OverviewTools,
    CitationTools,
):
    """Every read tool over one :class:`~automotive_log_mcp.app.App`."""
