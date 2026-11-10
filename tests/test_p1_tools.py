"""M4 P1 tools: overview, count, detect (rules, loading, overrides) and check_citations.

Golden outputs (text and structured content) on the bugreport zip are in
``tests/golden/tools__{overview,count,detect,check_citations}.json``; regenerate with
``UPDATE_GOLDEN=1`` and review the diff.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from automotive_log_mcp import tools as tools_mod
from automotive_log_mcp.app import App
from automotive_log_mcp.config import Caps, ConfigError, load_config
from automotive_log_mcp.core import citations as cit
from automotive_log_mcp.core import format as fmt
from automotive_log_mcp.detect.engine import load_rules
from automotive_log_mcp.policy import Policy
from automotive_log_mcp.tools import Output, ToolInputError, Tools

from .conftest import FIXED_SECRET, REF_TIME, check_golden
from .test_golden import make_bugreport_zip
from .test_tools import F, br  # noqa: F401  (fixture)

# -- golden outputs ------------------------------------------------------------------------------

ANSWER = """\
# HVAC app crash

The HVAC app crashed at 01-01 00:00:01.000 with a NullPointerException in Controller.update
(log:3#1, art:1/exception:L3). Just before, `setProperty failed, status=NOT_AVAILABLE` (log:1#2).

- The process died: log:2#1.
- A range log:3#1-3 and a missing line log:1#99 and art:9 and art:1/nosuch and log:abc#1.
- The quote "Controller crashed on purpose" log:1#2 is not in the line.

This paragraph states a finding about the vehicle HAL without any citation at all.

It is probably a race in the HAL start-up; the logs do not show the HAL side.

```
log:42#42 inside a code block is not checked
```
"""

CALLS: dict[str, list[tuple[str, dict[str, Any]]]] = {
    "overview": [("all", {}), ("filtered: level E", {"filter": {"level": "E"}})],
    "count": [
        ("by tag", {}),
        ("by level per minute", {"by": "level", "bucket": "1m"}),
        ("text query by pid", {"by": "pid", "text": "hvac"}),
        ("regex by source", {"by": "source", "text": r"pid \d+", "mode": "regex"}),
        ("totals only", {"by": None, "filter": {"level": "W"}}),
        ("template", {"by": "template", "limit": 3}),
        ("kernel buckets (monotonic)", {"by": "level", "bucket": "1m", "filter": {"sources": ["5"]}}),
    ],
    "detect": [
        ("all rules", {}),
        ("one rule, filtered", {"rules": ["java_crash"], "filter": {"to": "00:00:02"}}),
    ],
    "check_citations": [
        ("answer", {"text": ANSWER}),
        ("pass", {"text": "Crash: log:3#1 (art:1/exception:L3).\n"}),
    ],
}


@pytest.mark.parametrize("tool", list(CALLS))
def test_p1_tool_golden(br: Tools, tool: str) -> None:  # noqa: F811
    out: dict[str, Any] = {}
    for label, args in CALLS[tool]:
        a = dict(args)
        if "filter" in a:
            a["filter"] = F(**{("from_" if k == "from" else k): v for k, v in a["filter"].items()})
        o = getattr(br, tool)("br", **a)
        assert fmt.utf8_len(o.text) <= br.caps.max_bytes
        dumped = o.data.model_dump(mode="json", by_alias=True, exclude_none=True)
        type(o.data).model_validate(dumped)  # what the MCP output schema check does
        out[label] = {
            "args": args,
            "text": o.text.split("\n"),
            "structured": o.data.model_dump(mode="json", by_alias=True, exclude_none=True),
        }
    check_golden(f"tools__{tool}", out)


def test_tiny_caps_hold_for_p1_tools(app: App, tmp_path: Path) -> None:
    app.ingestor.add(app.config.case_db("br"), str(make_bugreport_zip(tmp_path)))
    caps = Caps(max_lines=4, max_bytes=1500, max_msg_chars=60)
    t = Tools(App.create(replace(app.config, caps=caps), Policy()))
    outs = [
        t.overview("br"),
        t.count("br"),
        t.count("br", by="template", bucket="10m"),
        t.detect("br", examples=10),
        t.check_citations("br", ANSWER * 20),
    ]
    for o in outs:
        assert fmt.utf8_len(o.text) <= caps.max_bytes, o.text
    assert "more values" in outs[1].text
    assert "not shown" in outs[3].text or "output cap" in outs[3].text
    assert "more issues not shown" in outs[4].text
    with pytest.raises(ToolInputError, match="at most 4"):
        t.count("br", bucket="10s")  # 4 buckets at most with max_lines=4


# -- detect ------------------------------------------------------------------------------------

# One line per default rule, plus near misses that must not hit.
RULES_LOG = """\
--------- beginning of main
01-05 10:00:00.000   500   500 I boot_progress_start: 1000
01-05 10:01:00.000  2000  2000 E AndroidRuntime: FATAL EXCEPTION: main
01-05 10:01:00.000  2000  2000 E AndroidRuntime: Process: com.example.foo, PID: 2000
01-05 10:01:01.000  2100  2101 F libc    : Fatal signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault addr 0x0 in tid 2101 (worker), pid 2100 (vendor.foo)
01-05 10:01:02.000  1000  1100 E ActivityManager: ANR in com.example.foo (com.example.foo/.Main)
01-05 10:01:03.000  1000  1012 W Watchdog: *** WATCHDOG KILLING SYSTEM PROCESS: Blocked in handler on foreground thread
01-05 10:01:04.000   400   400 I lowmemorykiller: Kill 'com.example.bar' (2300), uid 10060, oom_score_adj 900 to free 50000kB rss
01-05 10:01:05.000  2400  2400 W foo     : type=1400 audit(0.0:12): avc: denied { read } for name="x" scontext=u:r:foo:s0 tcontext=u:object_r:bar:s0 tclass=file permissive=0
01-05 10:01:06.000  1000  1188 E JavaBinder: !!! FAILED BINDER TRANSACTION !!!  (parcel size = 1048576)
01-05 10:01:07.000   610   640 E DefaultVehicleHal: getValue failed for prop 0x11400400
01-05 10:01:08.000     1     1 I init    : Service 'vendor.foo' (pid 2100) received signal 11
01-05 10:01:09.000  1000  1100 I ActivityManager: ANR rate is low
01-05 10:01:10.000  2400  2400 I foo     : avc: granted { read }
01-05 10:01:11.000   610   640 W DefaultVehicleHal: getValue slow for prop 0x11400400
01-05 10:01:12.000  2000  2000 I AndroidRuntime: no exception here
01-05 10:05:00.000  1000  1100 I ActivityManager: still booting
"""


@pytest.fixture
def rules_case(app: App, tmp_path: Path) -> Tools:
    p = tmp_path / "rules.log"
    p.write_text(RULES_LOG, encoding="utf-8")
    os.utime(p, (REF_TIME, REF_TIME))
    app.ingestor.add(app.config.case_db("r"), str(p))
    return Tools(app)


def _hits(out: Output) -> dict[str, list[str]]:
    return {r.rule: [h.record.cite for h in r.hits] for r in out.data.results}  # type: ignore[attr-defined]


def test_every_default_rule_hits_once(rules_case: Tools) -> None:
    out = rules_case.detect("r", examples=10)
    assert _hits(out) == {
        "boot_not_completed": ["log:1#1"],
        "java_crash": ["log:1#2"],
        "native_crash": ["log:1#3"],
        "anr": ["log:1#4"],
        "watchdog": ["log:1#5"],
        "lmk": ["log:1#6"],
        "selinux_denial": ["log:1#7"],
        "binder_failure": ["log:1#8"],
        "vhal_error": ["log:1#9"],
        "service_restart": ["log:1#10"],
    }
    assert out.data.no_hits == []  # type: ignore[attr-defined]
    assert len(load_rules().rules) == 10
    # severity order, then time
    sev = [r.severity for r in out.data.results]  # type: ignore[attr-defined]
    assert sev == sorted(sev, key=["high", "medium", "low"].index)


def test_boot_completed_closes_the_window(app: App, tmp_path: Path) -> None:
    log = RULES_LOG.replace(
        "01-05 10:01:12.000  2000  2000 I AndroidRuntime: no exception here",
        "01-05 10:01:12.000   500   500 I boot_progress_enable_screen: 72000",
    )
    p = tmp_path / "b.log"
    p.write_text(log, encoding="utf-8")
    app.ingestor.add(app.config.case_db("b"), str(p))
    out = Tools(app).detect("b", rules=["boot_not_completed"])
    assert out.data.results == []  # type: ignore[attr-defined]
    assert "no hits: boot_not_completed" in out.text


def test_rule_dirs_override_and_add(home: Path, tmp_path: Path) -> None:
    rules = tmp_path / "myrules"
    rules.mkdir()
    (rules / "anr.yaml").write_text(
        "rule: anr\nseverity: low\ndescription: my ANR\nmatch: 'ANR rate'\ntags: [ActivityManager]\n",
        encoding="utf-8",
    )
    (rules / "more.yaml").write_text(
        """\
rules:
  - rule: died_then_started
    severity: medium
    description: process death followed by a new process within 5 s
    match: 'FATAL EXCEPTION'
    followed_by: {match: 'Fatal signal', within: 5s}
  - rule: crash_not_followed
    severity: low
    description: FATAL not followed by a watchdog within 1 s
    match: 'FATAL EXCEPTION'
    followed_by: {tags: [Watchdog], within: 1s, absent: true}
  - rule: same_pid_followed
    severity: low
    description: needs the same pid
    match: 'FATAL EXCEPTION'
    group_by: pid
    followed_by: {match: 'Fatal signal', within: 5s}
  - rule: raw_mac
    severity: low
    description: a raw value in a pattern is redacted like a query
    match: '3c:5a:b4:01:02:03'
""",
        encoding="utf-8",
    )
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"rule_dirs: [{rules.as_posix()}]\n", encoding="utf-8")
    home.mkdir(parents=True, exist_ok=True)
    (home / "secret.key").write_bytes(FIXED_SECRET)
    app = App.create(load_config(cfg), Policy())
    assert app.rules.overridden["anr"].endswith("anr.yaml")
    assert app.rules.rules["anr"].origin == str(rules / "anr.yaml")
    log = RULES_LOG + "01-05 10:05:01.000  1000  1100 I wpa_supplicant: associated with 3c:5a:b4:01:02:03\n"
    p = tmp_path / "r.log"
    p.write_text(log, encoding="utf-8")
    app.ingestor.add(app.config.case_db("r"), str(p))
    out = Tools(app).detect("r")
    hits = _hits(out)
    assert hits["anr"] == ["log:1#11"]  # the override, not the package rule
    assert hits["died_then_started"] == ["log:1#2"]
    res = {r.rule: r for r in out.data.results}  # type: ignore[attr-defined]
    assert res["died_then_started"].hits[0].followed_by == "log:1#3"
    assert "followed by log:1#3 (+1 s)" in out.text
    assert hits["crash_not_followed"] == ["log:1#2"]  # the watchdog comes 3 s later
    assert "same_pid_followed" in out.data.no_hits  # type: ignore[attr-defined]  # pid 2000 vs 2100
    assert hits["raw_mac"] == ["log:1#16"]


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("rule: Bad\nseverity: low\ndescription: x\nmatch: x\n", "lowercase name"),
        ("rule: x\nseverity: urgent\ndescription: x\nmatch: x\n", "severity"),
        ("rule: x\nseverity: low\ndescription: x\nmatch: '('\n", "not a valid regex"),
        ("rule: x\nseverity: low\ndescription: x\n", "at least one of"),
        ("rule: x\nseverity: low\ndescription: x\nmatch: x\nwhen: now\n", "unknown keys: when"),
        (
            "rule: x\nseverity: low\ndescription: x\nmatch: x\nfollowed_by: {match: y}\n",
            "'within' is required",
        ),
        (
            "rule: x\nseverity: low\ndescription: x\nmatch: x\nfollowed_by: {match: y, within: soon}\n",
            "duration",
        ),
        ("rule: x\nseverity: low\ndescription: x\nmatch: x\ngroup_by: tid\n", "group_by"),
        ("rule: x\nseverity: low\ndescription: x\nmatch: x\nany: [{match: y}]\n", "either 'any'"),
        (
            "rules: [{rule: x, severity: low, description: x, match: x}, {rule: x, severity: low, description: y, match: y}]\n",
            "defined twice",
        ),
        ("foo: bar\n", "expected 'rule"),
    ],
)
def test_invalid_rule_files_fail_loudly(tmp_path: Path, text: str, match: str) -> None:
    d = tmp_path / "r"
    d.mkdir()
    (d / "x.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=match):
        load_rules([d])


def test_missing_rule_dir(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        load_rules([tmp_path / "nope"])


def test_detect_unknown_rule_and_filter(rules_case: Tools) -> None:
    with pytest.raises(ToolInputError, match="unknown rules: nope"):
        rules_case.detect("r", rules=["nope"])
    out = rules_case.detect("r", filter=F(from_="01-05 10:01:05", to="01-05 10:01:08"))
    assert set(_hits(out)) == {"selinux_denial", "binder_failure", "vhal_error", "service_restart"}


def test_detect_on_kernel_monotonic_records(app: App, tmp_path: Path) -> None:
    p = tmp_path / "dmesg.txt"
    p.write_text(
        "<6>[    0.000000] Booting Linux on physical CPU 0x0\n"
        "<3>[  500.123456] Out of memory: Killed process 1234 (foo) total-vm:1000kB, anon-rss:900kB\n"
        "<5>[  501.000000] audit: type=1400 audit(1700000000.123:7): avc:  denied  { write } for pid=88 "
        'comm="bar" scontext=u:r:bar:s0 tcontext=u:object_r:baz:s0 tclass=file permissive=0\n',
        encoding="utf-8",
    )
    app.ingestor.add(app.config.case_db("k"), str(p))
    out = Tools(app).detect("k")
    assert _hits(out) == {"lmk": ["log:1#2"], "selinux_denial": ["log:1#3"]}
    assert "500.123456" in out.text


# -- count -------------------------------------------------------------------------------------


def test_count_matches_search_totals(br: Tools) -> None:  # noqa: F811
    for q, mode in (("hvac", "text"), (r"pid \d+", "regex")):
        total = br.search("br", q, mode=mode, count_only=True).data.total  # type: ignore[attr-defined]
        for by in ("tag", "level", "pid", "source", "template", None):
            assert br.count("br", by=by, text=q, mode=mode).data.total == total  # type: ignore[attr-defined]
    every = sum(s.records for s in br.app.list_sources("br") or [])
    assert br.count("br", by="source").data.total == every  # type: ignore[attr-defined]


def test_count_buckets_include_empty_and_reject_too_many(br: Tools) -> None:  # noqa: F811
    out = br.count("br", by=None, bucket="1m")
    buckets = out.data.buckets  # type: ignore[attr-defined]
    assert len(buckets) == 17 and sum(b.count for b in buckets) == out.data.total  # type: ignore[attr-defined]
    assert any(b.count == 0 for b in buckets)
    assert buckets[0].ts == "2025-12-31 23:50:00.000"
    # every printed bucket start is accepted back as 'from'
    first = out.text.split("\n")[1].split()[:2]
    assert br.search("br", None, filter=F(from_=" ".join(first), to="+59s"), count_only=True).data.total == 1  # type: ignore[attr-defined]
    with pytest.raises(ToolInputError, match="use bucket='30s'"):
        br.count("br", bucket="1s")
    with pytest.raises(ToolInputError, match="invalid bucket"):
        br.count("br", bucket="soon")


def test_count_broad_query_is_sampled(br: Tools, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    exact = br.count("br", by="level", text="com").data.total  # type: ignore[attr-defined]
    monkeypatch.setattr(tools_mod, "AGG_ROWS", 1)
    out = br.count("br", by="level", text="com")
    assert out.data.exact is False and out.text.startswith("≈")  # type: ignore[attr-defined]
    assert "estimated from every" in out.text
    assert out.data.total > 0 and exact > 0  # type: ignore[attr-defined]


# -- overview ----------------------------------------------------------------------------------


def _write_log(tmp_path: Path, name: str, lines: list[str]) -> Path:
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(p, (REF_TIME, REF_TIME))
    return p


def test_overview_gaps_and_bursts(app: App, tmp_path: Path) -> None:
    lines = [f"02-01 09:00:{s:02d}.000  100  100 I Steady: tick {s}" for s in range(0, 60, 2)]
    lines += [f"02-01 09:01:{s:02d}.000  100  100 I Steady: tick {s}" for s in range(0, 60, 2)]
    lines += [
        f"02-01 09:02:30.{ms:03d}  200  200 E Bursty: failure {ms}" for ms in range(0, 1000, 25)
    ]  # 40 E
    lines += [f"02-01 09:04:{s:02d}.000  100  100 I Steady: tick {s}" for s in range(0, 60, 2)]
    lines += [f"02-01 09:05:{s:02d}.000  100  100 E Steady: occasional {s}" for s in range(0, 60, 20)]
    app.ingestor.add(app.config.case_db("o"), str(_write_log(tmp_path, "o.log", lines)))
    out = Tools(app).overview("o")
    d = out.data
    gaps = [(g.before, g.after, g.seconds) for g in d.gaps]  # type: ignore[attr-defined]
    # 09:01:58 -> 09:02:30.000 (32 s) and 09:02:30.975 -> 09:04:00 (89.025 s); 2-s steps are no gap
    assert gaps == [("log:1#60", "log:1#61", 32.0), ("log:1#100", "log:1#101", 89.025)]
    assert [(b.errors, b.top_tag, b.first) for b in d.bursts] == [(40, "Bursty", "log:1#61")]  # type: ignore[attr-defined]
    assert d.levels == {"E": 43, "I": 90}  # type: ignore[attr-defined]
    assert len(d.histogram) <= 60 and sum(h.count for h in d.histogram) == 133  # type: ignore[attr-defined]
    assert "error bursts" in out.text and "Bursty" in out.text
    assert fmt.utf8_len(out.text) <= 8192


def test_overview_empty_and_filtered(app: App, br: Tools) -> None:  # noqa: F811
    out = br.overview("br", F(sources=["5"]))
    assert out.data.records == 3 and out.data.mono_from == "0.000000"  # type: ignore[attr-defined]
    assert "since boot 0.000000 .. 124.000000" in out.text
    with pytest.raises(ToolInputError, match="does not exist"):
        br.overview("nope")


# -- check_citations ---------------------------------------------------------------------------


def test_citation_scanner() -> None:
    text = (
        "See log:3#1, `log:3#2`, (art:6/backtrace:L17). and **log:4#1**; art:2/p1500.t1.main.\n"
        "State of the art: none. The log: nothing. catalog:7#1 is not one.\n"
        "Bad: log:3#0 log:3 log:x#1 art:1:L5-L2 log:3#1-30s log:3#5..9\n"
    )
    found = [(f.text, f.valid) for f in cit.find(text)]
    assert found == [
        ("log:3#1", True),
        ("log:3#2", True),
        ("art:6/backtrace:L17", True),
        ("log:4#1", True),
        ("art:2/p1500.t1.main", True),
        ("log:3#0", False),
        ("log:3", False),
        ("log:x#1", False),
        ("art:1:L5-L2", False),
        ("log:3#1-30s", False),
        ("log:3#5..9", False),
    ]
    paras = cit.paragraphs(
        "# Title\n\nOne two\nthree.\n\n- item one\n- item two\n\n| a | b |\n|---|---|\n| c | d |\n"
    )
    assert [(p.kind, p.text) for p in paras] == [
        ("text", "One two three."),
        ("item", "item one"),
        ("item", "item two"),
        ("table", "| a | b |"),
        ("table", "| c | d |"),
    ]


def test_check_citations_errors_and_warnings(br: Tools) -> None:  # noqa: F811
    out = br.check_citations("br", ANSWER)
    d = out.data
    assert d.ok is False and out.text.startswith("FAIL")  # type: ignore[attr-defined]
    errs = {e.cite: e.message for e in d.errors}  # type: ignore[attr-defined]
    assert set(errs) == {"log:3#1-3", "log:1#99", "art:9", "art:1/nosuch", "log:abc#1"}
    assert "ranges are not citations" in errs["log:3#1-3"]
    assert "source 1 has 2 records" in errs["log:1#99"]
    assert "no artifact art:9" in errs["art:9"]
    assert "no section 'nosuch'" in errs["art:1/nosuch"]
    warns = [w.message for w in d.warnings]  # type: ignore[attr-defined]
    assert any("vehicle HAL without any citation" in w for w in warns)
    assert any('quote "Controller crashed on purpose"' in w for w in warns)
    assert not any("setProperty failed" in w for w in warns)  # that quote is in log:1#2
    assert not any("probably a race" in w for w in warns)  # a marked guess needs no citation
    assert d.uncited == 1  # type: ignore[attr-defined]
    assert "log:42#42" not in out.text  # code blocks are not checked


def test_check_citations_pass_and_no_citations(br: Tools) -> None:  # noqa: F811
    ok = br.check_citations("br", "The app crashed (log:3#1), stack in art:1/exception:L3-L4.\n")
    assert ok.data.ok and ok.text.startswith("PASS")  # type: ignore[attr-defined]
    none = br.check_citations("br", "The app crashed because the HAL was not ready in time, clearly.\n")
    assert none.data.ok and none.data.citations == 0  # type: ignore[attr-defined]
    assert "cites nothing" in none.text and none.data.uncited == 1  # type: ignore[attr-defined]


def test_check_citations_respects_blocked_sources(app: App, tmp_path: Path) -> None:
    app.ingestor.add(app.config.case_db("br"), str(make_bugreport_zip(tmp_path)))
    blocked = App.create(app.config, Policy(blocked_sources=("*SYSTEM LOG:crash*",)))
    out = Tools(blocked).check_citations("br", "Crash at log:3#1 and art:1.\n")
    assert {e.cite for e in out.data.errors} == {"log:3#1", "art:1"}  # type: ignore[attr-defined]


def test_call_log_keeps_only_size_of_checked_text(app: App, tmp_path: Path) -> None:
    import anyio

    from automotive_log_mcp.server import LogServer

    app.ingestor.add(app.config.case_db("br"), str(make_bugreport_zip(tmp_path)))
    srv = LogServer(app.config, Policy(), session="t")

    async def main() -> None:
        await srv.mcp.call_tool("check_citations", {"case": "br", "text": "secret words log:3#1"})

    anyio.run(main)
    lines = [
        json.loads(x) for x in (app.config.home / "calllog.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    entry = next(x for x in lines if x["tool"] == "check_citations")
    assert entry["args"]["text"].startswith("<20 chars, sha256 ")
    assert "secret" not in json.dumps(lines)
