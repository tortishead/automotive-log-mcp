"""Command line: ``automotive-log-mcp serve`` runs the MCP server over stdio.

Without an agent: ``index <case> <path>`` indexes a log file, archive or directory;
``sources <case>`` lists what a case holds; ``query <case> --search ...`` searches it;
``relink <case> <source> <path>`` points a case at a moved original (the hash must match);
``reindex <case> <source>`` indexes an original again. ``rules`` arrives with M4.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .app import App
from .config import CASE_NAME_RE, ConfigError, load_config
from .connectors.base import ConnectorError
from .core.ingest import IngestError
from .core.levels import LEVEL_RANK
from .policy import load_policy
from .store import db, query
from .store.db import StoreError, check_fts5


def _case(value: str) -> str:
    if not re.fullmatch(CASE_NAME_RE, value):
        raise argparse.ArgumentTypeError(
            f"invalid case name {value!r}: letters, digits, '.', '_' and '-', at most 64 characters"
        )
    return value


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="automotive-log-mcp", description=__doc__.splitlines()[0])
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--config", type=Path, help="config file (default: <home>/config.yaml)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the MCP server over stdio")

    ix = sub.add_parser("index", help="index a log file, archive or directory into a case")
    ix.add_argument("case", type=_case)
    ix.add_argument("path", type=Path)
    ix.add_argument("--format", help="force a connector (default: sniff)")
    ix.add_argument("--force", action="store_true", help="reindex even if the file is already in the case")
    ix.add_argument("-q", "--quiet", action="store_true", help="no progress on stderr")

    so = sub.add_parser("sources", help="list the sources of a case")
    so.add_argument("case", type=_case)

    qu = sub.add_parser("query", help="search the records of a case")
    qu.add_argument("case", type=_case)
    qu.add_argument("--search", "-s", help="full-text query (FTS5) over message and tag")
    qu.add_argument("--regex", "-r", help="regular expression over message and tag")
    qu.add_argument("--level", "-l", choices=list(LEVEL_RANK), help="minimum level")
    qu.add_argument("--tag", "-t", action="append", help="tag (glob); repeatable")
    qu.add_argument("--source", type=int, action="append", help="source id; repeatable")
    qu.add_argument("--pid", type=int)
    qu.add_argument("--limit", "-n", type=int, default=50)
    qu.add_argument("--count", action="store_true", help="print only the number of matches")

    rl = sub.add_parser("relink", help="point a case at a moved original file (hash must match)")
    rl.add_argument("case", type=_case)
    rl.add_argument("source", type=int)
    rl.add_argument("path", type=Path)

    ri = sub.add_parser("reindex", help="index the original of a source again")
    ri.add_argument("case", type=_case)
    ri.add_argument("source", type=int)
    ri.add_argument("-q", "--quiet", action="store_true")
    return p


def _err(msg: str) -> None:
    print(f"automotive-log-mcp: {msg}", file=sys.stderr)


def _print_sources(app: App, case: str) -> int:
    rows = app.list_sources(case)
    if rows is None:
        _err(f"case '{case}' does not exist")
        return 1
    if not rows:
        print(f"No sources in case '{case}'.")
    for s in rows:
        t0, t1 = query.time_range(s)
        flags = [s.status] if s.status != "done" else []
        if s.original_missing:
            flags.append("original missing")
        if s.stale:
            flags.append("stale: reindex recommended")
        extra = f"  [{', '.join(flags)}]" if flags else ""
        print(f"{s.id:>4}  {s.format:<8} {s.records:>10} rec  {t0 or '-'} .. {t1 or '-'}  {s.name}{extra}")
        if s.error:
            print(f"      error: {s.error}")
    return 0


def _run(app: App, args: argparse.Namespace) -> int:
    if args.command == "index":
        if not args.quiet:
            app.ingestor.progress = lambda m: print(m, file=sys.stderr)
        result = app.ingestor.add(app.config.case_db(args.case), str(args.path), args.format, args.force)
        for f in result.files:
            if f.existing:
                print(
                    f"{f.path}: already in case '{args.case}' (sources {', '.join(map(str, f.source_ids))})"
                )
            else:
                rate = f.lines / f.seconds if f.seconds else 0
                print(
                    f"{f.path}: {f.records} records from {f.lines} lines in {f.seconds:.1f} s "
                    f"({rate:,.0f} lines/s); sources {', '.join(map(str, f.source_ids))}"
                )
            for n in f.notes:
                print(f"  note: {n}")
        for s in result.skipped:
            print(f"  skipped: {s}")
        return 0
    if args.command == "sources":
        return _print_sources(app, args.case)
    if args.command == "relink":
        ids = app.relink(args.case, args.source, args.path)
        print(f"relinked sources {', '.join(map(str, ids))} to {args.path.expanduser().resolve()}")
        return 0
    if args.command == "reindex":
        if not args.quiet:
            app.ingestor.progress = lambda m: print(m, file=sys.stderr)
        result = app.ingestor.reindex(app.config.case_db(args.case), args.source)
        print(f"reindexed: sources {', '.join(map(str, result.source_ids))}")
        return 0
    if args.command == "query":
        rows = app.list_sources(args.case)
        if rows is None:
            _err(f"case '{args.case}' does not exist")
            return 1
        visible = {s.id: s.ts_format for s in rows}
        wanted = args.source or list(visible)
        unknown = [s for s in wanted if s not in visible]
        if unknown:
            _err(f"unknown source ids: {', '.join(map(str, unknown))}")
            return 2
        q = query.Query(
            text=args.search,
            regex=args.regex,
            min_level=args.level,
            tags=args.tag,
            sources=wanted,
            pid=args.pid,
            limit=args.limit,
        )
        if q.regex:
            try:
                re.compile(q.regex)
            except re.error as e:
                _err(f"invalid regex: {e}")
                return 2
        if q.text:
            q.text = app.redactor.redact(q.text)  # raw values are stored as tokens
        with db.opened(app.config.case_db(args.case)) as conn:
            try:
                hits, total = query.search(conn, q, visible)
            except sqlite3.OperationalError as e:
                _err(f"invalid search query: {e}")
                return 2
        if args.count:
            print(total)
            return 0
        for h in hits:
            print(query.format_hit(h, app.config.caps.max_msg_chars))
        if total > len(hits):
            print(f"… {total - len(hits)} more; narrow with --level, --tag, --source or a longer query")
        elif total == 0:
            print("No matches.")
        return 0
    raise AssertionError(args.command)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        check_fts5()
        config = load_config(args.config)
        policy = load_policy(config.policy_file)
        if args.command == "serve":
            from .server import build_server

            server = build_server(config, policy)
        else:
            app = App.create(config, policy)
    except (ConfigError, StoreError) as e:
        # stdout belongs to the MCP protocol; errors go to stderr.
        _err(str(e))
        return 2
    if args.command == "serve":
        server.run("stdio")
        return 0
    try:
        return _run(app, args)
    except (IngestError, ConnectorError, StoreError) as e:
        _err(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
