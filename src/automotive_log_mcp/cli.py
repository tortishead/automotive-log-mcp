"""Command line: ``automotive-log-mcp serve`` runs the MCP server over stdio.

``index``, ``query`` and ``rules`` (working without an agent) arrive with M1 and M4.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config
from .policy import load_policy
from .server import build_server
from .store.db import StoreError, check_fts5


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="automotive-log-mcp", description=__doc__.splitlines()[0])
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--config", type=Path, help="config file (default: <home>/config.yaml)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the MCP server over stdio")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        check_fts5()
        config = load_config(args.config)
        policy = load_policy(config.policy_file)
        server = build_server(config, policy)
    except (ConfigError, StoreError) as e:
        # stdout belongs to the MCP protocol; errors go to stderr.
        print(f"automotive-log-mcp: {e}", file=sys.stderr)
        return 2
    if args.command == "serve":
        server.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
