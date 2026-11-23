"""Paths, output caps, rule directories and the policy file location.

Configuration is read from ``<home>/config.yaml`` (or the file named by ``--config`` /
``AUTOMOTIVE_LOG_MCP_CONFIG``). Every key is optional; unknown keys are an error so that
typos fail loudly instead of being ignored.

``<home>`` is ``~/.automotive-log-mcp`` unless ``AUTOMOTIVE_LOG_MCP_HOME`` is set.

``AUTOMOTIVE_LOG_MCP_RULE_DIRS`` adds rule directories to the ones in ``rule_dirs``: a list of
paths separated by ``os.pathsep`` (``:`` on Linux/macOS, ``;`` on Windows). Relative entries resolve
against the current directory; empty entries are skipped.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ENV_HOME = "AUTOMOTIVE_LOG_MCP_HOME"
ENV_CONFIG = "AUTOMOTIVE_LOG_MCP_CONFIG"
ENV_RULE_DIRS = "AUTOMOTIVE_LOG_MCP_RULE_DIRS"


CASE_NAME_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


class ConfigError(ValueError):
    """Raised for a missing, unreadable or invalid configuration or policy file."""


@dataclass(frozen=True)
class Caps:
    """Output limits applied by every tool (design: 50 lines / ~8 KB, messages cut at 200 chars)."""

    max_lines: int = 50
    max_bytes: int = 8192
    max_msg_chars: int = 200
    regex_scan_rows: int = 1_000_000  # candidate records a regex search scans before it stops


@dataclass(frozen=True)
class Retention:
    """Automatic cleanup when the server starts; ``None`` turns a rule off (see ``retention.py``)."""

    case_days: int | None = 30  # delete a case DB not used for this many days
    rounds_days: int | None = 30  # delete round files older than this
    call_log_mb: int | None = 20  # rotate the call log to ``<name>.1`` above this size


@dataclass(frozen=True)
class Config:
    home: Path
    cases_dir: Path
    policy_file: Path
    call_log: Path | None
    caps: Caps = field(default_factory=Caps)
    rule_dirs: tuple[Path, ...] = ()
    # Tool results with structuredContent and output schemas; off by default because some clients
    # (Claude Code) then forward only the JSON to the model, at 1.5-2x the bytes of the text.
    structured_output: bool = False
    # One call-log file per round (and a report next to it), to hand over for analysis.
    rounds_dir: Path | None = None
    round_gap_minutes: int = 10
    retention: Retention = field(default_factory=Retention)

    def case_db(self, case: str) -> Path:
        """Path of a case's DB file. ``case`` must already be validated."""
        return self.cases_dir / f"{case}.db"


def default_home() -> Path:
    env = os.environ.get(ENV_HOME)
    return Path(env).expanduser() if env else Path.home() / ".automotive-log-mcp"


_TOP_KEYS = {
    "cases_dir",
    "policy_file",
    "call_log",
    "caps",
    "rule_dirs",
    "structured_output",
    "rounds_dir",
    "round_gap_minutes",
    "retention",
}
_CAP_KEYS = {"max_lines", "max_bytes", "max_msg_chars", "regex_scan_rows"}
_RETENTION_KEYS = {"case_days", "rounds_days", "call_log_mb"}


def _path(value: Any, key: str, base: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"config key '{key}' must be a non-empty path string")
    p = Path(value).expanduser()
    return p if p.is_absolute() else base / p


def _positive_int(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"config key 'caps.{key}' must be a positive integer, got {value!r}")
    return value


def _retention(data: Any) -> Retention:
    if data is None:
        return Retention(None, None, None)  # ``retention: null``: no automatic cleanup
    if not isinstance(data, dict):
        raise ConfigError("config key 'retention' must be a mapping")
    unknown = set(data) - _RETENTION_KEYS
    if unknown:
        raise ConfigError(f"unknown keys under 'retention': {', '.join(sorted(unknown))}")
    values: dict[str, int | None] = {}
    for k, v in data.items():
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v <= 0):
            raise ConfigError(f"config key 'retention.{k}' must be a positive integer or null, got {v!r}")
        values[k] = v
    return Retention(**values)


