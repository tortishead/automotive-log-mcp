"""``automotive-log-mcp`` runs the MCP server (stdio; ``--http``: streamable HTTP)."""

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


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="automotive-log-mcp", description=__doc__.splitlines()[0])
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


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        check_fts5()
        config = load_config(args.config)
        policy = load_policy(config.policy_file)
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
