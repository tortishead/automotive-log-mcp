"""Evidence recall: do the tools surface a case's root-cause citations without an agent?

For each case in one or more case files (format in eval/cases.yaml) the script indexes the files
into a case named after the case id, then runs what an agent sees first: ``detect``,
``list_artifacts`` and the case's recorded ``queries`` (first page each, default arguments). An
expected citation counts as

- **exact** when one of these outputs prints it,
- **artifact** when it is an artifact line (``art:<id>/…:L<n>``) and the outputs print its
  artifact ``art:<id>`` (the next step, read_artifact, shows the line),
- **missed** otherwise.

Recall = (exact + artifact) / expected. Every expected citation must also resolve in the case
(check_citations); one that does not means the case definition is out of date.

    python eval/evidence_recall.py eval/cases.yaml                       # temporary home, deleted
    python eval/evidence_recall.py eval/cases.yaml eval/cases.local.yaml --report eval/results/recall.md
    python eval/evidence_recall.py eval/cases.local.yaml --home ~/.automotive-log-mcp   # keep the cases
    python eval/evidence_recall.py eval/cases.yaml --synthetic-only --min-recall 1      # as in CI

On Windows use ``py`` instead of ``python``. Exit status: 0, or 1 when a case's recall is below
``--min-recall`` or an expected citation does not resolve; 2 for invalid case files. Cases whose
files are missing are skipped (reported), unless ``--strict``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from automotive_log_mcp.app import App
from automotive_log_mcp.config import CASE_NAME_RE, load_config
from automotive_log_mcp.policy import Policy, load_policy
from automotive_log_mcp.tools import Filter, Output, ToolInputError, Tools

CITE = re.compile(r"\b(?:log:\d+#\d+|art:\d+(?:/[a-z0-9_.-]+)?(?::L\d+(?:-L?\d+)?)?)")
TOOLS = ("search", "count", "context", "list_artifacts", "read_artifact", "get_records", "detect", "overview")


class CaseError(ValueError):
    pass


@dataclass
class Case:
    id: str
    title: str
    files: list[Path]
    component: str
    citations: list[str]
    texts: dict[str, str] = field(default_factory=dict)  # citation -> text its line must contain
    synthetic: bool = False
    symptom: str = ""
    queries: list[dict[str, Any]] = field(default_factory=list)
    origin: str = ""


@dataclass
class CaseResult:
    case: Case
    status: str  # ok | skipped | error
    exact: list[str] = field(default_factory=list)
    artifact: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    found_by: dict[str, list[str]] = field(default_factory=dict)  # citation -> tools
    calls: list[dict[str, Any]] = field(default_factory=list)  # tool, args, bytes, ms
    note: str = ""

    @property
    def recall(self) -> float:
        n = len(self.case.citations)
        return (len(self.exact) + len(self.artifact)) / n if n else 1.0


def load_cases(path: Path) -> list[Case]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise CaseError(f"{path}: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise CaseError(f"{path}: expected a mapping with a 'cases' list")
    out: list[Case] = []
    for i, c in enumerate(data["cases"]):
        where = f"{path}: cases[{i}]"
        if not isinstance(c, dict):
            raise CaseError(f"{where} must be a mapping")
        cid = c.get("id")
        if not isinstance(cid, str) or not re.fullmatch(CASE_NAME_RE, cid):
            raise CaseError(f"{where}: 'id' must be a case name (letters, digits, '.', '_', '-')")
        exp = c.get("expected") or {}
        raw = exp.get("citations")
        if not isinstance(raw, list) or not raw:
            raise CaseError(f"{where} ({cid}): expected.citations must be a non-empty list")
        cites: list[str] = []
        texts: dict[str, str] = {}
        for x in raw:
            if isinstance(x, dict) and isinstance(x.get("cite"), str):
                cites.append(x["cite"])
                if isinstance(x.get("text"), str) and x["text"]:
                    texts[x["cite"]] = x["text"]
            elif isinstance(x, str):
                cites.append(x)
            else:
                raise CaseError(f"{where} ({cid}): a citation is a string or {{cite: …, text: …}}")
        bad = [x for x in cites if not CITE.fullmatch(x)]
        if bad:
            raise CaseError(f"{where} ({cid}): malformed citations {bad}")
        files = c.get("files")
        if not isinstance(files, list) or not files:
            raise CaseError(f"{where} ({cid}): 'files' must be a non-empty list")
        queries = c.get("queries") or []
        for q in queries:
            if (
                not isinstance(q, dict)
                or q.get("tool") not in TOOLS
                or not isinstance(q.get("args", {}), dict)
            ):
                raise CaseError(f"{where} ({cid}): each query needs 'tool' ({', '.join(TOOLS)}) and 'args'")
        out.append(
            Case(
                id=cid,
                title=str(c.get("title", "")),
                files=[(path.parent / Path(os.path.expanduser(str(f)))).resolve() for f in files],
                component=str(exp.get("component", "")),
                citations=cites,
                texts=texts,
                synthetic=bool(c.get("synthetic", False)),
                symptom=str(c.get("symptom", "")),
                queries=queries,
                origin=str(path),
            )
        )
    return out


def _call(tools: Tools, case: str, tool: str, args: dict[str, Any]) -> Output:
    args = dict(args)
    if "filter" in args and args["filter"] is not None:
        args["filter"] = Filter.model_validate(args["filter"])
    if tool == "search":
        return tools.search(case, args.pop("query"), **args)
    if tool == "count":
        if "query" in args:
            args["text"] = args.pop("query")
        return tools.count(case, **args)
    return getattr(tools, tool)(case, **args)  # type: ignore[no-any-return]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _cited_text(tools: Tools, case: str, cite: str) -> str:
    """The text a citation points at: a record's tag and message, an artifact's lines, or (for a
    whole artifact or section without lines) its summary and lines."""
    if cite.startswith("log:"):
        recs = tools.get_records(case, [cite]).data.records  # type: ignore[attr-defined]
        return f"{recs[0].tag or ''} {recs[0].msg}" if recs else ""
    data = tools.read_artifact(case, cite).data
    lines = "\n".join(x.text for x in (data.lines or []))  # type: ignore[attr-defined]
    return f"{data.summary or ''}\n{lines}"  # type: ignore[attr-defined]


def run_case(app: App, tools: Tools, case: Case) -> CaseResult:
    missing = [str(f) for f in case.files if not f.exists()]
    if missing:
        return CaseResult(case, "skipped", note="files missing: " + ", ".join(missing))
    db = app.config.case_db(case.id)
    for f in case.files:
        app.ingestor.add(db, str(f), other_cases=False)  # one bugreport can serve several cases
    res = CaseResult(case, "ok")
    plan: list[tuple[str, dict[str, Any]]] = [("detect", {}), ("list_artifacts", {})]
    plan += [(q["tool"], q.get("args", {})) for q in case.queries]
    seen: dict[str, list[str]] = {}
    for tool, args in plan:
        start = time.perf_counter()
        try:
            out = _call(tools, case.id, tool, args)
        except (ToolInputError, TypeError) as e:
            res.status = "error"
            res.note = f"{tool}({args}): {e}"
            return res
        ms = (time.perf_counter() - start) * 1000
        res.calls.append(
            {"tool": tool, "args": args, "bytes": len(out.text.encode("utf-8")), "ms": round(ms, 1)}
        )
        for c in set(CITE.findall(out.text)):
            seen.setdefault(c, []).append(tool)
    check = tools.check_citations(case.id, "\n\n".join(f"Expected: {c}." for c in case.citations))
    errors = check.data.errors  # type: ignore[attr-defined]
    res.unresolved = [f"{e.cite}: {e.message}" for e in errors]
    bad = {e.cite for e in errors}
    for c, want in case.texts.items():
        if c in bad:
            continue
        got = _cited_text(tools, case.id, c)
        if _norm(want) not in _norm(got):
            res.unresolved.append(f"{c}: the line does not contain {want!r} (it shows {got[:120]!r})")
    for c in case.citations:
        art = re.match(r"art:\d+", c)
        if c in seen:
            res.exact.append(c)
            res.found_by[c] = sorted(set(seen[c]))
        elif art and c != art[0] and art[0] in seen:
            res.artifact.append(c)
            res.found_by[c] = sorted(set(seen[art[0]]))
        else:
            res.missed.append(c)
    return res


def report(results: list[CaseResult], min_recall: float) -> str:
    ran = [r for r in results if r.status == "ok"]
    lines = ["# Evidence recall", ""]
    if ran:
        total = sum(len(r.case.citations) for r in ran)
        hit = sum(len(r.exact) + len(r.artifact) for r in ran)
        exact = sum(len(r.exact) for r in ran)
        lines.append(
            f"{len(ran)} cases run: {hit}/{total} root-cause citations surfaced "
            f"({exact} exact, {hit - exact} by their artifact); recall {hit / total:.0%}."
        )
    else:
        lines.append("No case was run.")
    lines += [
        "",
        "| case | synthetic | recall | exact | artifact | missed | found by | bytes |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        if r.status != "ok":
            lines.append(
                f"| {r.case.id} | {'yes' if r.case.synthetic else 'no'} | {r.status} | | | | {r.note} | |"
            )
            continue
        found = "; ".join(f"{c}: {', '.join(t)}" for c, t in r.found_by.items())
        size = sum(c["bytes"] for c in r.calls)
        flag = "" if r.recall >= min_recall else " ⚠"
        lines.append(
            f"| {r.case.id} | {'yes' if r.case.synthetic else 'no'} | {r.recall:.0%}{flag} | "
            f"{', '.join(r.exact)} | {', '.join(r.artifact)} | {', '.join(r.missed)} | {found} | {size:,} |"
        )
    bad = [r for r in results if r.unresolved]
    if bad:
        lines += ["", "Expected citations that do not resolve (fix the case definition):"]
        lines += [f"- {r.case.id}: {u}" for r in bad for u in r.unresolved]
    lines += ["", "Calls per case (first page, default arguments):"]
    for r in ran:
        calls = ", ".join(f"{c['tool']} {c['bytes']:,} B {c['ms']:.0f} ms" for c in r.calls)
        lines.append(f"- {r.case.id}: {calls}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument("case_files", nargs="+", type=Path)
    ap.add_argument("--case", action="append", help="run only this case id (repeatable)")
    ap.add_argument("--synthetic-only", action="store_true", help="run only cases marked synthetic")
    ap.add_argument("--home", type=Path, help="server home to index into and keep (default: temporary)")
    ap.add_argument("--report", type=Path, help="write the Markdown report here (default: print it)")
    ap.add_argument("--json", type=Path, help="write the results as JSON")
    ap.add_argument("--min-recall", type=float, default=0.0, help="fail when a case's recall is lower")
    ap.add_argument("--strict", action="store_true", help="fail when a case's files are missing")
    args = ap.parse_args(argv)
    try:
        cases = [c for f in args.case_files for c in load_cases(f)]
    except CaseError as e:
        print(f"evidence_recall: {e}", file=sys.stderr)
        return 2
    ids = [c.id for c in cases]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        print(f"evidence_recall: duplicate case ids: {', '.join(dup)}", file=sys.stderr)
        return 2
    if args.case:
        unknown = set(args.case) - set(ids)
        if unknown:
            print(f"evidence_recall: unknown case ids: {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        cases = [c for c in cases if c.id in args.case]
    if args.synthetic_only:
        cases = [c for c in cases if c.synthetic]

    temp = None if args.home else Path(tempfile.mkdtemp(prefix="alm-eval-"))
    home = (args.home.expanduser() if args.home else temp) or Path()
    home.mkdir(parents=True, exist_ok=True)
    try:
        config = load_config(home=home)
        app = App.create(config, load_policy(config.policy_file) if args.home else Policy())
        tools = Tools(app)
        results = []
        for c in cases:
            if temp is None and config.case_db(c.id).exists():
                print(f"evidence_recall: reusing case '{c.id}' in {home}", file=sys.stderr)
            results.append(run_case(app, tools, c))
    finally:
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)
    text = report(results, args.min_recall)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
        print(f"report: {args.report}")
    else:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        print(text)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                [
                    {
                        "id": r.case.id,
                        "status": r.status,
                        "recall": r.recall if r.status == "ok" else None,
                        "exact": r.exact,
                        "artifact": r.artifact,
                        "missed": r.missed,
                        "unresolved": r.unresolved,
                        "found_by": r.found_by,
                        "calls": r.calls,
                        "note": r.note,
                    }
                    for r in results
                ],
                indent=1,
            ),
            encoding="utf-8",
        )
    failed = [r for r in results if r.status == "ok" and (r.recall < args.min_recall or r.unresolved)]
    failed += [r for r in results if r.status == "error" or (args.strict and r.status == "skipped")]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
