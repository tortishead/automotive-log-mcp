"""Command line: ``automotive-log-mcp serve`` runs the MCP server over stdio.

Without an agent: ``index <case> <path>`` indexes a log file, archive or directory;
``sources <case>`` lists what a case holds; ``query <case> --search ...`` searches it;
``relink <case> <source> <path>`` points a case at a moved original (the hash must match);
``reindex <case> <source>`` indexes an original again; ``artifacts <case>`` lists tombstones,
ANR traces, Java crashes and dropbox entries, ``artifacts <case> --show art:<id>[/section][:L<n>]``
prints one. ``overview``, ``count``, ``detect`` and ``check`` print the output of the MCP tools of
the same names (``check`` runs check_citations on a Markdown file); ``rules`` lists the detector
rules and where they come from. ``calls`` analyses agent sessions from the call log (optionally
joined with a Claude Code transcript): where the tokens went, why a run failed, what to change.
"""

from __future__ import annotations

import argparse
import dataclasses
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .app import App
from .config import CASE_NAME_RE, Config, ConfigError, load_config
from .connectors.base import ConnectorError
from .core.ingest import IngestError
from .core.levels import LEVEL_RANK
from .detect.engine import BUILTIN_DIR
from .policy import Policy, load_policy
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
    se = sub.add_parser("serve", help="run the MCP server over stdio")
    se.add_argument(
        "--structured",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="send structuredContent and output schemas (default: config structured_output, false)",
    )

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
    _filter_args(qu)
    qu.add_argument("--collapse", "-c", action="store_true", help="one line per repeated message")
    qu.add_argument("--limit", "-n", type=int, default=50)
    qu.add_argument("--cursor", help="next page, as a previous result printed it")
    qu.add_argument("--count", action="store_true", help="print only the number of matches")

    ov = sub.add_parser("overview", help="levels, top tags and messages, gaps, bursts, detector counts")
    ov.add_argument("case", type=_case)
    _filter_args(ov)

    co = sub.add_parser("count", help="count records by tag, level, pid, source or template")
    co.add_argument("case", type=_case)
    co.add_argument("--by", choices=["tag", "level", "pid", "source", "template", "none"], default="tag")
    co.add_argument("--search", "-s", help="count only records matching this full-text query")
    co.add_argument("--regex", "-r", help="count only records matching this regular expression")
    co.add_argument("--bucket", "-b", help="time bucket, e.g. 10s, 1m, 1h (at most 60 buckets)")
    co.add_argument("--limit", "-n", type=int, default=20)
    _filter_args(co)

    de = sub.add_parser("detect", help="run the detector rules over a case")
    de.add_argument("case", type=_case)
    de.add_argument("--rule", action="append", help="rule name; repeatable (default: all rules)")
    de.add_argument("--examples", type=int, default=3, help="example lines per rule (1-10)")
    _filter_args(de)

    ch = sub.add_parser("check", help="check the citations of a Markdown answer (exit 1 on errors)")
    ch.add_argument("case", type=_case)
    ch.add_argument("file", type=Path, help="Markdown file; '-' reads standard input")

    ru = sub.add_parser("rules", help="list the detector rules (package and configured rule_dirs)")
    ru.add_argument("--show", metavar="NAME", help="print one rule's definition")

    ar = sub.add_parser("artifacts", help="list artifacts (tombstones, ANRs, crashes, dropbox) or show one")
    ar.add_argument("case", type=_case)
    ar.add_argument("--type", choices=["tombstone", "anr", "java_crash", "dropbox"])
    ar.add_argument(
        "--show",
        metavar="CITATION",
        help="art:<id> (summary and sections), art:<id>/<section> or art:<id>[/<section>]:L<n>[-L<m>]",
    )

    ca = sub.add_parser("calls", help="analyse an agent session: calls, bytes, tokens, problems, suggestions")
    ca.add_argument("--log", type=Path, help="call log (default: config call_log, <home>/calllog.jsonl)")
    ca.add_argument("--session", action="append", help="session id (prefix ok); repeatable")
    ca.add_argument(
        "--last", type=int, nargs="?", const=1, metavar="N", help="the last N sessions (default 1)"
    )
    ca.add_argument("--list", type=int, nargs="?", const=15, metavar="N", help="list the last N sessions")
    ca.add_argument(
        "--transcript",
        type=Path,
        help="Claude Code session .jsonl or 'claude -p --output-format stream-json' output, for real "
        "token counts; picks the matching session unless --session is given",
    )
    ca.add_argument(
        "--reads",
        choices=["structured", "text", "both"],
        default="structured",
        help="what the client passes to the model when a result has structured content "
        "(Claude Code: structured; without a transcript this is an assumption)",
    )
    ca.add_argument("--fixed", action="store_true", help="print the fixed cost per tool and exit")

    rl = sub.add_parser("relink", help="point a case at a moved original file (hash must match)")
    rl.add_argument("case", type=_case)
    rl.add_argument("source", type=int)
    rl.add_argument("path", type=Path)

    ri = sub.add_parser("reindex", help="index the original of a source again")
    ri.add_argument("case", type=_case)
    ri.add_argument("source", type=int)
    ri.add_argument("-q", "--quiet", action="store_true")
    return p


