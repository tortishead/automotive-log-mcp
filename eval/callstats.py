"""Summarise the server's call log for agent runs: calls, bytes and tools per session and case.

    python eval/callstats.py                                  # default call log, all sessions
    python eval/callstats.py --case syn-hvac-crash            # one case
    python eval/callstats.py --since 2026-11-10T09:00 --markdown
    py eval\\callstats.py %USERPROFILE%\\.automotive-log-mcp\\calllog.jsonl --case T-1   (Windows)

The call log (``calllog.jsonl`` in the server home, see README "Files") has one line per tool
call: time, session, tool, redacted arguments, status, duration and output bytes; never log
content. One server process is one session: start a fresh agent chat (and so a fresh server) for
each run to keep runs apart.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def default_log() -> Path:
    home = os.environ.get("AUTOMOTIVE_LOG_MCP_HOME")
    return (Path(home).expanduser() if home else Path.home() / ".automotive-log-mcp") / "calllog.jsonl"


@dataclass
class Run:
    session: str
    cases: set[str] = field(default_factory=set)
    first: str = ""
    last: str = ""
    calls: int = 0
    bytes: int = 0
    ms: float = 0.0
    errors: int = 0
    refused: int = 0
    tools: Counter[str] = field(default_factory=Counter)
    checks: int = 0  # check_citations calls


def read(path: Path, case: str | None, session: str | None, since: str | None) -> list[Run]:
    runs: dict[str, Run] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                continue
            args = e.get("args") or {}
            if case and args.get("case") != case:
                continue
            if session and e.get("session") != session:
                continue
            if since and str(e.get("ts", "")) < since:
                continue
            r = runs.setdefault(e.get("session", "?"), Run(e.get("session", "?")))
            if isinstance(args.get("case"), str):
                r.cases.add(args["case"])
            ts = str(e.get("ts", ""))
            r.first = r.first or ts
            r.last = ts
            r.calls += 1
            r.bytes += int(e.get("bytes_out") or 0)
            r.ms += float(e.get("duration_ms") or 0)
            r.errors += e.get("status") == "error"
            r.refused += e.get("status") == "refused"
            r.tools[str(e.get("tool"))] += 1
            r.checks += e.get("tool") == "check_citations"
    return list(runs.values())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument(
        "log", nargs="?", type=Path, default=None, help="call log (default: <home>/calllog.jsonl)"
    )
    ap.add_argument("--case", help="only calls for this case")
    ap.add_argument("--session", help="only this session id")
    ap.add_argument("--since", help="only calls at or after this ISO time (UTC), e.g. 2026-11-10T09:00")
    ap.add_argument("--markdown", action="store_true", help="a Markdown table for the run notes")
    args = ap.parse_args(argv)
    path = args.log or default_log()
    if not path.is_file():
        print(f"callstats: no call log at {path}", file=sys.stderr)
        return 2
    runs = read(path, args.case, args.session, args.since)
    if not runs:
        print("callstats: no matching calls", file=sys.stderr)
        return 1
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.markdown:
        print("| session | case | first call (UTC) | calls | bytes | errors | check_citations | tools |")
        print("|---|---|---|---|---|---|---|---|")
    for r in runs:
        tools = ", ".join(f"{t} {n}" for t, n in r.tools.most_common())
        cases = ", ".join(sorted(r.cases)) or "-"
        if args.markdown:
            print(
                f"| {r.session} | {cases} | {r.first[:19]} | {r.calls} | {r.bytes:,} | {r.errors} | "
                f"{r.checks} | {tools} |"
            )
        else:
            print(
                f"session {r.session}  case {cases}  {r.first[:19]} .. {r.last[:19]}  {r.calls} calls  "
                f"{r.bytes:,} bytes  {r.ms / 1000:.1f} s in tools  {r.errors} errors"
                + (f"  {r.refused} refused" if r.refused else "")
                + f"  check_citations {r.checks}x\n  {tools}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
