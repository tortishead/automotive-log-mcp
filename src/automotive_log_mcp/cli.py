"""``automotive-log-mcp`` runs the MCP server over stdio.

``automotive-log-mcp <tool> <case> ...`` runs one tool as a command instead (see ``toolcli``).
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, load_config
from .policy import load_policy
from .store.db import StoreError, check_fts5
from .toolcli import commands_help


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="automotive-log-mcp",
        usage="%(prog)s [--config FILE] [server options]\n       %(prog)s [--config FILE] <command> ...",
        description="Run the MCP server (stdio), or one tool as a command.",
        epilog=commands_help(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--config", type=Path, help="config file (default: <home>/config.yaml)")
    p.add_argument(
        "--structured",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="send structuredContent and output schemas (default: config structured_output, false)",
    )
    return p


_VALUE_OPTIONS = frozenset({"--config"})


def _split(argv: Sequence[str]) -> tuple[list[str], str | None, list[str]]:
    """Server options, the command (the first word that is not an option or its value) and its
    arguments."""
    for i, a in enumerate(argv):
        if a == "--":
            break
        if not a.startswith("-") and (i == 0 or argv[i - 1] not in _VALUE_OPTIONS):
            return list(argv[:i]), a, list(argv[i + 1 :])
    return list(argv), None, []


def _cleanup(config: Config) -> None:
    """Retention rules at server start; never fatal. Messages go to stderr (stdout is MCP)."""
    from .retention import cleanup

    try:
        for line in cleanup(config):
            print(f"automotive-log-mcp: {line}", file=sys.stderr)
    except Exception as e:
        print(f"automotive-log-mcp: cleanup skipped: {e}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    head, command, rest = _split(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(head)
    if command is not None and args.structured is not None:
        parser.error("--structured is for the server, not a command")
    try:
        check_fts5()
        config = load_config(args.config)
        policy = load_policy(config.policy_file)
        if command is not None:
            from .toolcli import run_tool

            return run_tool(config, policy, command, rest)
        if args.structured is not None:
            config = dataclasses.replace(config, structured_output=args.structured)
        from .server import build_server

        server = build_server(config, policy)
        _cleanup(config)
    except (ConfigError, StoreError) as e:
        # stdout belongs to the MCP protocol; errors go to stderr.
        print(f"automotive-log-mcp: {e}", file=sys.stderr)
        return 2
    server.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
