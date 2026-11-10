"""Detector rules: YAML definitions of known failure patterns, run over a case's records.

Default rules ship in the package (``rules/detect/*.yaml``); YAML files in the config's
``rule_dirs`` add rules, and a rule with the name of an earlier one replaces it (D8: later
directories win). A file holds one rule, or several under ``rules:``. Files with a
``connector:`` key are connector definitions and are skipped here.

One rule::

    rule: anr                         # name: [a-z][a-z0-9_]*, the key for overriding
    version: "1"
    severity: high                    # high | medium | low
    description: Application not responding
    match: '^ANR in '                 # Python regex over message and tag (case-sensitive)
    level: E                          # optional minimum level
    tags: [ActivityManager]           # optional tag globs (case-sensitive)
    any:                              # or: alternatives, each with match / level / tags
      - {match: '^ANR in ', tags: [ActivityManager]}
      - {match: '^\\[art:\\d+\\] ANR in', tags: [anr]}
    followed_by:                      # optional: a second event within a time window
      match: 'Start proc'             # (also any / level / tags)
      within: 30s                     # ms | s | m | h
      absent: false                   # true: a hit is a first event NOT followed in time
    group_by: pid                     # optional: one line per pid; with followed_by, the second
                                      # event must have the same pid
    note: what to look at next        # shown with the hits

A condition needs at least one of ``match``, ``level``, ``tags``. Patterns are redacted like
search queries (D5), so a raw value in a pattern matches its token. Unknown keys, invalid
regexes and duplicate names within one directory are errors.
"""

from __future__ import annotations

import bisect
import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import ConfigError, read_yaml_mapping
from ..core.levels import LEVEL_RANK
from ..core.timeparse import NS
from ..store import query
from ..store.query import Rec, RecordFilter

