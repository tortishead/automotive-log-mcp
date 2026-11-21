"""docs/walkthrough.md: every call runs, and every output line shown (except lines cut with '…')
is in the real output."""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Any

import anyio

from automotive_log_mcp.config import load_config
from automotive_log_mcp.policy import Policy
from automotive_log_mcp.server import LogServer

from .conftest import FIXED_SECRET
from .test_golden import make_bugreport_zip

DOC = Path(__file__).parents[1] / "docs" / "walkthrough.md"


def _pairs() -> list[tuple[str, dict[str, Any], list[str]]]:
    blocks = re.findall(r"^```\n(.*?)^```", DOC.read_text(encoding="utf-8"), re.M | re.S)
    out = []
    for call, shown in zip(blocks[0::2], blocks[1::2], strict=True):
        node = ast.parse(call.strip(), mode="eval").body
        assert isinstance(node, ast.Call) and isinstance(node.func, ast.Name), call
        kwargs = {k.arg: ast.literal_eval(k.value) for k in node.keywords if k.arg}
        out.append((node.func.id, kwargs, [ln for ln in shown.splitlines() if ln and "…" not in ln]))
    return out


def test_walkthrough_outputs_are_real(home: Path, tmp_path: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "secret.key").write_bytes(FIXED_SECRET)
    zpath = make_bugreport_zip(tmp_path)
    server = LogServer(load_config(), Policy(), wait_for_imports=True)
    pairs = _pairs()
    assert len(pairs) == 10

    async def main() -> None:
        for name, kwargs, shown in pairs:
            if name == "add_source":
                kwargs["path"] = str(zpath.rename(zpath.with_name("bugreport.zip")))
            result = await server.mcp.call_tool(name, kwargs)
            text = "\n".join(c.text for c in result.content if c.type == "text")  # type: ignore[union-attr]
            text = text.replace(str(tmp_path) + os.sep, "")  # the doc shows the file name only
            for line in shown:
                assert line in text, f"{name}: {line!r} not in\n{text}"

    anyio.run(main)
