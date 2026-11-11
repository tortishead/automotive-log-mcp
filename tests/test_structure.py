"""The layout of the tools package and server wiring: no import cycle, one tool table, typed errors."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import automotive_log_mcp.tools
from automotive_log_mcp.app import App
from automotive_log_mcp.core.align import _zone_offset
from automotive_log_mcp.core.timeparse import NS
from automotive_log_mcp.server import TOOL_NAMES, TOOL_SPECS
from automotive_log_mcp.tools import CaseNotFoundError, ToolInputError, Tools
from automotive_log_mcp.tools.timespec import full_ts_ns

TOOLS_DIR = Path(automotive_log_mcp.tools.__file__).parent


def _runtime_imports(path: Path) -> set[str]:
    """Sibling modules a tools module imports at run time (``TYPE_CHECKING`` blocks left out)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.If) and getattr(node.test, "id", None) == "TYPE_CHECKING":
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.ImportFrom) and sub.level == 1:
                out |= {sub.module} if sub.module else {a.name for a in sub.names}
    return out


def test_tools_package_has_no_import_cycle() -> None:
    """The modules form layers (see the package docstring): no function-local imports needed."""
    graph = {p.stem: _runtime_imports(p) for p in TOOLS_DIR.glob("*.py") if p.stem != "__init__"}
    done: set[str] = set()

    def visit(mod: str, path: tuple[str, ...]) -> None:
        assert mod not in path, "import cycle: " + " -> ".join((*path, mod))
        if mod in done:
            return
        for dep in graph.get(mod, ()):
            visit(dep, (*path, mod))
        done.add(mod)

    for mod in graph:
        visit(mod, ())
    assert "base" not in graph["clock"] | graph["diff"]


def test_tool_specs_are_the_tool_names() -> None:
    names = [name for name, _, _ in TOOL_SPECS]
    assert tuple(names) == TOOL_NAMES and len(set(names)) == len(names) == 14


def test_missing_case_is_a_typed_input_error(app: App) -> None:
    with pytest.raises(CaseNotFoundError, match="does not exist") as e:
        Tools(app).overview("no-such-case")
    assert isinstance(e.value, ToolInputError)  # the server turns it into a tool error


def test_unknown_or_malformed_zone_gives_no_offset() -> None:
    at = 1_767_225_600 * NS
    assert _zone_offset("Europe/Berlin", "Europe/Berlin", at) == 0
    assert _zone_offset("Europe/Berlin", "UTC", at) == -3600 * NS
    assert _zone_offset("Not/AZone", "UTC", at) is None
    assert _zone_offset("../etc", "UTC", at) is None


def test_full_ts_ns_rejects_other_text() -> None:
    assert full_ts_ns("1970-01-01 00:00:01.000") == NS
    with pytest.raises(ValueError, match="not a full timestamp"):
        full_ts_ns("yesterday")
