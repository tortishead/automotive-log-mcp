"""Connector protocols and the registry (D13).

Two kinds of plug-ins turn bytes into data:

- A **connector** parses one stream into records (:class:`Connector`). Text formats subclass
  :class:`TextConnector` and implement :meth:`TextConnector.parse_lines`.
- A **container** expands one file into members (:class:`Container`): an archive into its files,
  a bugreport into its sections. Each member is then sniffed (or has a format hint) and parsed.

The registry holds built-in connectors, YAML regex connectors (from the package's ``rules``
directory and configured rule directories) and, when the policy allows code connectors, Python
entry points in the group ``automotive_log_mcp.connectors``. Third-party connectors extend the set
by default; one with the name of an existing connector replaces it only when it says
``replace: true``. The highest ``sniff()`` score above 0.5 wins.
"""

from __future__ import annotations

import codecs
import contextlib
import io
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any, BinaryIO, Protocol, runtime_checkable

from ..model import ProcessInfo, RawRecord, StreamMarker

SNIFF_BYTES = 64 * 1024
MIN_SCORE = 0.5
ENTRY_POINT_GROUP = "automotive_log_mcp.connectors"


class ConnectorError(ValueError):
    """Unknown format, invalid connector definition, or a connector that cannot load."""


@dataclass
class SourceContext:
    """What a connector knows about the stream it parses."""

    name: str  # display name of the member, e.g. 'bugreport.zip!bugreport.txt'
    filename: str  # base file name used for sniffing
    ref_year: int  # year the log was taken (for formats without a year)
    ref_month: int = 12
    meta: dict[str, Any] = field(default_factory=dict)  # container metadata (fingerprint, tz, ...)
    options: dict[str, Any] = field(default_factory=dict)


ParseItem = RawRecord | StreamMarker


@runtime_checkable
class Connector(Protocol):
    name: str
    version: str

    def sniff(self, head: bytes, filename: str) -> float: ...

    def parse(self, stream: BinaryIO, ctx: SourceContext) -> Iterator[ParseItem]: ...


def iter_text_lines(stream: BinaryIO) -> Iterator[str]:
    """Decode a byte stream as UTF-8 (invalid bytes replaced), one line at a time, without EOL."""
    text = io.TextIOWrapper(stream, encoding="utf-8", errors="replace", newline="\n")
    try:
        for line in text:
            yield line.rstrip("\r\n")
    finally:
        with contextlib.suppress(ValueError):  # the stream may already be closed
            text.detach()


class TextConnector:
    """Base class for line-oriented formats."""

    name = "text"
    version = "1"

    def sniff(self, head: bytes, filename: str) -> float:
        return 0.0

    def parse(self, stream: BinaryIO, ctx: SourceContext) -> Iterator[ParseItem]:
        return self.parse_lines(iter_text_lines(stream), ctx)

    def parse_lines(self, lines: Iterator[str], ctx: SourceContext) -> Iterator[ParseItem]:
        raise NotImplementedError


def head_lines(head: bytes, n: int = 40) -> list[str]:
    """First lines of a sniff head, decoded leniently; the last (possibly cut) line is dropped."""
    text = codecs.decode(head, "utf-8", errors="replace")
    lines = text.splitlines()
    if len(head) >= SNIFF_BYTES and lines:
        lines = lines[:-1]
    return [ln for ln in lines[:n]]


@dataclass
class Member:
    """One stream inside a container.

    Exactly one of ``open`` (a byte stream) or ``lines`` (decoded lines, for sections of a text
    container) is set. ``format`` forces a connector; ``None`` means sniff. ``stream`` names the
    default sub-source (e.g. 'events', 'kernel'). ``optional`` members whose format is unknown are
    skipped instead of failing the import.
    """

    name: str
    open: Callable[[], BinaryIO] | None = None
    lines: Iterator[str] | None = None
    format: str | None = None
    stream: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    ref_year: int | None = None
    ref_month: int | None = None
    optional: bool = True