def _filter_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--level", "-l", choices=list(LEVEL_RANK), help="minimum level")
    p.add_argument("--tag", "-t", action="append", help="tag (glob); repeatable")
    p.add_argument("--not-tag", action="append", help="tag to exclude (glob); repeatable")
    p.add_argument("--source", type=int, action="append", help="source id; repeatable")
    p.add_argument("--pid", type=int)
    p.add_argument(
        "--from", dest="from_", metavar="TIME", help="start time, as printed, relative or a citation"
    )
    p.add_argument("--to", metavar="TIME", help="end time (inclusive)")


def _filter(args: argparse.Namespace) -> Filter | None:
    flt = {
        "sources": [str(x) for x in args.source] if args.source else None,
        "from": args.from_,
        "to": args.to,
        "level": args.level,
        "tags": args.tag,
        "not_tags": args.not_tag,
        "pid": args.pid,
    }
    values = {k: v for k, v in flt.items() if v is not None}
    return Filter.model_validate(values) if values else None


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
    tools = Tools(app)
    try:
        out = tools.search(
            args.case,
            args.regex or args.search,
            mode="regex" if args.regex else "text",
            filter=_filter(args) or Filter.model_validate({}),
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


def _tool(app: App, args: argparse.Namespace) -> int:
    """overview, count, detect and check: print the MCP tool's text."""
    tools = Tools(app)
    try:
        if args.command == "overview":
            out = tools.overview(args.case, _filter(args))
        elif args.command == "count":
            if args.search and args.regex:
                _err("give --search or --regex, not both")
                return 2
            out = tools.count(
                args.case,
                None if args.by == "none" else args.by,
                args.regex or args.search,
                "regex" if args.regex else "text",
                _filter(args),
                args.bucket,
                args.limit,
            )
        elif args.command == "detect":
            out = tools.detect(args.case, args.rule, _filter(args), max(1, min(args.examples, 10)))
        else:
            text = sys.stdin.read() if str(args.file) == "-" else args.file.read_text(encoding="utf-8")
            out = tools.check_citations(args.case, text)
    except (ToolInputError, OSError) as e:
        _err(str(e))
        return 1 if "does not exist" in str(e) else 2
    print(out.text)
    if args.command == "check":
        return 0 if out.data.ok else 1  # type: ignore[attr-defined]
    return 0


def _rules(app: App, show: str | None) -> int:
    rs = app.rules
    if show:
        r = rs.rules.get(show)
        if r is None:
            _err(f"no rule '{show}'; rules: {', '.join(sorted(rs.rules))}")
            return 2
        print(f"# {r.origin}")
        print(Path(r.origin).read_text(encoding="utf-8").rstrip())
        return 0
    for r in rs.ordered():
        where = "package" if Path(r.origin).is_relative_to(BUILTIN_DIR) else r.origin
        extra = f"  (replaces {rs.overridden[r.name]})" if r.name in rs.overridden else ""
        print(f"{r.name:<20} {r.severity:<7} {r.description}  [{where}]{extra}")
    return 0


def _fixed(config: Config, policy: Policy) -> int:
    from .server import LogServer
    from .usage import est_tokens

    fc = LogServer(config, policy, session="fixed-cost").fixed_cost()
    print(f"{'tool':<16} {'model bytes':>11} {'~tokens':>8} {'output schema':>14}")
    for t in sorted(fc.tools, key=lambda t: -t.model_bytes):
        print(
            f"{t.name:<16} {t.model_bytes:>11,} {est_tokens(t.model_bytes):>8,} {t.output_schema_bytes:>14,}"
        )
    print(f"{'instructions':<16} {fc.instructions_bytes:>11,} {est_tokens(fc.instructions_bytes):>8,}")
    print(
        f"{'per request':<16} {fc.model_bytes:>11,} {est_tokens(fc.model_bytes):>8,}  "
        f"(structured output {'on' if config.structured_output else 'off'}; tokens ≈ bytes/4)"
    )
    return 0


def _calls(config: Config, policy: Policy, args: argparse.Namespace) -> int:
    from . import callreport as cr

    if args.fixed:
        return _fixed(config, policy)
    path = args.log or config.call_log
    if path is None or not path.is_file():
        _err(f"no call log at {path} (config call_log)")
        return 2
    sessions = [s for s in cr.read_calllog(path) if s.calls]
    if not sessions:
        _err(f"no calls in {path}")
        return 1
    if args.list:
        for s in sessions[-args.list :]:
            print(cr.session_line(s, args.reads))
        return 0
    transcript = cr.read_transcript(args.transcript) if args.transcript else None
    if args.session:
        chosen = [s for s in sessions if any(s.id.startswith(p) for p in args.session)]
        if not chosen:
            _err(f"no session {', '.join(args.session)} in {path}; try --list")
            return 1
    elif transcript is not None:
        best = max(sessions, key=lambda s: (cr.match_score(transcript, s), s.last))
        if cr.match_score(transcript, best) == 0:
            _err("no session in the call log matches the transcript's calls; give --session")
            return 1
        chosen = [best]
    else:
        chosen = sessions[-(args.last or 1) :]
    for i, s in enumerate(chosen):
        if i:
            print()
        print(cr.analyse(s, transcript, args.reads).text())
    return 0


def _run(app: App, args: argparse.Namespace) -> int:
    if args.command == "index":
        if not args.quiet:
            app.ingestor.progress = lambda m: print(m, file=sys.stderr)
        result = app.ingestor.add(app.config.case_db(args.case), str(args.path), args.format, args.force)
        for f in result.files:
            if f.other_case:
                print(
                    f"{f.path}: already indexed in case '{f.other_case}' (sources "
                    f"{', '.join(map(str, f.source_ids))}); use that case, or --force to index it here too"
                )
            elif f.existing:
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
    if args.command in ("overview", "count", "detect", "check"):
        return _tool(app, args)
    if args.command == "rules":
        return _rules(app, args.show)
    raise AssertionError(args.command)


def _utf8_stdout() -> None:
    """Tool texts use ×, …, ⏎ and block characters; a Windows console or pipe in a legacy code
    page cannot encode them, so print UTF-8 there."""
    try:
        "×…⏎▁█·".encode(sys.stdout.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        reconfigure = getattr(sys.stdout, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command != "serve":
        _utf8_stdout()
    try:
        check_fts5()
        config = load_config(args.config)
        policy = load_policy(config.policy_file)
        if args.command == "calls":
            return _calls(config, policy, args)
        if args.command == "serve":
            from .server import build_server

            if args.structured is not None:
                config = dataclasses.replace(config, structured_output=args.structured)
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