BUILTIN_DIR = Path(__file__).resolve().parent.parent / "rules"
SEVERITIES = ("high", "medium", "low")
FOLLOW_MAX = 20_000  # first events evaluated for followed_by / group_by (more: result inexact)

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_RULE_KEYS = {"rule", "version", "severity", "description", "note", "match", "level", "tags", "any"}
_RULE_KEYS |= {"followed_by", "group_by"}
_COND_KEYS = {"match", "level", "tags"}
_FOLLOW_KEYS = _COND_KEYS | {"any", "within", "absent"}
_DUR = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)\s*$")
_UNIT = {"ms": NS // 1000, "s": NS, "m": 60 * NS, "h": 3600 * NS}


@dataclass(frozen=True)
class Cond:
    match: str | None = None
    level: str | None = None
    tags: tuple[str, ...] | None = None

    def describe(self) -> str:
        parts = []
        if self.match:
            parts.append(f"/{self.match}/")
        if self.tags:
            parts.append("tags " + ",".join(self.tags))
        if self.level:
            parts.append(f"level>={self.level}")
        return " ".join(parts)


@dataclass(frozen=True)
class Follow:
    conds: tuple[Cond, ...]
    within: str
    within_ns: int
    absent: bool = False


@dataclass(frozen=True)
class Rule:
    name: str
    severity: str
    description: str
    conds: tuple[Cond, ...]
    version: str = "1"
    note: str | None = None
    follow: Follow | None = None
    group_by: str | None = None
    origin: str = ""

    @property
    def needs_records(self) -> bool:
        return self.follow is not None or self.group_by is not None


@dataclass
class RuleSet:
    rules: dict[str, Rule]
    overridden: dict[str, str] = field(default_factory=dict)  # name -> origin of the replaced rule

    def ordered(self) -> list[Rule]:
        return sorted(self.rules.values(), key=lambda r: (SEVERITIES.index(r.severity), r.name))


# -- loading ----------------------------------------------------------------------------------


def _fail(path: Path, msg: str) -> ConfigError:
    return ConfigError(f"detector rule file {path}: {msg}")


def _cond(spec: Any, path: Path, where: str, keys: set[str] = _COND_KEYS) -> Cond:
    if not isinstance(spec, dict):
        raise _fail(path, f"{where} must be a mapping")
    unknown = set(spec) - keys
    if unknown:
        raise _fail(path, f"{where}: unknown keys: {', '.join(sorted(unknown))}")
    match = spec.get("match")
    if match is not None:
        if not isinstance(match, str) or not match:
            raise _fail(path, f"{where}: 'match' must be a non-empty regex string")
        try:
            re.compile(match)
        except re.error as e:
            raise _fail(path, f"{where}: 'match' is not a valid regex: {e}") from e
    level = spec.get("level")
    if level is not None and level not in LEVEL_RANK:
        raise _fail(path, f"{where}: 'level' must be one of V D I W E F")
    tags = spec.get("tags")
    if tags is not None:
        if isinstance(tags, str):
            tags = [tags]
        if not isinstance(tags, list) or not tags or not all(isinstance(t, str) and t for t in tags):
            raise _fail(path, f"{where}: 'tags' must be a list of tag globs")
        tags = tuple(tags)
    if match is None and level is None and tags is None:
        raise _fail(path, f"{where}: give at least one of 'match', 'level', 'tags'")
    return Cond(match, level, tags)


def _conds(spec: dict[str, Any], path: Path, where: str) -> tuple[Cond, ...]:
    alts = spec.get("any")
    own = {k: spec[k] for k in _COND_KEYS if k in spec}
    if alts is not None:
        if own:
            raise _fail(path, f"{where}: give either 'any' or match/level/tags, not both")
        if not isinstance(alts, list) or not alts:
            raise _fail(path, f"{where}: 'any' must be a non-empty list of conditions")
        return tuple(_cond(a, path, f"{where}.any[{i}]") for i, a in enumerate(alts))
    return (_cond(own, path, where),)


def duration_ns(text: Any) -> int:
    m = _DUR.match(str(text))
    if m is None:
        raise ValueError(f"invalid duration {text!r}; use e.g. 500ms, 10s, 2m, 1h")
    return int(float(m[1]) * _UNIT[m[2]])


def parse_rule(spec: Any, path: Path) -> Rule:
    if not isinstance(spec, dict):
        raise _fail(path, "a rule must be a mapping")
    name = spec.get("rule")
    where = f"rule '{name}'" if isinstance(name, str) else "rule"
    unknown = set(spec) - _RULE_KEYS
    if unknown:
        raise _fail(path, f"{where}: unknown keys: {', '.join(sorted(unknown))}")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise _fail(path, "'rule' must be a lowercase name ([a-z][a-z0-9_]*, at most 48 characters)")
    severity = spec.get("severity")
    if severity not in SEVERITIES:
        raise _fail(path, f"{where}: 'severity' must be one of {', '.join(SEVERITIES)}")
    description = spec.get("description")
    if not isinstance(description, str) or not description.strip():
        raise _fail(path, f"{where}: 'description' is required")
    note = spec.get("note")
    if note is not None and not isinstance(note, str):
        raise _fail(path, f"{where}: 'note' must be text")
    follow = None
    fb = spec.get("followed_by")
    if fb is not None:
        if not isinstance(fb, dict):
            raise _fail(path, f"{where}: 'followed_by' must be a mapping")
        unknown = set(fb) - _FOLLOW_KEYS
        if unknown:
            raise _fail(path, f"{where}.followed_by: unknown keys: {', '.join(sorted(unknown))}")
        if "within" not in fb:
            raise _fail(path, f"{where}.followed_by: 'within' is required (e.g. 30s)")
        try:
            within_ns = duration_ns(fb["within"])
        except ValueError as e:
            raise _fail(path, f"{where}.followed_by: {e}") from e
        absent = fb.get("absent", False)
        if not isinstance(absent, bool):
            raise _fail(path, f"{where}.followed_by: 'absent' must be true or false")
        cond_spec = {k: v for k, v in fb.items() if k in _COND_KEYS | {"any"}}
        follow = Follow(_conds(cond_spec, path, f"{where}.followed_by"), str(fb["within"]), within_ns, absent)
    group_by = spec.get("group_by")
    if group_by not in (None, "pid"):
        raise _fail(path, f"{where}: 'group_by' must be 'pid'")
    return Rule(
        name=name,
        severity=severity,
        description=description.strip(),
        conds=_conds(spec, path, where),
        version=str(spec.get("version", "1")),
        note=note.strip() if note else None,
        follow=follow,
        group_by=group_by,
        origin=str(path),
    )


def load_file(path: Path) -> list[Rule]:
    data = read_yaml_mapping(path, "detector rule file")
    if "connector" in data:
        return []  # a regex connector definition (connectors/regex_yaml.py)
    if "rules" in data:
        if set(data) != {"rules"} or not isinstance(data["rules"], list):
            raise _fail(path, "'rules' must be the only key, with a list of rules")
        return [parse_rule(r, path) for r in data["rules"]]
    if "rule" not in data:
        raise _fail(path, "expected 'rule: <name>' (one rule) or 'rules: [...]'")
    return [parse_rule(data, path)]


def _yaml_files(d: Path) -> list[Path]:
    return sorted([*d.rglob("*.yaml"), *d.rglob("*.yml")])


def load_rules(rule_dirs: Iterable[Path] = ()) -> RuleSet:
    """Package rules, then each configured directory in order; later names replace earlier ones."""
    out = RuleSet({})
    for i, d in enumerate([BUILTIN_DIR / "detect", *rule_dirs]):
        if not d.is_dir():
            if i == 0:
                continue
            raise ConfigError(f"rule directory {d} does not exist")
        seen: dict[str, str] = {}
        for path in _yaml_files(d):
            for r in load_file(path):
                if r.name in seen:
                    raise ConfigError(
                        f"detector rule '{r.name}' is defined twice in {d}: {seen[r.name]}, {path}"
                    )
                seen[r.name] = str(path)
                if r.name in out.rules:
                    out.overridden[r.name] = out.rules[r.name].origin
                out.rules[r.name] = r
    return out


# -- running ----------------------------------------------------------------------------------


@dataclass
class HitGroup:
    """Hits with the same message template (or the same pid with ``group_by: pid``)."""

    first: Rec
    count: int
    last: Rec | None = None
    follow: Rec | None = None  # the second event of the first hit (followed_by)
    open: bool = False  # absent rule: the case ends before the window closes


@dataclass
class RuleResult:
    rule: Rule
    count: int
    exact: bool
    groups: list[HitGroup]
    first_ts: int | None = None
    last_ts: int | None = None
    note: str | None = None  # why the result is partial


@dataclass
class _Compiled:
    where: list[str]
    params: list[Any]
    test: query.RecordTest | None  # None: the SQL conditions alone decide (no regex)
    empty: bool  # a tag list matched no tag in the case


def compile_conds(
    conn: sqlite3.Connection, conds: tuple[Cond, ...], redact: Callable[[str], str]
) -> _Compiled:
    """SQL (an OR over the conditions) and a record test that decides each candidate exactly."""
    parts: list[tuple[list[str], list[Any]]] = []
    tests: list[tuple[int, set[int] | None, re.Pattern[str] | None]] = []
    for c in conds:
        rf = RecordFilter(min_level=c.level)
        tag_set: set[int] | None = None
        if c.tags is not None:
            ids, _ = query.tag_ids(conn, [redact(t) for t in c.tags])
            if not ids:
                continue  # this alternative cannot match in this case
            rf.tag_ids = ids
            tag_set = set(ids)
        where, params = rf.where()
        rx = None
        if c.match:
            pattern = redact(c.match)
            rx = re.compile(pattern)
            pw, pp = query.regex_prefilter(pattern)
            where, params = [*pw, *where], [*pp, *params]
        parts.append((where, params))
        tests.append((LEVEL_RANK[c.level] if c.level else -1, tag_set, rx))
    if not parts:
        return _Compiled(["0"], [], None, True)
    if any(not w for w, _ in parts):
        sql: list[str] = []
        params_all: list[Any] = []
    else:
        sql = ["(" + " OR ".join("(" + " AND ".join(w) + ")" for w, _ in parts) + ")"]
        params_all = [p for _, ps in parts for p in ps]
    if all(rx is None for _, _, rx in tests) and len(tests) == 1:
        return _Compiled(sql, params_all, None, False)

    def test(msg: str, tag: str | None, level: str | None, tag_id: int | None) -> bool:
        for min_rank, tag_set, rx in tests:
            if min_rank >= 0 and (level is None or LEVEL_RANK.get(level, -1) < min_rank):
                continue
            if tag_set is not None and tag_id not in tag_set:
                continue
            if rx is not None and not (rx.search(msg) or (tag and rx.search(tag))):
                continue
            return True
        return False

    return _Compiled(sql, params_all, test, False)


def _clock_key(r: Rec) -> tuple[int, int] | None:
    """(clock, time): wall-clock time, or the monotonic time within its source."""
    if r.ts is not None:
        return (-1, r.ts)
    if r.ts_src is not None:
        return (r.source_id, r.ts_src)
    return None


def _group_key(r: Rec, rule: Rule) -> tuple[Any, ...]:
    if rule.group_by == "pid":
        return ("pid", r.pid)
    return (r.source_id, r.template_id, r.tag, r.level)


def run_rule(
    conn: sqlite3.Connection,
    rule: Rule,
    where: list[str],
    params: list[Any],
    redact: Callable[[str], str],
    max_rows: int,
    follow_where: tuple[list[str], list[Any]] = ([], []),
    case_end: int | None = None,
) -> RuleResult:
    """Run one rule over the records passing ``where`` (the caller's filter). ``follow_where``
    restricts second events (followed_by) without the time part of the filter."""
    first = compile_conds(conn, rule.conds, redact)
    if first.empty:
        return RuleResult(rule, 0, True, [])
    w, p = [*where, *first.where], [*params, *first.params]
    if not rule.needs_records:
        if first.test is None:
            groups, exact = query.aggregate(conn, w, p), True
        else:
            scan = query.regex_scan(conn, w, p, first.test, max_rows, 0)
            groups, exact = scan.groups, scan.complete
        return _from_groups(conn, rule, groups, exact)
    test = first.test or (lambda *_: True)
    scan = query.regex_scan(conn, w, p, test, max_rows, FOLLOW_MAX)
    recs = scan.first
    total = sum(g.count for g in scan.groups)
    exact = scan.complete and total <= FOLLOW_MAX
    note = None
    if total > FOLLOW_MAX:
        note = f"only the first {FOLLOW_MAX:,} of {total:,} first events were evaluated"
    if rule.follow is None:
        hits: list[tuple[Rec, Rec | None, bool]] = [(r, None, False) for r in recs]
    else:
        hits = _follow(conn, rule, recs, redact, max_rows, follow_where, case_end)
    groups_out: dict[tuple[Any, ...], HitGroup] = {}
    for a, b, opn in hits:
        key = _group_key(a, rule)
        g = groups_out.get(key)
        if g is None:
            groups_out[key] = HitGroup(a, 1, None, b, opn)
        else:
            g.count += 1
            g.last = a
            g.open = g.open and opn
    count = len(hits)
    walls = [a.ts for a, _, _ in hits if a.ts is not None]
    return RuleResult(
        rule,
        count,
        exact,
        list(groups_out.values()),
        min(walls) if walls else None,
        max(walls) if walls else None,
        note,
    )


def _from_groups(conn: sqlite3.Connection, rule: Rule, groups: list[query.Group], exact: bool) -> RuleResult:
    count = sum(g.count for g in groups)
    firsts = query.records_by_id(conn, [g.first_id for g in groups])
    lasts = query.records_by_id(conn, [g.last_id for g in groups if g.count > 1])
    out = [
        HitGroup(firsts[g.first_id], g.count, lasts.get(g.last_id) if g.count > 1 else None) for g in groups
    ]
    walls = [g.first_ts for g in groups if g.first_ts is not None]
    last_walls = [r.ts for r in [*lasts.values(), *firsts.values()] if r.ts is not None]
    return RuleResult(
        rule, count, exact, out, min(walls) if walls else None, max(last_walls) if last_walls else None
    )


def _follow(
    conn: sqlite3.Connection,
    rule: Rule,
    firsts: list[Rec],
    redact: Callable[[str], str],
    max_rows: int,
    follow_where: tuple[list[str], list[Any]],
    case_end: int | None,
) -> list[tuple[Rec, Rec | None, bool]]:
    """Pair each first event with the earliest second event within the window (same pid with
    ``group_by: pid``). Returns (first, second or None, window still open) for each hit."""
    assert rule.follow is not None
    fol = rule.follow
    keyed = [(k, a) for a in firsts if (k := _clock_key(a)) is not None]
    if not keyed:
        return []
    second = compile_conds(conn, fol.conds, redact)
    by_clock: dict[tuple[int, int | None], tuple[list[tuple[int, int]], list[Rec]]] = {}
    if not second.empty:
        w, p = [*follow_where[0], *second.where], [*follow_where[1], *second.params]
        walls = [k[1] for k, _ in keyed if k[0] == -1]
        if walls and len(walls) == len(keyed):
            w = [*w, "r.ts BETWEEN ? AND ?"]
            p = [*p, min(walls), max(walls) + fol.within_ns]
        scan = query.regex_scan(conn, w, p, second.test or (lambda *_: True), max_rows, FOLLOW_MAX * 5)
        bs = sorted(
            ((k, b) for b in scan.first if (k := _clock_key(b)) is not None), key=lambda x: (x[0], x[1].id)
        )
        for k, b in bs:
            ck = (k[0], b.pid if rule.group_by == "pid" else None)
            keys, recs = by_clock.setdefault(ck, ([], []))
            keys.append((k[1], b.id))
            recs.append(b)
    out: list[tuple[Rec, Rec | None, bool]] = []
    for k, a in keyed:
        ck = (k[0], a.pid if rule.group_by == "pid" else None)
        found: Rec | None = None
        if ck in by_clock:
            keys, recs = by_clock[ck]
            i = bisect.bisect_right(keys, (k[1], a.id))
            if i < len(keys) and keys[i][0] <= k[1] + fol.within_ns:
                found = recs[i]
        if fol.absent:
            if found is None:
                opn = k[0] == -1 and case_end is not None and k[1] + fol.within_ns > case_end
                out.append((a, None, opn))
        elif found is not None:
            out.append((a, found, False))
    return out
