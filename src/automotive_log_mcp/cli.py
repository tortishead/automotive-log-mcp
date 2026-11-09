"""Command line: ``automotive-log-mcp serve`` runs the MCP server over stdio.

Without an agent: ``index <case> <path>`` indexes a log file, archive or directory;
``sources <case>`` lists what a case holds; ``query <case> --search ...`` searches it;
``relink <case> <source> <path>`` points a case at a moved original (the hash must match);
``reindex <case> <source>`` indexes an original again; ``artifacts <case>`` lists tombstones,
ANR traces, Java crashes and dropbox entries, ``artifacts <case> --show art:<id>[/section][:L<n>]``
prints one. ``rules`` arrives with M4.
"""

from __future__ import annotations

import argparse
import re
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
from .store import query
from .store.db import StoreError, check_fts5
from .tools import Filter, ToolInputError, Tools


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
    qu.add_argument("--not-tag", action="append", help="tag to exclude (glob); repeatable")
    qu.add_argument("--pid", type=int)
    qu.add_argument(
        "--from", dest="from_", metavar="TIME", help="start time, as printed, relative or a citation"
    )
    qu.add_argument("--to", metavar="TIME", help="end time (inclusive)")
    qu.add_argument("--collapse", "-c", action="store_true", help="one line per repeated message")
    qu.add_argument("--limit", "-n", type=int, default=50)
    qu.add_argument("--cursor", help="next page, as a previous result printed it")
    qu.add_argument("--count", action="store_true", help="print only the number of matches")

    ar = sub.add_parser("artifacts", help="list artifacts (tombstones, ANRs, crashes, dropbox) or show one")
    ar.add_argument("case", type=_case)
    ar.add_argument("--type", choices=["tombstone", "anr", "java_crash", "dropbox"])
    ar.add_argument(
        "--show",
        metavar="CITATION",
        help="art:<id> (summary and sections), art:<id>/<section> or art:<id>[/<section>]:L<n>[-L<m>]",
    )

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


def _print_artifacts(app: App, case: str, type: str | None) -> int:
    rows = app.list_artifacts(case, type)
    if rows is None:
        _err(f"case '{case}' does not exist")
        return 1
    if not rows:
        print(f"No artifacts in case '{case}'" + (f" of type {type}." if type else "."))
    for a in rows:
        when = query.fmt_ns(a.ts) or "-"
        who = f"pid {a.pid}" if a.pid is not None else "pid -"
        print(f"{a.cite:<7} {a.type:<10} {when:<23}  {who:<10} {a.process or '-'}  {a.signature or '-'}")
    return 0


def _show_artifact(app: App, case: str, cite: str) -> int:
    try:
        a, lines = app.read_artifact(case, cite)
    except ValueError as e:
        _err(str(e))
        return 2
    if lines is not None:
        width = len(str(lines[-1][0])) if lines else 1
        for n, text in lines:
            print(f"L{n:<{width}}  {text}")
        return 0
    when = query.fmt_ns(a.ts) or "unknown"
    print(f"{a.cite}  {a.type}  record {a.record_cite}")
    print(f"name: {a.name}")
    print(f"time: {when}" + (f"  (as written: {a.ts_raw})" if a.ts_raw and a.ts_raw != when else ""))
    if a.process or a.pid is not None:
        print(f"process: {a.process or '-'}  pid {a.pid if a.pid is not None else '-'}  tid {a.tid or '-'}")
    if a.signature:
        print(f"signature: {a.signature}")
    print()
    print(a.summary)
    print()
    print(f"sections ({a.lines} lines; cite lines as {a.cite}/<section>:L<n>):")
    width = max((len(s.name) for s in a.sections), default=0)
    for s in a.sections:
        print(f"  {a.cite}/{s.name:<{width}}  L{s.first_line}-L{s.last_line}")
    return 0


def _query(app: App, args: argparse.Namespace) -> int:
    if args.search and args.regex:
        _err("give --search or --regex, not both")
        return 2
    flt = {
        "sources": [str(x) for x in args.source] if args.source else None,
        "from": args.from_,
        "to": args.to,
        "level": args.level,
        "tags": args.tag,
        "not_tags": args.not_tag,
        "pid": args.pid,
    }
    tools = Tools(app)
    try:
        out = tools.search(
            args.case,
            args.regex or args.search,
            mode="regex" if args.regex else "text",
            filter=Filter.model_validate({k: v for k, v in flt.items() if v is not None}),
            collapse=args.collapse,
            count_only=args.count,
            limit=args.limit,
            cursor=args.cursor,
        )
    except ToolInputError as e:
        _err(str(e))
        return 1 if "does not exist" in str(e) else 2
    if args.count:
        print(out.data.total)  # type: ignore[attr-defined]
    else:
        print(out.text)
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
    if args.command == "artifacts":
        if args.show:
            return _show_artifact(app, args.case, args.show)
        return _print_artifacts(app, args.case, args.type)
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
        return _query(app, args)
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
