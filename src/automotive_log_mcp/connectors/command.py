"""The ``command`` connector: an external program turns a file into JSON lines (D13, M5).

For formats that are easiest to decode with an existing tool or in another language (a binary
ECU trace, a vendor log), a YAML file in a rule directory names the program; the server runs it
on the file and reads one JSON object per line from its standard output. Example::

    connector: mcu_trace              # name used in list_sources and format=...
    version: "1"                      # bump when the program's output changes (marks sources stale)
    description: MCU trace via mcu2jsonl
    command: ["{python}", "{dir}/mcu2jsonl.py", "{path}"]
    sniff:
      filenames: ["*.mcutrace", "mcu_*.bin"]   # globs on the file name
      magic: "4d435554"                        # optional: hex bytes the file starts with
      head_regex: '^MCU TRACE v\\d'            # optional: regex on the decoded first 64 KiB
      score: 0.9                               # score when every given condition holds
    timeout: 600                      # seconds (default 3600)
    replace: false                    # true to replace a connector with the same name

Placeholders in ``command``: ``{path}`` (the file; a member of an archive is written to a
temporary file first), ``{python}`` (the interpreter running the server), ``{dir}`` (the YAML
file's directory), ``{name}`` (the source's display name). Without ``{path}`` the file is
written to the program's standard input. Without any ``sniff`` condition the connector is used
only when forced with ``format=<name>``.

Output lines (UTF-8 JSON objects; anything else becomes an unparsed record, never a failed
import):

- a record: ``msg`` (required), ``ts`` (wall clock as the device writes it: ``"2026-01-01
  08:00:01.123"`` or ns since the epoch as an integer), ``mono`` (monotonic clock: seconds
  since boot as ``"123.456789"`` or a number, or ``mono_ns``), ``ts_raw``, ``level`` (V D I W E F
  or a name such as ``error``), ``tag``, ``pid``, ``tid``, ``attrs`` (object of scalars),
  ``stream`` (sub-source, e.g. a core or channel), ``boot`` (true at a boot marker), ``lines``
  (physical lines it stands for);
- ``{"type": "artifact", "artifact_type": "coredump", "headline": ..., "summary": ...,
  "sections": [{"name": "info", "lines": [...]}], "ts": ..., "pid": ..., "process": ...,
  "signature": ..., "level": "F", "fields": {...}}``: an artifact (read with read_artifact);
- ``{"type": "meta", "values": {"timezone": "Europe/Berlin", "ecu": "MCU1"}}``: source metadata
  (``timezone`` is used by the clock alignment).

A program that exits with a non-zero status fails the import with the end of its standard error.
Everything it outputs is redacted like any other source (D5).
"""

from __future__ import annotations

import contextlib
import fnmatch
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO

from ..core.levels import normalize
from ..core.timeparse import NS, seconds_ns, wall_ns
from ..model import ArtifactSection, RawArtifact, RawRecord
from .base import ConnectorError, ParseItem, SourceContext

_KEYS = {"connector", "version", "description", "command", "sniff", "timeout", "replace"}
_SNIFF_KEYS = {"filenames", "magic", "head_regex", "score"}
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_WALL = re.compile(r"(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d):(\d\d)(?:[.,](\d{1,9}))?")
_SECTION_RE = re.compile(r"[^a-z0-9_.-]+")
STDERR_TAIL = 2000


def _err(path: Path, msg: str) -> ConnectorError:
    return ConnectorError(f"command connector {path}: {msg}")


