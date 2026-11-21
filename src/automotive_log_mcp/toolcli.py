"""Every MCP tool as a command: ``automotive-log-mcp <tool> <case> ...``.

The commands are built from the tools' input schemas, and each call goes through the same MCP tool
(argument validation, policy, byte budget, call log), so the CLI and the server cannot drift apart:

- required parameters are positional (``case``, ``path``, ``at``, ``id``, ``ids``); ``query`` is an
  optional positional; ``text`` (check_citations) is a file, ``-`` for standard input;
- every other parameter is an option, ``not_tags`` as ``--not-tags``; lists repeat (``--tag A --tag
  B``, the singular is accepted); the record filter's fields are options of the command itself
  (``--level E --from -5m``); ``--attrs KEY=VALUE`` repeats; a parameter that takes null as a value
  takes ``none`` (``count --by none``);
- ``--json`` prints the structured content instead of the text.

Exit status: 0 on success, 1 for a tool error or a check_citations FAIL, 2 for a usage error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import types
import typing
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .config import Config
from .policy import Policy
from .server import TOOL_SPECS, LogServer

PROG = "automotive-log-mcp"
_POSITIONAL_OPTIONAL = frozenset({"query"})  # optional, but the obvious thing to type after the case
_FILE_PARAMS = frozenset({"text"})  # read from a file or standard input
_PATH_PARAMS = frozenset({"path"})  # made absolute against the working directory


def normalize(name: str) -> str:
    return name.replace("-", "_")


def commands_help() -> str:
    width = max(len(n) for n, _, _ in TOOL_SPECS)
    rows = "\n".join(f"  {n:<{width}}  {title}" for n, _, title in TOOL_SPECS)
    return f"commands (run as a CLI, without an agent; '{PROG} <command> --help' for its options):\n{rows}"


@dataclass(frozen=True)
class _Arg:
    dest: str
    path: tuple[str, ...]  # where the value goes in the tool's arguments: ("filter", "level")
    convert: Callable[[Any], Any]


def _nullable(field: Any) -> bool:
    """Whether a pydantic field takes None although its default is not None (count's ``by``)."""
    if field is None or field.default is None:
        return False
    return type(None) in typing.get_args(field.annotation) or (
        isinstance(field.annotation, types.UnionType) and type(None) in field.annotation.__args__
    )


def _resolve(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    if not isinstance(ref, str):
        return node
    target = schema.get("$defs", {})[ref.rsplit("/", 1)[1]]
    return {**target, **{k: v for k, v in node.items() if k != "$ref"}}


def _scalar(value: str) -> Any:
    """``--attrs art=3``: a number or true/false if it reads as one, else the string."""
    try:
        parsed = json.loads(value)
    except ValueError:
        return value
    return parsed if isinstance(parsed, int | float | bool) else value


def _kv(text: str) -> tuple[str, Any]:
    key, sep, value = text.partition("=")
    if not sep or not key:
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, not {text!r}")
    return key, _scalar(value)


def _read_text(value: str) -> str:
    if value == "-":
        return sys.stdin.read()
    try:
        with open(value, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        raise ToolError(f"cannot read {value}: {e.strerror or e}") from None


def _help(node: dict[str, Any]) -> str:
    text = str(node.get("description", "")).replace("%", "%%")
    if "default" in node and node["default"] not in (None, ""):
        default = node["default"]
        shown = str(default).lower() if isinstance(default, bool) else default
        text += f" (default: {shown})"
    return text


def _add(
    parser: argparse._ActionsContainer,
    key: str,
    node: dict[str, Any],
    required: bool,
    path: tuple[str, ...],
    nullable: bool,
    args: list[_Arg],
) -> None:
    dest = "a_" + "__".join(path)
    kind = node.get("type")
    convert: Callable[[Any], Any] = lambda v: v  # noqa: E731
    kw: dict[str, Any] = {"help": _help(node)}
    if kind == "integer":
        kw["type"] = int
    elif kind == "number":
        kw["type"] = float
    if "enum" in node:
        kw["choices"] = [*node["enum"], "none"] if nullable else list(node["enum"])
        if nullable:
            convert = lambda v: None if v == "none" else v  # noqa: E731
    if key in _FILE_PARAMS:
        kw["metavar"] = "FILE"
        kw["help"] += " Give a file, or '-' for standard input."
        convert = _read_text
    elif key in _PATH_PARAMS:
        convert = os.path.abspath

    if required:
        if kind == "array":
            kw["nargs"] = "+"
        parser.add_argument(dest, metavar=kw.pop("metavar", key.upper()), **kw)
    elif key in _POSITIONAL_OPTIONAL:
        parser.add_argument(dest, metavar=key.upper(), nargs="?", **kw)
    else:
        flags = ["--" + key.replace("_", "-")]
        if "choices" not in kw:
            kw["metavar"] = key.upper()
        if kind == "boolean":
            kw.pop("metavar")
            kw["action"] = argparse.BooleanOptionalAction
        elif kind == "array":
            kw["action"] = "append"
            kw["metavar"] = key.rstrip("s").upper()
            if key.endswith("s"):
                flags.append(flags[0][:-1])  # --tag for --tags
        elif kind == "object":
            kw.update(action="append", type=_kv, metavar="KEY=VALUE")
            convert = dict
        parser.add_argument(*flags, dest=dest, default=None, **kw)
    args.append(_Arg(dest, path, convert))


def tool_parser(server: LogServer, name: str) -> tuple[argparse.ArgumentParser, list[_Arg]]:
    """The parser of one tool's command, from the tool's input schema."""
    tool = server.mcp._tool_manager.get_tool(name)
    if tool is None:
        raise ToolError(f"tool '{name}' is turned off by the policy (allowed_tools)")
    schema: dict[str, Any] = tool.parameters
    fields = tool.fn_metadata.arg_model.model_fields
    title = next(t for n, _, t in TOOL_SPECS if n == name)
    doc = (tool.description or title).strip()
    p = argparse.ArgumentParser(
        prog=f"{PROG} {name}",
        description=doc,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    args: list[_Arg] = []
    required = set(schema.get("required", []))
    groups: list[tuple[str, dict[str, Any]]] = []
    for key, raw in schema["properties"].items():
        node = _resolve(schema, raw)
        if node.get("type") == "object" and "properties" in node:
            groups.append((key, node))  # the record filter: its fields become options
            continue
        _add(p, key, node, key in required, (key,), _nullable(fields.get(key)), args)
    for key, node in groups:
        g = p.add_argument_group(key, str(node.get("description", "")))
        for sub, sub_node in node["properties"].items():
            if sub in schema["properties"]:
                raise RuntimeError(f"{name}: {key}.{sub} clashes with the parameter {sub}")
            _add(g, sub, sub_node, False, (key, sub), False, args)
    p.add_argument("--json", action="store_true", help="print the structured content as JSON")
    if name == "add_source":
        p.add_argument("-q", "--quiet", action="store_true", help="no progress on standard error")
    return p, args


def _arguments(ns: argparse.Namespace, spec: list[_Arg]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for a in spec:
        value = getattr(ns, a.dest)
        if value is None:
            continue
        node = out
        for part in a.path[:-1]:
            node = node.setdefault(part, {})
        node[a.path[-1]] = a.convert(value)
    return out


def _utf8_stdout() -> None:
    """Tool texts use ×, …, ⏎ and block characters; a Windows console or pipe in a legacy code
    page cannot encode them, so print UTF-8 there."""
    try:
        "×…⏎▁█·".encode(sys.stdout.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        reconfigure = getattr(sys.stdout, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def _clean(message: str, name: str) -> str:
    """A tool error as the CLI prints it: without the MCP prefix and pydantic's help links."""
    message = message.removeprefix(f"Error executing tool {name}: ")
    lines = [ln for ln in message.splitlines() if "errors.pydantic.dev" not in ln]
    return "\n".join(lines)


def run_tool(config: Config, policy: Policy, command: str, argv: Sequence[str]) -> int:
    """Parse one command's arguments, call its tool and print the result."""
    name = normalize(command)
    if name not in {n for n, _, _ in TOOL_SPECS}:
        print(f"{PROG}: unknown command '{command}'\n\n{commands_help()}", file=sys.stderr)
        return 2
    _utf8_stdout()
    # Structured content always (the text is the same): --json and check_citations' verdict use it.
    config = replace(config, structured_output=True)
    server = LogServer(config, policy, session=f"cli-{os.getpid()}", wait_for_imports=True)
    try:
        parser, spec = tool_parser(server, name)
    except ToolError as e:
        print(f"{PROG}: {e}", file=sys.stderr)
        return 2
    ns = parser.parse_args(argv)
    if name == "add_source" and not ns.quiet:
        server.app.ingestor.progress = lambda m: print(m, file=sys.stderr)
    try:
        arguments = _arguments(ns, spec)
        result = asyncio.run(server.mcp.call_tool(name, arguments))
    except Exception as e:  # ToolError: the message says how to fix the call; anything else: a bug
        cause = e.__cause__ if type(e).__name__ == "UnexpectedToolError" and e.__cause__ else e
        print(f"{PROG} {name}: {_clean(str(cause), name)}", file=sys.stderr)
        return 1
    structured = getattr(result, "structured_content", None)
    if ns.json and structured is not None:
        print(json.dumps(structured, ensure_ascii=False, indent=2))
    else:
        content = getattr(result, "content", [])
        print("\n".join(c.text for c in content if getattr(c, "type", None) == "text"))
    if name == "check_citations" and isinstance(structured, dict) and structured.get("ok") is False:
        return 1
    return 0
