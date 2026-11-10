"""Policy file: which tools an agent may call and how much it may read.

``policy.yaml`` (path from config) is optional; without it every tool is allowed and there is
no byte budget. Keys:

- ``allowed_tools``: list of tool names; tools not listed are not registered at all, so agents
  never see them. Omitted or null means all tools.
- ``blocked_sources``: glob patterns matched against source paths and source ids; matching
  sources are refused by ``add_source`` and hidden from every tool (enforced from M1).
- ``blocked_ecus``: DLT ECU ids hidden from every tool (not enforced yet; planned with the DLT connector).
- ``byte_budget``: total bytes of tool output per server session; calls beyond it are refused.
- ``allow_code_connectors``: whether Python entry-point connectors may be loaded (D13).
  Default false; YAML and ``command`` connectors are always allowed.
- ``allowed_roots``: directories ``add_source`` (and the CLI ``index``) may read from; a path
  outside them is refused before the server looks at it, also through a symbolic link. Omitted
  or null: any path (stdio, where the agent runs as the user). Over HTTP an omitted list means
  **no** path: ``serve --http`` refuses every ``add_source`` until roots are listed (files can
  still be indexed with the CLI on the server).
"""

from __future__ import annotations

import fnmatch
import os
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any

from .config import ConfigError, read_yaml_mapping

_KEYS = {
    "allowed_tools",
    "blocked_sources",
    "blocked_ecus",
    "byte_budget",
    "allow_code_connectors",
    "allowed_roots",
}


@dataclass(frozen=True)
class Policy:
    allowed_tools: frozenset[str] | None = None
    blocked_sources: tuple[str, ...] = ()
    blocked_ecus: frozenset[str] = frozenset()
    byte_budget: int | None = None
    allow_code_connectors: bool = False
    allowed_roots: tuple[Path, ...] | None = None  # None: any path; (): none

    def path_allowed(self, path: Path) -> bool:
        """Whether ``path`` (absolute, resolved) is inside one of ``allowed_roots``."""
        if self.allowed_roots is None:
            return True
        p = _norm(path)
        return any(p == r or p.is_relative_to(r) for r in map(_norm, self.allowed_roots))

    def roots_text(self) -> str:
        return ", ".join(str(r) for r in self.allowed_roots or ()) or "none"

    def tool_allowed(self, name: str) -> bool:
        return self.allowed_tools is None or name in self.allowed_tools

    def source_blocked(self, *names: str) -> bool:
        return any(fnmatch.fnmatch(n, pat) for n in names for pat in self.blocked_sources)

    def ecu_blocked(self, ecu: str) -> bool:
        return ecu in self.blocked_ecus

    def check_tools(self, known: Iterable[str]) -> None:
        """Fail loudly when ``allowed_tools`` names a tool that does not exist."""
        if self.allowed_tools is None:
            return
        unknown = self.allowed_tools - set(known)
        if unknown:
            raise ConfigError(f"policy allowed_tools names unknown tools: {', '.join(sorted(unknown))}")


def _norm(path: Path) -> PurePath:
    """Case-folded on Windows, where paths are case-insensitive."""
    return PurePath(os.path.normcase(str(path)))


def _str_list(value: Any, key: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"policy key '{key}' must be a list of non-empty strings")
    return value


def load_policy(path: Path) -> Policy:
    """Load the policy file. A missing file means the permissive default."""
    if not path.exists():
        return Policy()
    data = read_yaml_mapping(path, "policy file")
    unknown = set(data) - _KEYS
    if unknown:
        raise ConfigError(f"unknown policy keys in {path}: {', '.join(sorted(unknown))}")

    allowed = data.get("allowed_tools")
    budget = data.get("byte_budget")
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0):
        raise ConfigError(f"policy key 'byte_budget' must be a positive integer, got {budget!r}")
    code = data.get("allow_code_connectors", False)
    if not isinstance(code, bool):
        raise ConfigError("policy key 'allow_code_connectors' must be true or false")

    roots: tuple[Path, ...] | None = None
    if data.get("allowed_roots") is not None:
        roots = tuple(
            (path.parent / Path(r).expanduser()).resolve()
            for r in _str_list(data["allowed_roots"], "allowed_roots")
        )
    return Policy(
        allowed_roots=roots,
        allowed_tools=None if allowed is None else frozenset(_str_list(allowed, "allowed_tools")),
        blocked_sources=tuple(_str_list(data.get("blocked_sources"), "blocked_sources")),
        blocked_ecus=frozenset(_str_list(data.get("blocked_ecus"), "blocked_ecus")),
        byte_budget=budget,
        allow_code_connectors=code,
    )


@dataclass
class ByteBudget:
    """Counts output bytes for one server session against ``Policy.byte_budget``."""

    limit: int | None
    used: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def exhausted(self) -> bool:
        with self._lock:
            return self.limit is not None and self.used >= self.limit

    def add(self, n: int) -> None:
        with self._lock:
            self.used += n