class CommandConnector:
    def __init__(self, spec: dict[str, Any], path: Path) -> None:
        unknown = set(spec) - _KEYS
        if unknown:
            raise _err(path, f"unknown keys: {', '.join(sorted(unknown))}")
        name = spec.get("connector")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise _err(path, "'connector' must be a lowercase name ([a-z][a-z0-9_]*)")
        self.name = name
        self.version = str(spec.get("version", "1"))
        self.description = str(spec.get("description", ""))
        self.replace = spec.get("replace", False)
        if not isinstance(self.replace, bool):
            raise _err(path, "'replace' must be true or false")
        self.path = path
        cmd = spec.get("command")
        if isinstance(cmd, str):
            raise _err(path, "'command' must be a list of arguments, e.g. ['{python}', 'tool.py', '{path}']")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(a, str) and a for a in cmd):
            raise _err(path, "'command' (a non-empty list of strings) is required")
        for a in cmd:
            for field in re.findall(r"\{(\w*)\}", a):
                if field not in ("path", "python", "dir", "name"):
                    raise _err(
                        path, f"unknown placeholder {{{field}}} in 'command' (path, python, dir, name)"
                    )
        self.command = list(cmd)
        self.uses_path = any("{path}" in a for a in cmd)
        timeout = spec.get("timeout", 3600)
        if not isinstance(timeout, int | float) or timeout <= 0:
            raise _err(path, "'timeout' must be a positive number of seconds")
        self.timeout = float(timeout)

        sniff = spec.get("sniff") or {}
        if not isinstance(sniff, dict) or set(sniff) - _SNIFF_KEYS:
            raise _err(path, f"'sniff' must be a mapping with keys {sorted(_SNIFF_KEYS)}")
        self.filenames = sniff.get("filenames") or []
        if not isinstance(self.filenames, list) or not all(isinstance(x, str) for x in self.filenames):
            raise _err(path, "'sniff.filenames' must be a list of globs")
        magic = sniff.get("magic")
        try:
            self.magic = bytes.fromhex(magic) if magic else None
        except (ValueError, TypeError):
            raise _err(path, "'sniff.magic' must be hex bytes, e.g. '4d435554'") from None
        head_regex = sniff.get("head_regex")
        try:
            self.head_re = re.compile(head_regex, re.M) if head_regex else None
        except (re.error, TypeError) as e:
            raise _err(path, f"'sniff.head_regex' is not a valid regex: {e}") from None
        score = sniff.get("score", 0.9)
        if not isinstance(score, int | float) or not 0 < score <= 1:
            raise _err(path, "'sniff.score' must be a number in (0, 1]")
        self.score = float(score)

    # -- sniff --------------------------------------------------------------------------------

    def sniff(self, head: bytes, filename: str) -> float:
        if not (self.filenames or self.magic or self.head_re):
            return 0.0
        if self.filenames and not any(fnmatch.fnmatch(filename, g) for g in self.filenames):
            return 0.0
        if self.magic is not None and not head.startswith(self.magic):
            return 0.0
        if self.head_re is not None and not self.head_re.search(head.decode("utf-8", errors="replace")):
            return 0.0
        return self.score

    # -- parse --------------------------------------------------------------------------------

    def _argv(self, path: str | None, ctx: SourceContext) -> list[str]:
        values = {
            "python": sys.executable,
            "dir": str(self.path.parent),
            "name": ctx.name,
            "path": path or "",
        }
        out = []
        for a in self.command:
            for k, v in values.items():
                a = a.replace("{" + k + "}", v)
            out.append(a)
        exe = shutil.which(out[0]) if not os.path.isabs(out[0]) else out[0]
        if exe is None and not Path(out[0]).exists():
            raise ConnectorError(f"connector {self.name}: program {out[0]!r} not found ({self.path})")
        return out

    def parse(self, stream: BinaryIO, ctx: SourceContext) -> Iterator[ParseItem]:
        tmp: str | None = None
        path: str | None = None
        if self.uses_path:
            name = getattr(stream, "name", None)
            if isinstance(name, str) and os.path.isfile(name) and isinstance(stream, io.BufferedReader):
                path = name
            else:
                fd, tmp = tempfile.mkstemp(prefix="alm-", suffix="-" + Path(ctx.filename).name)
                with os.fdopen(fd, "wb") as f:
                    shutil.copyfileobj(stream, f, 1 << 20)
                path = tmp
        try:
            yield from self._run(stream, path, ctx)
        finally:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)

    def _run(self, stream: BinaryIO, path: str | None, ctx: SourceContext) -> Iterator[ParseItem]:
        argv = self._argv(path, ctx)
        env = {**os.environ, "AUTOMOTIVE_LOG_MCP_SOURCE": ctx.name}
        with tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL if self.uses_path else subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=err,
                env=env,
            )
            feeder = None
            if not self.uses_path:
                feeder = threading.Thread(target=_feed, args=(stream, proc.stdin), daemon=True)
                feeder.start()
            killed = threading.Event()

            def kill() -> None:
                killed.set()
                proc.kill()

            timer = threading.Timer(self.timeout, kill)
            timer.start()
            finished = False
            try:
                assert proc.stdout is not None
                out = io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="replace", newline="\n")
                for line in out:
                    line = line.rstrip("\r\n")
                    if line.strip():
                        yield from self._item(line, ctx)
                code = proc.wait()
                finished = True
            finally:
                timer.cancel()
                if not finished:
                    proc.kill()
                    proc.wait()
                if feeder is not None:
                    feeder.join(timeout=5)
            if killed.is_set() or code != 0:
                err.seek(0)
                tail = err.read().decode("utf-8", errors="replace")[-STDERR_TAIL:].strip()
                why = (
                    f"was stopped after {self.timeout:g} s"
                    if killed.is_set()
                    else f"exited with status {code}"
                )
                raise ConnectorError(
                    f"connector {self.name}: {' '.join(argv[:2])} {why}" + (f": {tail}" if tail else "")
                )

    def _item(self, line: str, ctx: SourceContext) -> Iterator[ParseItem]:
        try:
            obj = json.loads(line)
        except ValueError:
            yield RawRecord(msg=line, unparsed=True)
            return
        if not isinstance(obj, dict):
            yield RawRecord(msg=line, unparsed=True)
            return
        kind = obj.get("type")
        if kind == "meta":
            values = obj.get("values")
            if isinstance(values, dict):
                ctx.meta.update(
                    {str(k): v for k, v in values.items() if isinstance(v, str | int | float | bool)}
                )
            return
        if kind == "artifact":
            art = self._artifact(obj)
            if art is None:
                yield RawRecord(msg=line, unparsed=True)
            else:
                yield art
            return
        msg = obj.get("msg")
        if not isinstance(msg, str):
            yield RawRecord(msg=line, unparsed=True)
            return
        ts = _wall(obj.get("ts"))
        mono = _mono(obj)
        stream = obj.get("stream")
        attrs = obj.get("attrs")
        rec = RawRecord(
            msg=msg,
            ts_raw=obj["ts_raw"] if isinstance(obj.get("ts_raw"), str) else _raw(obj.get("ts"), obj),
            ts=ts,
            ts_src=ts if ts is not None else mono,
            level=normalize(str(obj["level"])) if obj.get("level") is not None else None,
            tag=str(obj["tag"]) if obj.get("tag") is not None else None,
            pid=_int(obj.get("pid")),
            tid=_int(obj.get("tid")),
            attrs={str(k): v for k, v in attrs.items() if isinstance(v, str | int | float | bool)}
            if isinstance(attrs, dict)
            else None,
            stream=stream if isinstance(stream, str) and stream else None,
            lines=max(_int(obj.get("lines")) or 1, 1),
            boot=obj.get("boot") is True,
            monotonic=ts is None and mono is not None,
        )
        yield rec

    def _artifact(self, obj: dict[str, Any]) -> RawArtifact | None:
        sections = []
        names: set[str] = set()
        for i, sec in enumerate(obj.get("sections") or []):
            if not isinstance(sec, dict) or not isinstance(sec.get("lines"), list):
                return None
            name = (
                _SECTION_RE.sub("-", str(sec.get("name") or f"part{i + 1}").lower()).strip("-")[:64] or "part"
            )
            base, n = name, 2
            while name in names:
                name, n = f"{base}-{n}", n + 1
            names.add(name)
            sections.append(ArtifactSection(name, [str(x) for x in sec["lines"]]))
        headline = obj.get("headline")
        if not sections or not isinstance(headline, str):
            return None
        atype = obj.get("artifact_type")
        atype = _SECTION_RE.sub("_", str(atype).lower())[:32] if atype else self.name
        fields = obj.get("fields")
        level = normalize(str(obj["level"])) if obj.get("level") is not None else None
        return RawArtifact(
            type=atype,
            sections=sections,
            headline=headline,
            summary=str(obj.get("summary") or headline),
            signature=str(obj["signature"]) if obj.get("signature") else None,
            ts=_wall(obj.get("ts")),
            ts_raw=obj["ts_raw"] if isinstance(obj.get("ts_raw"), str) else None,
            pid=_int(obj.get("pid")),
            tid=_int(obj.get("tid")),
            process=str(obj["process"]) if obj.get("process") else None,
            level=level or "F",
            fields=fields if isinstance(fields, dict) else {},
            consumed=1,
        )


def _feed(stream: BinaryIO, pipe: Any) -> None:
    try:
        shutil.copyfileobj(stream, pipe, 1 << 20)
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        with contextlib.suppress(OSError):
            pipe.close()


def _int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v)
    return None


def _wall(v: Any) -> int | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        m = _WALL.fullmatch(v.strip())
        if m:
            try:
                return wall_ns(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]), int(m[6]), m[7] or "")
            except ValueError:
                return None
    return None


def _mono(obj: dict[str, Any]) -> int | None:
    ns = obj.get("mono_ns")
    if isinstance(ns, int) and not isinstance(ns, bool):
        return ns
    v = obj.get("mono")
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v * NS
    if isinstance(v, float):
        return round(v * NS)
    if isinstance(v, str):
        try:
            return seconds_ns(v)
        except ValueError:
            return None
    return None


def _raw(ts: Any, obj: dict[str, Any]) -> str | None:
    if isinstance(ts, str):
        return ts
    if ts is None and obj.get("mono") is not None:
        return str(obj["mono"])
    return None
