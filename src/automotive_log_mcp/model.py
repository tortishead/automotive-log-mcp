"""Data passed from connectors to the pipeline, and what the store keeps.

Connectors produce :class:`RawRecord` (one log record, possibly merged from several physical
lines) and :class:`RawArtifact` (a document that is not a line stream: tombstone, ANR trace, Java
crash, dropbox entry). The pipeline normalizes, redacts, templates and segments them into stored records. The
stored record model is the design's: ``source_id``, ``seq``, ``ts_raw``, ``ts``, ``level``,
``tag``, ``pid``, ``tid``, ``msg``, ``template_id``, ``attrs``; plus ``ts_src`` (the timestamp in
the source's own clock, e.g. kernel monotonic time) and ``seg`` (segment number).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Record ids pack the citation: id = source_id << SEQ_BITS | seq. A source holds up to 2^40 records.
SEQ_BITS = 40
SEQ_MASK = (1 << SEQ_BITS) - 1

LEVELS = ("V", "D", "I", "W", "E", "F")


def record_id(source_id: int, seq: int) -> int:
    return (source_id << SEQ_BITS) | seq


def split_record_id(rid: int) -> tuple[int, int]:
    return rid >> SEQ_BITS, rid & SEQ_MASK


def citation(source_id: int, seq: int) -> str:
    return f"log:{source_id}#{seq}"


@dataclass(slots=True)
class RawRecord:
    """One record as a connector parsed it, before redaction.

    ``ts`` is wall-clock time in ns since the epoch as written in the log (no time-zone
    conversion; the source's zone is kept in its metadata). ``ts_src`` is the timestamp in the
    source's own clock: equal to ``ts`` for wall-clock logs, monotonic ns for kernel logs.
    ``stream`` routes the record to a sub-source (e.g. logcat buffer 'main', 'crash'); ``None``
    means the default stream of the member. ``boot`` marks a boot marker (new segment).
    ``unparsed`` marks a line the connector could not parse; ``msg`` then holds the raw text.
    """

    msg: str
    ts_raw: str | None = None
    ts: int | None = None
    ts_src: int | None = None
    level: str | None = None
    tag: str | None = None
    pid: int | None = None
    tid: int | None = None
    attrs: dict[str, Any] | None = None
    stream: str | None = None
    lines: int = 1
    boot: bool = False
    unparsed: bool = False
    monotonic: bool = False
    artifact: RawArtifact | None = None  # this record *is* an artifact (e.g. a crash-buffer crash)


@dataclass(slots=True)
class StreamMarker:
    """Emitted by a connector when the following records belong to another stream (buffer).

    ``join``: the following records belong to the current stream *or* ``stream`` (logcat without
    ``--dividers`` prints ``beginning of <buffer>`` once per buffer, after which buffers interleave
    with no further marker); the current source is renamed ``<current>+<stream>`` instead of a new
    source being started. ``switch``: the connector saw an explicit switch (``switch to <buffer>``).
    """

    stream: str
    join: bool = False
    switch: bool = False


@dataclass(slots=True)
class SourceMeta:
    """Metadata a container learns about its members (fingerprint, time zone, ...)."""

    values: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


@dataclass(slots=True)
class ProcessInfo:
    pid: int
    name: str
    tid: int | None = None
    ppid: int | None = None
    user: str | None = None


@dataclass(slots=True)
class ArtifactSection:
    """A named part of an artifact's text. Names are unique within the artifact and contain only
    ``[a-z0-9_.-]``, so they can appear in citations (``art:<id>/<section>:L<line>``)."""

    name: str
    lines: list[str]


@dataclass(slots=True)
class RawArtifact:
    """One artifact as a parser produced it, before redaction.

    ``sections`` cover the artifact's text in order and without gaps, so line ``n`` of the
    artifact (``art:<id>:L<n>``, 1-based over all sections) is line ``n`` of the original text.
    ``headline`` is the one-line message of the artifact's record on the timeline; ``summary`` is
    a few lines for ``read_artifact``; ``signature`` groups artifacts of the same crash.

    A connector yields a ``RawArtifact`` on its own (the pipeline then adds one synthetic record
    for it in the current source) or attaches it to a :class:`RawRecord` (``RawRecord.artifact``)
    that already is the artifact's line, e.g. a ``FATAL EXCEPTION`` in the crash buffer.
    ``consumed`` is the number of physical lines read for it (the synthetic record's ``lines``).
    """

    type: str
    sections: list[ArtifactSection]
    headline: str
    summary: str
    signature: str | None = None
    ts: int | None = None
    ts_raw: str | None = None
    pid: int | None = None
    tid: int | None = None
    process: str | None = None
    level: str = "F"
    fields: dict[str, Any] = field(default_factory=dict)
    consumed: int = 0

    @property
    def line_count(self) -> int:
        return sum(len(s.lines) for s in self.sections)