@dataclass
class ContainerContext:
    """Shared state of one container expansion.

    A container fills ``meta`` (applied to every source from this file, also from sections that
    come later in the file, e.g. properties after logs) and ``processes``.
    """

    name: str
    filename: str
    ref_year: int
    ref_month: int
    meta: dict[str, Any] = field(default_factory=dict)
    processes: list[ProcessInfo] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@runtime_checkable
class Container(Protocol):
    name: str
    version: str

    def sniff(self, head: bytes, filename: str) -> float: ...

    def expand(self, member: Member, ctx: ContainerContext) -> Iterator[Member]: ...


Plugin = Connector | Container


@dataclass(frozen=True)
class Guess:
    name: str
    score: float


class Registry:
    def __init__(self) -> None:
        self._plugins: dict[str, Plugin] = {}
        self.origins: dict[str, str] = {}

    def add(self, plugin: Plugin, origin: str = "builtin", replace: bool = False) -> None:
        name = plugin.name
        if name in self._plugins and not replace:
            raise ConnectorError(
                f"connector '{name}' from {origin} has the same name as the one from "
                f"{self.origins[name]}; set 'replace: true' to replace it"
            )
        self._plugins[name] = plugin
        self.origins[name] = origin

    def get(self, name: str) -> Plugin:
        try:
            return self._plugins[name]
        except KeyError:
            raise ConnectorError(
                f"unknown format '{name}'; known formats: {', '.join(sorted(self._plugins))}"
            ) from None

    def names(self) -> list[str]:
        return sorted(self._plugins)

    def plugins(self) -> Iterable[Plugin]:
        return self._plugins.values()

    def guesses(self, head: bytes, filename: str) -> list[Guess]:
        out = []
        for p in self._plugins.values():
            try:
                score = float(p.sniff(head, filename))
            except Exception:
                score = 0.0
            out.append(Guess(p.name, max(0.0, min(1.0, score))))
        out.sort(key=lambda g: (-g.score, g.name))
        return out

    def detect(self, head: bytes, filename: str) -> Plugin:
        guesses = self.guesses(head, filename)
        if guesses and guesses[0].score > MIN_SCORE:
            return self._plugins[guesses[0].name]
        top = ", ".join(f"{g.name} {g.score:.2f}" for g in guesses[:3])
        raise ConnectorError(
            f"unknown format for '{filename}' (no connector scored above {MIN_SCORE}; top guesses: {top}). "
            "Pass format=<name> to force one."
        )


def is_container(p: Plugin) -> bool:
    return hasattr(p, "expand")


def load_entry_points(registry: Registry) -> None:
    for ep in metadata.entry_points(group=ENTRY_POINT_GROUP):
        try:
            obj = ep.load()
            plugin = obj() if isinstance(obj, type) else obj
        except Exception as e:
            raise ConnectorError(f"cannot load connector entry point '{ep.name}' ({ep.value}): {e}") from e
        if not isinstance(plugin, Connector | Container):
            raise ConnectorError(f"entry point '{ep.name}' is not a connector or container")
        registry.add(
            plugin, origin=f"entry point {ep.value}", replace=bool(getattr(plugin, "replace", False))
        )


def build_registry(rule_dirs: Iterable[Path] = (), allow_code_connectors: bool = False) -> Registry:
    """Built-in connectors, then YAML connectors, then (if allowed) entry points."""
    from . import archive, bugreport, kernel, logcat, regex_yaml

    reg = Registry()
    for plugin in (
        archive.ArchiveContainer(),
        bugreport.BugreportContainer(),
        logcat.LogcatConnector(),
        kernel.KernelConnector(),
    ):
        reg.add(plugin)
    regex_yaml.load_dirs(reg, [regex_yaml.BUILTIN_DIR, *rule_dirs])
    if allow_code_connectors:
        load_entry_points(reg)
    return reg
