"""Helpers shared by the artifact parsers: sections, times, frames, text clipping."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ...core.timeparse import fmt_wall, wall_ns
from ...model import ArtifactSection, RawArtifact

_SLUG = re.compile(r"[^a-z0-9_.]+")

WALL = re.compile(r"(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d):(\d\d)(?:\.(\d+))?(?:\s*([+-]\d\d:?\d\d|Z|UTC))?")

HEADLINE_CHARS = 240


def slug(text: str, limit: int = 40) -> str:
    """A section-name part: lower case, ``[a-z0-9_.-]``, at most ``limit`` characters."""
    s = _SLUG.sub("-", text.lower()).strip("-.")
    return s[:limit].rstrip("-.") or "x"


def clip(text: str, limit: int = HEADLINE_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_wall(text: str) -> tuple[int, str | None] | None:
    """'2026-01-01 00:00:05.123456789+0100' -> (ns as written, '+0100'); None if no time."""
    m = WALL.search(text)
    if m is None:
        return None
    y, mo, d, hh, mi, ss = (int(x) for x in m.groups()[:6])
    try:
        ns = wall_ns(y, mo, d, hh, mi, ss, m.group(7) or "")
    except ValueError:
        return None
    return ns, m.group(8)


def section_time(meta: dict[str, Any]) -> int | None:
    """The mtime of the file a bugreport section dumped (source metadata ``section_time``)."""
    value = meta.get("section_time")
    t = parse_wall(value) if isinstance(value, str) else None
    return t[0] if t else None


def fmt_time(ns: int | None) -> str:
    return fmt_wall(ns) if ns is not None else "unknown time"


class Sections:
    """Builds contiguous, uniquely named sections line by line."""

    def __init__(self, first: str = "header") -> None:
        self.items: list[ArtifactSection] = []
        self._names: set[str] = set()
        self._first = first

    def start(self, name: str) -> None:
        if self.items and not self.items[-1].lines:
            self._names.discard(self.items[-1].name)
            self.items.pop()
        base, n = name, 2
        while name in self._names:
            name = f"{base}-{n}"
            n += 1
        self._names.add(name)
        self.items.append(ArtifactSection(name, []))

    @property
    def current(self) -> str | None:
        return self.items[-1].name if self.items else None

    def add(self, line: str) -> None:
        if not self.items:
            self.start(self._first)
        self.items[-1].lines.append(line)

    def result(self) -> list[ArtifactSection]:
        return [s for s in self.items if s.lines]


@dataclass
class Parsed:
    """What a body parser found; ``describe`` turns it into headline, summary and signature."""

    type: str
    sections: list[ArtifactSection]
    fields: dict[str, Any] = field(default_factory=dict)
    ts: int | None = None
    ts_raw: str | None = None
    pid: int | None = None
    tid: int | None = None
    process: str | None = None
    level: str = "F"
    consumed: int = 0

    def prepend(self, section: ArtifactSection) -> None:
        """Add a section in front (e.g. a dropbox header), keeping names unique."""
        names = {s.name for s in self.sections}
        name, n = section.name, 2
        while name in names:
            name = f"{section.name}-{n}"
            n += 1
        self.sections.insert(0, ArtifactSection(name, section.lines))


Describe = Callable[[Parsed], tuple[str, str, str | None]]


def to_artifact(p: Parsed, describe: Describe) -> RawArtifact:
    headline, summary, signature = describe(p)
    return RawArtifact(
        type=p.type,
        sections=p.sections,
        headline=clip(headline),
        summary=summary,
        signature=signature,
        ts=p.ts,
        ts_raw=p.ts_raw,
        pid=p.pid,
        tid=p.tid,
        process=p.process,
        level=p.level,
        fields=p.fields,
        consumed=p.consumed or sum(len(s.lines) for s in p.sections),
    )


def strip_args(func: str) -> str:
    """'VehicleHal::set(Prop const&)' -> 'VehicleHal::set'; 'a.B.c(B.java:4)' -> 'a.B.c'."""
    func = func.strip()
    if func.endswith(")"):
        depth = 0
        for i in range(len(func) - 1, -1, -1):
            ch = func[i]
            if ch == ")":
                depth += 1
            elif ch == "(":
                depth -= 1
                if depth == 0:
                    return func[:i].strip() or func
    return func


def basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1] or path


def signature(kind: str, process: str | None, frames: list[str]) -> str:
    """``<kind> <process>: f1 < f2 < f3`` (``<`` reads 'called from')."""
    head = f"{kind} {process or '?'}"
    return f"{head}: {' < '.join(frames)}" if frames else head


def header_fields(lines: list[str]) -> dict[str, str]:
    """'Key: value' lines (dropbox headers) as a dict; later duplicates are ignored."""
    out: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.partition(": ")
        if sep and key and " " not in key.strip() and key.strip() not in out:
            out[key.strip()] = value.strip()
    return out


def to_int(text: str | None) -> int | None:
    if text is None:
        return None
    text = text.strip()
    return int(text) if text.isdigit() else None