def env_rule_dirs() -> list[Path]:
    """Rule directories from ``AUTOMOTIVE_LOG_MCP_RULE_DIRS`` (``os.pathsep``-separated)."""
    value = os.environ.get(ENV_RULE_DIRS, "")
    dirs: list[Path] = []
    for entry in value.split(os.pathsep):
        entry = entry.strip()
        if entry:
            p = Path(entry).expanduser()
            dirs.append(p if p.is_absolute() else Path.cwd() / p)
    return dirs


def _unique(paths: list[Path]) -> tuple[Path, ...]:
    """Drop repeats (a folder listed in both places would load its connectors twice)."""
    seen: set[Path] = set()
    out: list[Path] = []
    for p in paths:
        key = Path(os.path.normpath(p))
        if key not in seen:
            seen.add(key)
            out.append(p)
    return tuple(out)


def read_yaml_mapping(path: Path, what: str) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"cannot read {what} {path}: {e}") from e
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"{what} {path} is not valid YAML: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{what} {path} must be a mapping at the top level")
    return data


def load_config(config_file: Path | None = None, home: Path | None = None) -> Config:
    """Load the configuration. A missing default config file means all defaults."""
    home = home or default_home()
    explicit = config_file or (Path(os.environ[ENV_CONFIG]) if os.environ.get(ENV_CONFIG) else None)
    path = explicit or home / "config.yaml"

    data: dict[str, Any] = {}
    if explicit is not None or path.exists():
        data = read_yaml_mapping(path, "config file")
    base = path.parent

    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise ConfigError(f"unknown config keys in {path}: {', '.join(sorted(unknown))}")

    caps_data = data.get("caps") or {}
    if not isinstance(caps_data, dict):
        raise ConfigError("config key 'caps' must be a mapping")
    unknown_caps = set(caps_data) - _CAP_KEYS
    if unknown_caps:
        raise ConfigError(f"unknown keys under 'caps': {', '.join(sorted(unknown_caps))}")
    caps = Caps(**{k: _positive_int(v, k) for k, v in caps_data.items()})

    rule_dirs_data = data.get("rule_dirs") or []
    if not isinstance(rule_dirs_data, list):
        raise ConfigError("config key 'rule_dirs' must be a list of paths")

    structured = data.get("structured_output", False)
    if not isinstance(structured, bool):
        raise ConfigError(f"config key 'structured_output' must be true or false, got {structured!r}")

    call_log: Path | None
    if "call_log" in data and data["call_log"] is None:
        call_log = None  # explicitly disabled
    elif "call_log" in data:
        call_log = _path(data["call_log"], "call_log", base)
    else:
        call_log = home / "calllog.jsonl"

    rounds_dir: Path | None
    if "rounds_dir" in data and data["rounds_dir"] is None:
        rounds_dir = None  # explicitly disabled
    elif "rounds_dir" in data:
        rounds_dir = _path(data["rounds_dir"], "rounds_dir", base)
    else:
        rounds_dir = home / "rounds" if call_log is not None else None  # no logging: no rounds either
    gap = data.get("round_gap_minutes", 10)
    if isinstance(gap, bool) or not isinstance(gap, int) or gap <= 0:
        raise ConfigError(f"config key 'round_gap_minutes' must be a positive integer, got {gap!r}")

    return Config(
        home=home,
        cases_dir=_path(data["cases_dir"], "cases_dir", base) if "cases_dir" in data else home / "cases",
        policy_file=_path(data["policy_file"], "policy_file", base)
        if "policy_file" in data
        else home / "policy.yaml",
        call_log=call_log,
        caps=caps,
        rule_dirs=_unique([*(_path(d, "rule_dirs[]", base) for d in rule_dirs_data), *env_rule_dirs()]),
        structured_output=structured,
        rounds_dir=rounds_dir,
        round_gap_minutes=gap,
        retention=_retention(data["retention"]) if "retention" in data else Retention(),
    )
