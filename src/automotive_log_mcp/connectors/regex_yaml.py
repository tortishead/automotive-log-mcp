"""Config-driven text connectors: one YAML file describes a line format with a regex (D13).

Example (``syslog.yaml`` ships in the package)::

    connector: syslog                 # name used in list_sources and format=...
    version: "1"                      # bump when the definition changes (marks sources stale)
    description: BSD syslog / journalctl short output
    sniff:
      filenames: ["syslog*", "messages*"]   # optional: +0.05 when the file name matches
      min_fraction: 0.6                     # share of head lines that must match `line`
    line: '^(?P<ts>\\w{3} [ \\d]\\d \\d\\d:\\d\\d:\\d\\d) (?P<host>\\S+)
           (?P<tag>[^\\s\\[:]+)(?:\\[(?P<pid>\\d+)\\])?: (?P<msg>.*)$'   # (one line)
    continuation: '^\\s'              # optional: such lines are appended to the previous record
    timestamp:
      format: '%b %d %H:%M:%S'        # strptime format, or epoch | epoch_ms | epoch_us | monotonic
    levels: {err: E, warning: W}      # optional: raw level -> V D I W E F
    attrs: [host]                     # optional: other groups to keep (default: all other groups)
    replace: false                    # true to replace a connector with the same name

Named groups: ``msg`` (required), ``ts``, ``level``, ``tag``, ``pid``, ``tid``; any other group
goes to ``attrs``. A format without a year gets one by the same rule as logcat (New Year
increments the year). Unknown keys and invalid regexes are errors.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import ConfigError, read_yaml_mapping
from ..core.levels import normalize
from ..core.timeparse import NS, YearTracker, seconds_ns, wall_ns
from ..model import RawRecord
from .base import ConnectorError, ParseItem, Registry, SourceContext, TextConnector, head_lines
from .command import CommandConnector

BUILTIN_DIR = Path(__file__).resolve().parent.parent / "rules"

_KEYS = {"connector", "version", "description", "sniff", "line", "continuation", "timestamp", "levels"}
_KEYS |= {"attrs", "replace"}
_SNIFF_KEYS = {"filenames", "min_fraction"}
_TS_KEYS = {"format"}
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SPECIAL = {"ts", "level", "tag", "pid", "tid", "msg"}
_EPOCH = {"epoch": NS, "epoch_ms": 1_000_000, "epoch_us": 1000}


def _err(path: Path, msg: str) -> ConnectorError:
    return ConnectorError(f"regex connector {path}: {msg}")


class RegexConnector(TextConnector):
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

        line = spec.get("line")
        if not isinstance(line, str):
            raise _err(path, "'line' (a regex with a named group 'msg') is required")
        try:
            self.line_re = re.compile(line)
        except re.error as e:
            raise _err(path, f"'line' is not a valid regex: {e}") from e
        groups = set(self.line_re.groupindex)
        if "msg" not in groups:
            raise _err(path, "'line' must have a named group 'msg'")

        cont = spec.get("continuation")
        try:
            self.cont_re = re.compile(cont) if isinstance(cont, str) else None
        except re.error as e:
            raise _err(path, f"'continuation' is not a valid regex: {e}") from e
        if cont is not None and not isinstance(cont, str):
            raise _err(path, "'continuation' must be a regex string")

        sniff = spec.get("sniff") or {}
        if not isinstance(sniff, dict) or set(sniff) - _SNIFF_KEYS:
            raise _err(path, f"'sniff' must be a mapping with keys {sorted(_SNIFF_KEYS)}")
        self.filenames = sniff.get("filenames") or []
        if not isinstance(self.filenames, list) or not all(isinstance(x, str) for x in self.filenames):
            raise _err(path, "'sniff.filenames' must be a list of globs")
        self.min_fraction = sniff.get("min_fraction", 0.6)
        if not isinstance(self.min_fraction, int | float) or not 0 < self.min_fraction <= 1:
            raise _err(path, "'sniff.min_fraction' must be a number in (0, 1]")

        ts = spec.get("timestamp") or {}
        if not isinstance(ts, dict) or set(ts) - _TS_KEYS:
            raise _err(path, "'timestamp' must be a mapping with key 'format'")
        self.ts_format: str | None = ts.get("format")
        if self.ts_format is not None and not isinstance(self.ts_format, str):
            raise _err(path, "'timestamp.format' must be a string")
        if "ts" in groups and self.ts_format is None:
            raise _err(path, "a 'ts' group needs 'timestamp.format'")
        fmt = self.ts_format or ""
        self.monotonic = fmt == "monotonic"
        self.has_year = "%Y" in fmt or "%y" in fmt or fmt in _EPOCH or self.monotonic

        levels = spec.get("levels") or {}
        if not isinstance(levels, dict):
            raise _err(path, "'levels' must be a mapping")
        self.levels: dict[str, str] = {}
        for raw, lvl in levels.items():
            norm = normalize(str(lvl))
            if norm is None:
                raise _err(path, f"level {lvl!r} for {raw!r} is not one of V D I W E F")
            self.levels[str(raw)] = norm

        attrs = spec.get("attrs")
        others = groups - _SPECIAL
        if attrs is None:
            self.attrs = sorted(others)
        elif isinstance(attrs, list) and set(attrs) <= others:
            self.attrs = list(attrs)
        else:
            raise _err(path, f"'attrs' must list named groups of 'line' (available: {sorted(others)})")

    def sniff(self, head: bytes, filename: str) -> float:
        lines = [ln for ln in head_lines(head, 60) if ln.strip()]
        if not lines:
            return 0.0
        hits = sum(1 for ln in lines if self.line_re.match(ln))
        frac = hits / len(lines)
        if frac < self.min_fraction:
            return 0.0
        score = 0.5 + 0.4 * frac
        if any(fnmatch.fnmatch(filename, g) for g in self.filenames):
            score += 0.05
        return min(score, 0.95)

    def _ts(self, raw: str, years: YearTracker) -> int:
        fmt = self.ts_format
        assert fmt is not None
        if fmt in _EPOCH:
            return int(raw) * _EPOCH[fmt] if "." not in raw else seconds_ns(raw) * _EPOCH[fmt] // NS
        if fmt == "monotonic":
            return seconds_ns(raw)
        if "." in raw and "%f" not in fmt:
            fmt += ".%f"  # tolerate an added fraction (e.g. journalctl -o short-precise)
        if self.has_year:
            dt = datetime.strptime(raw, fmt)
            return wall_ns(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second) + dt.microsecond * 1000
        dt = datetime.strptime(f"{raw} 2000", f"{fmt} %Y")  # 2000 is a leap year: Feb 29 parses
        year = years.year_for(dt.month)
        return wall_ns(year, dt.month, dt.day, dt.hour, dt.minute, dt.second) + dt.microsecond * 1000

    def parse_lines(self, lines: Iterator[str], ctx: SourceContext) -> Iterator[ParseItem]:
        years = YearTracker(ctx.ref_year, ctx.ref_month)
        pending: RawRecord | None = None
        for line in lines:
            m = self.line_re.match(line)
            if m is None:
                if pending is not None and self.cont_re is not None and self.cont_re.match(line):
                    pending.msg += "\n" + line
                    pending.lines += 1
                    continue
                if pending is not None:
                    yield pending
                    pending = None
                yield RawRecord(msg=line, unparsed=True)
                continue
            if pending is not None:
                yield pending
            g = m.groupdict()
            ts = ts_src = None
            ts_raw = g.get("ts")
            if ts_raw:
                try:
                    ts_src = self._ts(ts_raw, years)
                    ts = None if self.monotonic else ts_src
                except ValueError:
                    ts_src = None
            lvl = g.get("level")
            level = self.levels.get(lvl) if lvl in self.levels else normalize(lvl) if lvl else None
            attrs = {k: g[k] for k in self.attrs if g.get(k) is not None} or None
            pending = RawRecord(
                msg=g["msg"] or "",
                ts_raw=ts_raw,
                ts=ts,
                ts_src=ts_src,
                level=level,
                tag=g.get("tag"),
                pid=int(g["pid"]) if g.get("pid") else None,
                tid=int(g["tid"]) if g.get("tid") else None,
                attrs=attrs,
                monotonic=self.monotonic,
            )
        if pending is not None:
            yield pending


def load_file(path: Path) -> RegexConnector | CommandConnector | None:
    """Load one YAML file; None if it is not a connector definition (e.g. a detector rule).
    A definition with ``command`` is a :class:`~.command.CommandConnector`."""
    try:
        spec = read_yaml_mapping(path, "regex connector")
    except ConfigError as e:
        raise ConnectorError(str(e)) from e
    if "connector" not in spec:
        return None
    if "command" in spec:
        return CommandConnector(spec, path)
    return RegexConnector(spec, path)


def load_dirs(registry: Registry, dirs: Iterable[Path]) -> None:
    for d in dirs:
        if not d.is_dir():
            if d != BUILTIN_DIR:
                raise ConnectorError(f"rule directory {d} does not exist")
            continue
        for path in sorted([*d.rglob("*.yaml"), *d.rglob("*.yml")]):
            conn = load_file(path)
            if conn is not None:
                registry.add(conn, origin=str(path), replace=conn.replace)
