"""Token accounting for the call log: sizes, hashes, citation ids and the server's fixed cost.

The server never sees the model's real token counts; the client does (see README "Token use").
What the server can measure is bytes: what each tool result holds, and what the server adds to
every model request (its instructions and the tool list). Tokens are estimated as bytes / 4,
which is a rough rule for English text; JSON and log lines with many digits and symbols usually
take more tokens per byte (Claude Code measured about 10.1k tokens for this server's ~40 KB
tool list and instructions, so ~4 bytes per token there).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

BYTES_PER_TOKEN = 4
PAGING_ARGS = frozenset({"limit", "cursor", "before", "after", "examples"})
MAX_CITES = 300  # citation ranges kept per call-log line

_LOG = re.compile(r"\blog:(\d+)#(\d+)\b")
_ART = re.compile(r"\bart:(\d+)\b")


def est_tokens(n_bytes: int) -> int:
    """Approximate tokens for ``n_bytes`` of UTF-8 (bytes / 4, rounded up)."""
    return -(-n_bytes // BYTES_PER_TOKEN)


def json_bytes(value: Any) -> int:
    """Bytes of ``value`` as compact JSON, the way clients forward structured content."""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


# -- argument hashes ---------------------------------------------------------------------------------


def _norm(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in sorted(value.items()) if v is not None and v != {} and v != []}
    if isinstance(value, list | tuple):
        return [_norm(v) for v in value]
    return value


def args_hashes(tool: str, args: dict[str, Any]) -> tuple[str, str]:
    """(exact, near) hashes of normalized arguments: None and empty values dropped, whitespace
    collapsed, keys sorted. ``near`` also drops paging and size arguments, so a search repeated
    with another limit or the next cursor has the same near hash."""
    norm = _norm(args)
    near = {k: v for k, v in norm.items() if k not in PAGING_ARGS}

    def h(v: Any) -> str:
        text = tool + "\0" + json.dumps(v, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]

    return h(norm), h(near)


# -- citation ids -------------------------------------------------------------------------------------


def cite_ids(text: str) -> tuple[set[tuple[int, int]], set[int]]:
    """Log citations (source, seq) and artifact ids in ``text`` (sections and lines count as the
    artifact)."""
    logs = {(int(m[1]), int(m[2])) for m in _LOG.finditer(text)}
    arts = {int(m[1]) for m in _ART.finditer(text)}
    return logs, arts


def compact_cites(logs: Iterable[tuple[int, int]], arts: Iterable[int]) -> list[str]:
    """``['art:4', 'log:3#1-5', 'log:3#9']``: ids only, consecutive seqs as ranges."""
    out = [f"art:{a}" for a in sorted(set(arts))]
    by_src: dict[int, list[int]] = {}
    for s, q in sorted(set(logs)):
        by_src.setdefault(s, []).append(q)
    for s, seqs in by_src.items():
        runs: list[list[int]] = []
        for q in seqs:
            if runs and q == runs[-1][1] + 1:
                runs[-1][1] = q
            else:
                runs.append([q, q])
        out.extend(f"log:{s}#{lo}" if lo == hi else f"log:{s}#{lo}-{hi}" for lo, hi in runs)
    return out


_RANGE = re.compile(r"log:(\d+)#(\d+)(?:-(\d+))?$")


@dataclass
class CiteSet:
    """Citation ids from compacted ranges, for 'was this output cited in the answer?'."""

    logs: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    arts: set[int] = field(default_factory=set)

    @classmethod
    def parse(cls, items: Iterable[str]) -> CiteSet:
        cs = cls()
        for item in items:
            if item.startswith("art:") and item[4:].isdigit():
                cs.arts.add(int(item[4:]))
                continue
            m = _RANGE.match(item)
            if m:
                lo = int(m[2])
                cs.logs.setdefault(int(m[1]), []).append((lo, int(m[3] or lo)))
        return cs

    @classmethod
    def from_text(cls, text: str) -> CiteSet:
        logs, arts = cite_ids(text)
        return cls.parse(compact_cites(logs, arts))

    def __bool__(self) -> bool:
        return bool(self.logs or self.arts)

    def overlaps(self, other: CiteSet) -> bool:
        if self.arts & other.arts:
            return True
        for src, ranges in self.logs.items():
            for lo, hi in other.logs.get(src, ()):
                if any(a <= hi and lo <= b for a, b in ranges):
                    return True
        return False


# -- the fixed cost of the server ---------------------------------------------------------------------


@dataclass
class ToolCost:
    name: str
    model_bytes: int  # name, description and input schema: what clients put into each model request
    output_schema_bytes: int  # protocol only in the clients checked (not sent to the model)
    listing_bytes: int  # the whole tools/list entry


@dataclass
class FixedCost:
    instructions_bytes: int
    tools: list[ToolCost]

    @property
    def model_bytes(self) -> int:
        """Bytes the server adds to every model request (instructions + tool definitions)."""
        return self.instructions_bytes + sum(t.model_bytes for t in self.tools)

    @property
    def listing_bytes(self) -> int:
        return sum(t.listing_bytes for t in self.tools)

    def as_log(self) -> dict[str, Any]:
        return {
            "instructions_bytes": self.instructions_bytes,
            "tools_model_bytes": sum(t.model_bytes for t in self.tools),
            "tools_listing_bytes": self.listing_bytes,
            "per_tool": {t.name: t.model_bytes for t in self.tools},
        }


def tool_cost(tool: dict[str, Any]) -> ToolCost:
    """Sizes of one tools/list entry (as JSON with the protocol's camelCase keys)."""
    model = {
        "name": tool.get("name"),
        "description": tool.get("description"),
        "input_schema": tool.get("inputSchema"),
    }
    out = tool.get("outputSchema")
    return ToolCost(
        name=str(tool.get("name")),
        model_bytes=json_bytes(model),
        output_schema_bytes=json_bytes(out) if out else 0,
        listing_bytes=json_bytes(tool),
    )


def fixed_cost(instructions: str | None, tools: Iterable[dict[str, Any]]) -> FixedCost:
    return FixedCost(
        instructions_bytes=len((instructions or "").encode("utf-8")),
        tools=[tool_cost(t) for t in tools],
    )
