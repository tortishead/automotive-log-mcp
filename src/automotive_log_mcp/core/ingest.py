"""Ingest pipeline: sniff -> container -> connector -> levels -> redact -> templates -> segments -> writer.

``Ingestor.add`` indexes one original file (or each file of a directory) into a case DB. The
original is referenced by absolute path and SHA-256, never copied (D7). Adding a file whose hash
is already in the case is a no-op that returns the existing sources; ``force`` reindexes.

Redaction runs before templating, so no raw value reaches ``records`` or ``templates`` (D5).
Unparseable lines become records with ``level=None`` and the raw (redacted) text.

:class:`CaseWorkers` runs imports in one background thread per case (D10); queries use their own
connections and see the committed prefix of a running import.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import queue
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import Config
from ..connectors.base import (
    SNIFF_BYTES,
    Connector,
    ConnectorError,
    Container,
    ContainerContext,
    Member,
    Registry,
    SourceContext,
    TextConnector,
    is_container,
)
from ..model import RawRecord, StreamMarker, record_id
from ..policy import Policy
from ..store import db
from .redact import Redactor
from .segments import SegmentTracker
from .templates import mask

Progress = Callable[[str], None]


class IngestError(ValueError):
    """The file cannot be added (missing, blocked by policy, unknown format, ...)."""


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class FileResult:
    path: str
    sha256: str
    source_ids: list[int]
    existing: bool = False  # the file was already in the case; nothing was indexed
    notes: list[str] = field(default_factory=list)
    records: int = 0
    lines: int = 0
    seconds: float = 0.0


@dataclass
class AddResult:
    files: list[FileResult] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def source_ids(self) -> list[int]:
        return [sid for f in self.files for sid in f.source_ids]


class _Source:
    """Write state of one source while its member is parsed."""

    __slots__ = (
        "id",
        "lines",
        "meta",
        "name",
        "records",
        "segs",
        "seq",
        "ts_max",
        "ts_min",
        "ts_src_max",
        "ts_src_min",
        "unparsed",
    )

    def __init__(self, sid: int, name: str, meta: dict[str, Any]) -> None:
        self.id = sid
        self.name = name
        self.meta = meta
        self.seq = 0
        self.records = 0
        self.lines = 0
        self.unparsed = 0
        self.ts_min: int | None = None
        self.ts_max: int | None = None
        self.ts_src_min: int | None = None
        self.ts_src_max: int | None = None
        self.segs = SegmentTracker()


@dataclass
class _Origin:
    path: Path
    sha256: str
    size: int
    created: list[tuple[int, dict[str, Any], list[dict[str, Any]]]] = field(default_factory=list)
    processes: list[Any] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    records: int = 0
    lines: int = 0


class Ingestor:
    def __init__(
        self,
        config: Config,
        policy: Policy,
        registry: Registry,
        redactor: Redactor,
        progress: Progress | None = None,
    ) -> None:
        self.config = config
        self.policy = policy
        self.registry = registry
        self.redactor = redactor
        self.progress = progress

    # -- checks that can run before the background job ---------------------------------------

    def check(self, path_str: str, format: str | None) -> Path:
        path = Path(path_str).expanduser()
        try:
            path = path.resolve(strict=True)
        except OSError:
            raise IngestError(f"no such file or directory: {path_str}") from None
        if self.policy.source_blocked(str(path), path.name, path.as_posix()):
            raise IngestError(f"{path} is blocked by the policy (blocked_sources)")
        if format is not None:
            try:
                self.registry.get(format)
            except ConnectorError as e:
                raise IngestError(str(e)) from None
        if not path.is_file() and not path.is_dir():
            raise IngestError(f"{path} is not a regular file or directory")
        return path

    # -- import ---------------------------------------------------------------------------------

    def add(self, case_db: Path, path_str: str, format: str | None = None, force: bool = False) -> AddResult:
        path = self.check(path_str, format)
        result = AddResult()
        if path.is_dir():
            for p in sorted(x for x in path.rglob("*") if x.is_file()):
                if self.policy.source_blocked(str(p), p.name, p.as_posix()):
                    result.skipped.append(f"{p}: blocked by policy")
                    continue
                try:
                    result.files.append(self._add_file(case_db, p, format, force, optional=True))
                except IngestError as e:
                    result.skipped.append(f"{p}: {e}")
            if not result.files:
                raise IngestError(f"no indexable files in {path}: " + "; ".join(result.skipped[:5]))
            return result
        result.files.append(self._add_file(case_db, path, format, force, optional=False))
        return result

    def reindex(self, case_db: Path, source_id: int) -> AddResult:
        with db.opened(case_db) as conn:
            row = conn.execute("SELECT path, sha256 FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            raise IngestError(f"no source {source_id} in this case")
        path, sha = row
        if not Path(path).is_file():
            raise IngestError(
                f"the original of source {source_id} is missing: {path}. "
                f"Use 'relink <case> {source_id} <new path>' first."
            )
        if file_sha256(Path(path)) != sha:
            raise IngestError(f"{path} has changed since it was indexed (hash differs); add it as a new file")
        return AddResult(files=[self._add_file(case_db, Path(path), None, True, optional=False)])

    def _say(self, msg: str) -> None:
        if self.progress is not None:
            self.progress(msg)

    def _add_file(
        self, case_db: Path, path: Path, format: str | None, force: bool, optional: bool
    ) -> FileResult:
        start = datetime.now(UTC)
        size = path.stat().st_size
        self._say(f"hashing {path} ({size / 1e6:.1f} MB)")
        sha = file_sha256(path)
        conn = db.connect(case_db, create=True)
        try:
            existing = conn.execute(
                "SELECT id, status FROM sources WHERE sha256 = ? ORDER BY id", (sha,)
            ).fetchall()
            if existing and not force and all(st == "done" for _, st in existing):
                return FileResult(str(path), sha, [sid for sid, _ in existing], existing=True)
            if existing:
                db.delete_sources(conn, [sid for sid, _ in existing])
                with conn:
                    conn.execute("DELETE FROM processes WHERE sha256 = ?", (sha,))

            origin = _Origin(path, sha, size)
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
            root = Member(
                name=path.name,
                open=lambda: path.open("rb"),
                format=format,
                ref_year=mtime.year,
                ref_month=mtime.month,
                optional=optional,
            )
            writer = db.BatchWriter(conn)
            try:
                self._process(conn, writer, origin, root, [], top=True)
                writer.flush()
            except BaseException as e:
                writer.discard()
                if conn.in_transaction:
                    conn.rollback()
                self._fail(conn, origin, e)
                raise
            self._finish(conn, origin)
        finally:
            conn.close()
        secs = (datetime.now(UTC) - start).total_seconds()
        ids = [sid for sid, _, _ in origin.created]
        self._say(f"{path.name}: {origin.records} records from {origin.lines} lines in {secs:.1f} s")
        return FileResult(
            str(path), sha, ids, notes=origin.notes, records=origin.records, lines=origin.lines, seconds=secs
        )

    def _fail(self, conn: Any, origin: _Origin, e: BaseException) -> None:
        ids = [sid for sid, _, _ in origin.created]
        if not ids:
            return
        # Keep the source rows (status failed, with the error) but not their partial records.
        msg = self.redactor.redact(str(e) or type(e).__name__)
        db.delete_sources(conn, ids)
        with conn:
            for sid, meta, _ in origin.created:
                conn.execute(
                    "INSERT INTO sources(id, name, format, connector_version, redaction_hash, path, sha256, "
                    "size, member, stream, status, error, added_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'failed', ?, ?)",
                    (
                        sid,
                        meta["_name"],
                        meta["_format"],
                        meta["_version"],
                        self.redactor.rules_hash,
                        str(origin.path),
                        origin.sha256,
                        origin.size,
                        meta["_member"],
                        meta["_stream"],
                        msg,
                        _now(),
                    ),
                )

    def _finish(self, conn: Any, origin: _Origin) -> None:
        """Apply container metadata (known only at the end, e.g. properties) and the process list."""
        with conn:
            for sid, _member_meta, chain in origin.created:
                merged: dict[str, Any] = {}
                for d in chain:
                    merged.update({k: v for k, v in d.items() if not k.startswith("_")})
                merged = self._redact_value(merged)
                conn.execute(
                    "UPDATE sources SET meta = ?, fingerprint = ? WHERE id = ?",
                    (db.dumps(merged), merged.get("fingerprint"), sid),
                )
            if origin.processes:
                conn.executemany(
                    "INSERT INTO processes(sha256, pid, tid, ppid, user, name) VALUES (?,?,?,?,?,?)",
                    [
                        (origin.sha256, p.pid, p.tid, p.ppid, p.user, self.redactor.redact(p.name))
                        for p in origin.processes
                    ],
                )

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redactor.redact(value)
        if isinstance(value, dict):
            return {k: self._redact_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact_value(v) for v in value]
        return value

    # -- members --------------------------------------------------------------------------------

    def _head(self, member: Member) -> bytes:
        assert member.open is not None
        with member.open() as f:
            return f.read(SNIFF_BYTES)

    def _process(
        self,
        conn: Any,
        writer: db.BatchWriter,
        origin: _Origin,
        member: Member,
        chain: list[dict[str, Any]],
        top: bool = False,
    ) -> None:
        inner = member.name.split("!", 1)[1] if "!" in member.name else ""
        if not top and self.policy.source_blocked(
            member.name, inner, posixpath.basename(inner or member.name)
        ):
            origin.notes.append(f"{member.name}: blocked by policy")
            return
        filename = posixpath.basename(member.name.split("#", 1)[0].rsplit("!", 1)[-1])
        if member.format is not None:
            plugin = self.registry.get(member.format)
        else:
            if member.lines is not None:
                raise IngestError(f"{member.name}: section without a format")
            head = self._head(member)
            if not head:
                origin.notes.append(f"{member.name}: empty, skipped")
                return
            try:
                plugin = self.registry.detect(head, filename)
            except ConnectorError as e:
                if member.optional and not top:
                    origin.notes.append(f"{member.name}: unknown format, skipped")
                    return
                raise IngestError(str(e)) from None
        if is_container(plugin):
            assert isinstance(plugin, Container)
            if member.lines is not None:
                raise IngestError(f"{member.name}: a container cannot read a section")
            cctx = ContainerContext(
                name=member.name,
                filename=filename,
                ref_year=member.ref_year or datetime.now().year,
                ref_month=member.ref_month or 12,
            )
            self._say(f"{member.name}: {plugin.name}")
            for sub in plugin.expand(member, cctx):
                if sub.ref_year is None:
                    sub.ref_year, sub.ref_month = cctx.ref_year, cctx.ref_month
                self._process(conn, writer, origin, sub, [*chain, cctx.meta])
            origin.processes.extend(cctx.processes)
            origin.notes.extend(cctx.notes)
            return
        assert isinstance(plugin, Connector)
        self._parse(conn, writer, origin, member, plugin, chain, filename)

    def _parse(
        self,
        conn: Any,
        writer: db.BatchWriter,
        origin: _Origin,
        member: Member,
        connector: Connector,
        chain: list[dict[str, Any]],
        filename: str,
    ) -> None:
        container_meta: dict[str, Any] = {}
        for d in chain:
            container_meta.update(d)
        ctx = SourceContext(
            name=member.name,
            filename=filename,
            ref_year=member.ref_year or datetime.now().year,
            ref_month=member.ref_month or 12,
            meta={k: v for k, v in container_meta.items() if k in ("boot_time_ns",)},
        )
        sources: dict[str | None, _Source] = {}
        version = getattr(connector, "version", "1")

        def get(stream: str | None) -> _Source:
            st = sources.get(stream)
            if st is not None:
                return st
            name = member.name + (f":{stream}" if stream else "")
            member_part = member.name.split("!", 1)[1] if "!" in member.name else None
            if "#" in member.name and member_part is None:
                member_part = member.name.split("#", 1)[1]
            with conn:
                cur = conn.execute(
                    "INSERT INTO sources(name, format, connector_version, redaction_hash, path, sha256, "
                    "size, member, stream, status, added_at) VALUES (?,?,?,?,?,?,?,?,?,'indexing',?)",
                    (
                        name,
                        connector.name,
                        version,
                        self.redactor.rules_hash,
                        str(origin.path),
                        origin.sha256,
                        origin.size,
                        member_part,
                        stream,
                        _now(),
                    ),
                )
            sid = int(cur.lastrowid or 0)
            internal = {
                "_name": name,
                "_format": connector.name,
                "_version": version,
                "_member": member_part,
                "_stream": stream,
            }
            st = _Source(sid, name, internal)
            sources[stream] = st
            origin.created.append((sid, internal, [*chain, ctx.meta]))
            return st

        def progress() -> list[db.Statement]:
            return [
                (
                    "UPDATE sources SET records = ?, lines = ?, unparsed = ?, ts_min = ?, ts_max = ?, "
                    "ts_src_min = ?, ts_src_max = ? WHERE id = ?",
                    (
                        st.records,
                        st.lines,
                        st.unparsed,
                        st.ts_min,
                        st.ts_max,
                        st.ts_src_min,
                        st.ts_src_max,
                        st.id,
                    ),
                )
                for st in sources.values()
            ]

        writer.on_flush.append(progress)
        try:
            if member.lines is not None:
                if not isinstance(connector, TextConnector):
                    raise IngestError(f"{member.name}: connector {connector.name} cannot read text sections")
                items = connector.parse_lines(member.lines, ctx)
                self._consume(items, get, member.stream, writer)
            else:
                assert member.open is not None
                with member.open() as stream:
                    self._consume(connector.parse(stream, ctx), get, member.stream, writer)
            if not sources:
                get(member.stream)  # an empty log is still a source
            writer.flush()
        finally:
            writer.on_flush.remove(progress)
        with conn:
            for sql, params in progress():
                conn.execute(sql, params)
            for st in sources.values():
                conn.executemany(
                    "INSERT INTO segments(source_id, seg, reason, first_seq, last_seq, count, ts_first, "
                    "ts_last, ts_src_first, ts_src_last) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            st.id,
                            s.seg,
                            s.reason,
                            s.first_seq,
                            s.last_seq,
                            s.count,
                            s.ts_first,
                            s.ts_last,
                            s.ts_src_first,
                            s.ts_src_last,
                        )
                        for s in st.segs.segments
                    ],
                )
                conn.execute(
                    "UPDATE sources SET status = 'done', finished_at = ? WHERE id = ?", (_now(), st.id)
                )
                origin.records += st.records
                origin.lines += st.lines
        self._say(
            f"{member.name}: {sum(s.records for s in sources.values())} records in {len(sources)} source(s)"
        )

    def _consume(
        self,
        items: Iterator[RawRecord | StreamMarker],
        get: Callable[[str | None], _Source],
        default_stream: str | None,
        writer: db.BatchWriter,
    ) -> None:
        redact = self.redactor.redact
        redact_tag = self.redactor.redact_cached
        template_id = writer.template_id
        tag_ids = writer.tags
        add = writer.add
        dumps = db.dumps
        cur: _Source | None = None  # created on the first record, so a leading marker leaves no empty source
        for item in items:
            if type(item) is StreamMarker:
                cur = get(item.stream)
                continue
            rec: RawRecord = item  # type: ignore[assignment]
            if rec.stream is not None:
                st = get(rec.stream)
            else:
                if cur is None:
                    cur = get(default_stream)
                st = cur
            st.seq += 1
            seq = st.seq
            msg = redact(rec.msg)
            tag = rec.tag
            tag_id = None
            if tag is not None:
                tag = redact_tag(tag)
                tag_id = tag_ids.get(tag)
                if tag_id is None:
                    tag_id = writer.tag_id(tag)
            attrs = rec.attrs
            attrs_json = None
            if attrs:
                attrs_json = dumps({k: redact(v) if isinstance(v, str) else v for k, v in attrs.items()})
            ts = rec.ts
            ts_src = rec.ts_src
            seg = st.segs.observe(seq, ts, ts_src, rec.monotonic, rec.boot)
            if ts is not None:
                if st.ts_min is None or ts < st.ts_min:
                    st.ts_min = ts
                if st.ts_max is None or ts > st.ts_max:
                    st.ts_max = ts
            if ts_src is not None:
                if st.ts_src_min is None or ts_src < st.ts_src_min:
                    st.ts_src_min = ts_src
                if st.ts_src_max is None or ts_src > st.ts_src_max:
                    st.ts_src_max = ts_src
            st.records += 1
            st.lines += rec.lines
            if rec.unparsed:
                st.unparsed += 1
            add(
                (
                    record_id(st.id, seq),
                    ts,
                    None if ts_src == ts else ts_src,
                    rec.ts_raw,
                    rec.level,
                    tag_id,
                    rec.pid,
                    rec.tid,
                    msg,
                    template_id(mask(msg)),
                    seg,
                    attrs_json,
                ),
                tag,
            )


# -- background indexing (D10) ----------------------------------------------------------------


@dataclass
class _Job:
    case_db: Path
    path: str
    format: str | None
    force: bool
    future: Future[AddResult]


class CaseWorker:
    """One daemon thread that runs the imports of one case in order."""

    def __init__(self, ingestor: Ingestor, name: str) -> None:
        self.ingestor = ingestor
        self._queue: queue.Queue[_Job] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name=f"index-{name}", daemon=True)
        self._thread.start()
        self.current: str | None = None

    def submit(self, case_db: Path, path: str, format: str | None, force: bool) -> Future[AddResult]:
        fut: Future[AddResult] = Future()
        self._queue.put(_Job(case_db, path, format, force, fut))
        return fut

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if not job.future.set_running_or_notify_cancel():
                continue
            self.current = job.path
            try:
                job.future.set_result(self.ingestor.add(job.case_db, job.path, job.format, job.force))
            except BaseException as e:
                job.future.set_exception(e)
            finally:
                self.current = None


class CaseWorkers:
    def __init__(self, ingestor: Ingestor) -> None:
        self.ingestor = ingestor
        self._workers: dict[str, CaseWorker] = {}
        self._lock = threading.Lock()

    def submit(
        self, case: str, case_db: Path, path: str, format: str | None, force: bool = False
    ) -> Future[AddResult]:
        with self._lock:
            w = self._workers.get(case)
            if w is None:
                w = self._workers[case] = CaseWorker(self.ingestor, case)
        return w.submit(case_db, path, format, force)

    def busy(self, case: str) -> bool:
        w = self._workers.get(case)
        return w is not None and w.current is not None


def json_meta(text: str | None) -> dict[str, Any]:
    return json.loads(text) if text else {}


__all__ = ["AddResult", "CaseWorkers", "FileResult", "IngestError", "Ingestor", "file_sha256", "json_meta"]
