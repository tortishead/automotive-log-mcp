"""``automotive-log-mcp`` runs the MCP server (stdio; ``--http``: streamable HTTP).

``automotive-log-mcp <tool> <case> ...`` runs one tool as a command instead (see ``toolcli``).
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config
from .policy import load_policy
from .store.db import StoreError, check_fts5
from .toolcli import commands_help


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="automotive-log-mcp",
        usage="%(prog)s [--config FILE] [server options]\n       %(prog)s [--config FILE] <command> ...",
        description="Run the MCP server (stdio, or streamable HTTP with --http), or one tool as a command.",
        epilog=commands_help(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--config", type=Path, help="config file (default: <home>/config.yaml)")
    p.add_argument(
        "--http",
        action="store_true",
        help="streamable HTTP with a bearer token (AUTOMOTIVE_LOG_MCP_TOKEN or http.token_file) instead of "
        "stdio; add_source reads only from the policy's allowed_roots",
    )
    p.add_argument("--host", help="HTTP: address to bind (default: config http.host, 127.0.0.1)")
    p.add_argument("--port", type=int, help="HTTP: port (default: config http.port, 8765)")
    p.add_argument(
        "--structured",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="send structuredContent and output schemas (default: config structured_output, false)",
    )
    return p


_VALUE_OPTIONS = frozenset({"--config", "--host", "--port"})


def _split(argv: Sequence[str]) -> tuple[list[str], str | None, list[str]]:
    """Server options, the command (the first word that is not an option or its value) and its
    arguments."""
    for i, a in enumerate(argv):
        if a == "--":
            break
        if not a.startswith("-") and (i == 0 or argv[i - 1] not in _VALUE_OPTIONS):
            return list(argv[:i]), a, list(argv[i + 1 :])
    return list(argv), None, []


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    head, command, rest = _split(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(head)
    if command is not None and (args.http or args.host or args.port or args.structured is not None):
        parser.error("--http, --host, --port and --structured are for the server, not a command")
    try:
        check_fts5()
        config = load_config(args.config)
        policy = load_policy(config.policy_file)
        if command is not None:
            from .toolcli import run_tool

            return run_tool(config, policy, command, rest)
        if args.structured is not None:
            config = dataclasses.replace(config, structured_output=args.structured)
        if args.host or args.port:
            if not args.http:
                raise ConfigError("--host and --port need --http")
            http = dataclasses.replace(
                config.http, host=args.host or config.http.host, port=args.port or config.http.port
            )
            config = dataclasses.replace(config, http=http)
        if args.http:
            from .httpserve import serve_http

            serve_http(config, policy)
            return 0
        from .server import build_server

        server = build_server(config, policy)
    except (ConfigError, StoreError) as e:
        # stdout belongs to the MCP protocol; errors go to stderr.
        print(f"automotive-log-mcp: {e}", file=sys.stderr)
        return 2
    server.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
