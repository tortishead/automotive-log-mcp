"""M3 P0 tools: filter resolution, caps, paging, collapse, context, artifacts, empty reasons.

Golden outputs per tool (text and structured content) on the bugreport zip are in
``tests/golden/tools__*.json``; regenerate with ``UPDATE_GOLDEN=1``.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from automotive_log_mcp.app import App
from automotive_log_mcp.config import Caps
from automotive_log_mcp.core import format as fmt
from automotive_log_mcp.policy import Policy
from automotive_log_mcp.store import query
from automotive_log_mcp.tools import Filter, Output, ToolInputError, Tools, looks_like_regex

from .conftest import check_golden, copy_fixture
from .test_golden import make_bugreport_zip

GEN = Path(__file__).resolve().parent.parent / "scripts" / "gen_logcat.py"


@pytest.fixture
def br(app: App, tmp_path: Path) -> Tools:
    app.ingestor.add(app.config.case_db("br"), str(make_bugreport_zip(tmp_path)))
    return Tools(app)


def F(**kw: Any) -> Filter:
    if "from_" in kw:
        kw["from"] = kw.pop("from_")
    return Filter.model_validate(kw)


def cites(out: Output) -> list[str]:
    return [h["cite"] for h in out.data.model_dump()["hits"]]


# -- golden outputs per tool --------------------------------------------------------------------

CALLS: dict[str, list[tuple[str, dict[str, Any]]]] = {
    "search": [
        ("level W, collapsed", {"text": "pid", "filter": {"level": "W"}}),
        ("not collapsed, page 1", {"text": "pid", "collapse": False, "limit": 3}),
        ("regex", {"text": r"status=\w+_AVAIL", "mode": "regex"}),
        ("count only", {"text": "com", "count_only": True}),
        ("time window from a citation", {"text": "com", "filter": {"from": "log:3#1-1s", "to": "+2s"}}),
        ("kernel, monotonic clock", {"text": "init OR wlan0", "filter": {"from": "120.0"}}),
        ("raw value redacted", {"text": "3c:5a:b4:01:02:03"}),
        ("empty: word not in the case", {"text": "nothing_like_this"}),
        ("empty: filtered out", {"text": "setProperty", "filter": {"level": "F", "pid": 1234}}),
        ("regex-looking text query", {"text": r"status=\w+"}),
        ("unknown tag glob", {"text": "setProperty", "filter": {"tags": ["hvac*"]}}),
    ],
    "context": [
        ("source", {"at": "log:1#2", "before": 2, "after": 2}),
        ("artifact record, pid scope", {"at": "art:1", "scope": "pid"}),
        ("kernel", {"at": "log:5#2", "before": 1, "after": 1}),
    ],
    "list_artifacts": [("all", {}), ("type", {"type": "tombstone"}), ("type dropbox", {"type": "dropbox"})],
    "read_artifact": [
        ("summary tombstone", {"id": "art:6"}),
        ("summary crash-buffer crash with dropbox copy", {"id": "art:1"}),
        ("section", {"id": "art:6/backtrace"}),
        ("range", {"id": "art:2:L8-L10"}),
        ("section and lines", {"id": "art:4", "section": "exception", "start_line": 12, "end_line": 14}),
        ("one line", {"id": "art:6/backtrace:L17"}),
    ],
    "get_records": [("crash and one missing", {"ids": ["log:3#1", "log:5#3", "log:99#1"]})],
}


def _call(tools: Tools, tool: str, args: dict[str, Any]) -> Output:
    args = dict(args)
    if tool == "search":
        f = args.pop("filter", None)
        return tools.search("br", args.pop("text"), filter=F(**f) if f else None, **args)
    return getattr(tools, tool)("br", **args)  # type: ignore[no-any-return]


@pytest.mark.parametrize("tool", list(CALLS))
def test_tool_golden(br: Tools, tool: str) -> None:
    out: dict[str, Any] = {}
    for label, args in CALLS[tool]:
        o = _call(br, tool, args)
        assert fmt.utf8_len(o.text) <= br.caps.max_bytes
        out[label] = {
            "args": args,
            "text": o.text.split("\n"),
            "structured": o.data.model_dump(mode="json", by_alias=True, exclude_none=True),
        }
    check_golden(f"tools__{tool}", out)


# -- filter --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expect"),
    [
        ("2026-01-01 00:00:02.000", ["log:2#1", "log:4#2"]),
        ("01-01 00:00:02.000", ["log:2#1", "log:4#2"]),
        ("01-01 00:00:02", ["log:2#1", "log:4#2"]),
        ("00:00:02", ["log:2#1", "log:4#2"]),
        ("2026-01-01T00:00:02", ["log:2#1", "log:4#2"]),
        ("log:2#1", ["log:2#1", "log:4#2"]),
    ],
)
def test_time_forms_round_trip(br: Tools, spec: str, expect: list[str]) -> None:
    out = br.search("br", None, filter=F(from_=spec, to=spec), collapse=False)
    assert cites(out) == expect


def test_printed_times_are_accepted_back(br: Tools) -> None:
    """Every timestamp a hit prints (short in text, full in structured content) selects that hit."""
    out = br.search("br", None, collapse=False)
    hits = out.data.model_dump()["hits"]
    lines = out.text.split("\n")[1:]
    assert len(hits) >= 15
    for h, line in zip(hits, lines, strict=False):
        short = line.split("  ")[1]
        for spec in (short, h["ts"]):
            got = cites(br.search("br", None, filter=F(from_=spec, to=spec), collapse=False))
            assert h["cite"] in got, (spec, h)


def test_to_is_inclusive_to_its_precision(br: Tools) -> None:
    # The tombstone's record is at 00:00:05.123456789: '...05.123' and '00:00:05' include it.
    for to in ("01-01 00:00:05.123", "00:00:05", "01-01 00:00"):
        assert "log:11#1" in cites(br.search("br", None, filter=F(from_="00:00:05", to=to), collapse=False))
    assert "log:11#1" not in cites(br.search("br", None, filter=F(to="01-01 00:00:05.122"), collapse=False))


def test_relative_times(br: Tools) -> None:
    # Case end: 01-01 00:06:00 (dropbox crash); -1m starts at 00:05:00.
    assert cites(br.search("br", None, filter=F(from_="-1m"), collapse=False)) == ["log:6#1", "log:7#1"]
    first = cites(br.search("br", None, filter=F(to="+1s500ms"), collapse=False))
    assert first == ["log:8#1"]  # starts 12-31 23:50:00
    got = cites(br.search("br", None, filter=F(from_="art:6", to="+3m"), collapse=False))
    assert got == ["log:11#1", "log:12#1"]


@pytest.mark.parametrize(
    ("flt", "match"),
    [
        ({"from": "yesterday"}, "cannot read from='yesterday'"),
        ({"from": "13-45 00:00:00"}, "invalid date"),
        ({"from": "01-01 00:00:00", "to": "100.0"}, "different clocks"),
        ({"from": "00:06:00", "to": "00:05:00"}, "after 'to'"),
        ({"from": "12:00:00"}, "outside the case"),
        ({"sources": ["99"]}, "unknown source '99'; sources in case 'br': 1-12"),
        ({"from": "log:3#999"}, "no record log:3#999"),
        ({"from": "art:99"}, "no artifact art:99"),
        ({"attrs": {"bad key": 1}}, "invalid attrs key"),
    ],
)
def test_filter_errors(br: Tools, flt: dict[str, Any], match: str) -> None:
    with pytest.raises(ToolInputError, match=re.escape(match)):
        br.search("br", "pid", filter=Filter.model_validate(flt))


def test_ambiguous_year(app: App, tmp_path: Path) -> None:
    p = tmp_path / "y.log"
    p.write_text(
        "2025-06-01 10:00:00.000  100  100 I A: one\n2026-06-02 10:00:00.000  100  100 I A: two\n",
        encoding="utf-8",
    )
    app.ingestor.add(app.config.case_db("y"), str(p), "logcat")
    t = Tools(app)
    with pytest.raises(ToolInputError, match="ambiguous"):
        t.search("y", None, filter=F(from_="06-01 09:00:00"))
    assert cites(t.search("y", None, filter=F(from_="2026-01-01"), collapse=False)) == ["log:1#2"]


def test_tags_not_tags_pid_attrs(br: Tools) -> None:
    assert cites(br.search("br", None, filter=F(tags=["Hvac*"]), collapse=False)) == ["log:1#1", "log:1#2"]
    got = cites(br.search("br", None, filter=F(pid=1234, not_tags=["Hvac*", "java_*"]), collapse=False))
    assert got == ["log:3#1"]
    assert cites(br.search("br", None, filter=F(attrs={"art": 6}), collapse=False)) == ["log:11#1"]
    assert cites(br.search("br", None, filter=F(attrs={"uid": 1000}), collapse=False))[:2] == [
        "log:4#1",
        "log:1#1",
    ]
    out = br.search("br", None, filter=F(tags=["hvac*"]))
    assert out.data.model_dump()["total"] == 0 and "similar: HvacCtrl" in out.text


def test_wall_filter_warns_about_monotonic_sources(br: Tools) -> None:
    out = br.search("br", "pid", filter=F(from_="-10m"))
    assert "records of source 5 have only a monotonic clock" in out.text


# -- search ------------------------------------------------------------------------------------


def test_regex_prefilter_literals() -> None:
    assert query.required_literals(r"status=\w+_AVAIL") == ["status=", "_AVAIL"]
    assert query.required_literals(r"Service '(\w+)' exited") == ["Service '", "' exited"]
    assert query.required_literals(r"colou?r") == ["colo"]
    assert query.required_literals(r"a|b") == []
    assert query.required_literals(r"(?i)fatal") == []
    assert query.required_literals(r"x{0,2}yzzy\.so") == ["yzzy.so"]
    assert query.literals_fts(["status=", "_AVAIL"]) == '"AVAIL"*'
    assert query.literals_fts(["Service 'vendor.hvac' (pid"]) == '"vendor" "hvac" "pid"*'
    assert query.literals_fts(["abc"]) is None


@pytest.mark.parametrize(
    "pattern",
    [
        r"status=\w+_AVAIL",
        r"(pid \d+)",
        r"Native crash SIG[A-Z]+",
        r"init: Service '[^']+' exited",
        r"<MAC:[0-9a-f]+>",
        r"\.example\.",
        r"Controller\.java:4\d",
        r"^\[art:\d\] ",
        r"ab?c",
    ],
)
def test_regex_prefilter_never_drops_matches(br: Tools, pattern: str) -> None:
    """The SQL prefilter (instr + FTS) is a superset: same hits as a plain scan of every record."""
    rx = re.compile(pattern)
    with br.app.connect("br") as conn:
        conn.row_factory = None
        want = [
            f"log:{rid >> 40}#{rid & ((1 << 40) - 1)}"
            for rid, msg, tag in conn.execute("SELECT id, msg, tag FROM records_v")
            if rx.search(msg) or (tag and rx.search(tag))
        ]
    conn.close()
    got = br.search("br", pattern, mode="regex", collapse=False, limit=50)
    assert sorted(cites(got)) == sorted(want)


def test_collapse_and_paging_on_generated_log(app: App, tmp_path: Path) -> None:
    log = tmp_path / "gen.log"
    subprocess.run([sys.executable, str(GEN), str(log), "--mb", "2"], check=True)
    app.ingestor.add(app.config.case_db("g"), str(log))
    t = Tools(app)
    out = t.search("g", "setProperty failed")
    data = out.data.model_dump()
    total = data["total"]
    # One template (numbers masked), one group per buffer (main, system are separate sources).
    assert total > 500 and data["groups"] == len(data["hits"]) == 2
    assert {h["template"] for h in data["hits"]} == {"setProperty failed, status=NOT_AVAILABLE, prop=<*>"}
    assert sum(h["count"] for h in data["hits"]) == total
    hit = data["hits"][0]
    assert hit["last"] != hit["cite"]
    assert hit["literal"] == "setProperty failed, status=NOT_AVAILABLE, prop="
    assert f"×{hit['count']} until " in out.text

    # Paging without collapse: pages are disjoint, in time order, and cover every match.
    f = F(tags=["HvacCtrl"], to="10-11 08:00:30")
    seen: list[str] = []
    times: list[str] = []
    cursor = None
    while True:
        page = t.search("g", "setProperty", filter=f, collapse=False, limit=50, cursor=cursor)
        d = page.data.model_dump()
        seen += [h["cite"] for h in d["hits"]]
        times += [h["ts"] for h in d["hits"]]
        assert fmt.utf8_len(page.text) <= 8192
        cursor = d.get("next_cursor")
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == d["total"]
    assert times == sorted(times)

    broad = t.search("g", "com", limit=500)
    assert fmt.utf8_len(broad.text) <= 8192
    assert "limit 500 reduced to 50" in broad.text and "next page: cursor=" in broad.text
    assert "narrow by level (" in broad.text and "tags (" in broad.text

    n = t.search("g", "Delivering", mode="regex", count_only=True).data.model_dump()["total"]
    assert n == t.search("g", "Delivering", count_only=True).data.model_dump()["total"] > 0


def test_regex_scan_cap(app: App, tmp_path: Path) -> None:
    p = copy_fixture("logcat/threadtime.log", tmp_path)
    app.ingestor.add(app.config.case_db("c"), str(p))
    small = App.create(replace(app.config, caps=replace(app.config.caps, regex_scan_rows=3)), Policy())
    out = Tools(small).search("c", ".", mode="regex", count_only=True)
    d = out.data.model_dump()
    assert d["exact"] is False and d["total"] == 3
    assert (
        out.text.startswith("at least 3 matches")
        and "regex scan stopped after 3 candidate records" in out.text
    )


def test_text_query_errors_and_hints(br: Tools) -> None:
    with pytest.raises(ToolInputError, match="invalid text query"):
        br.search("br", '"unbalanced')
    with pytest.raises(ToolInputError, match="invalid regex"):
        br.search("br", "(", mode="regex")
    with pytest.raises(ToolInputError, match="does not exist"):
        br.search("nope", "x")
    out = br.search("br", "setProp*")
    assert cites(out) == ["log:1#2"]
    assert (
        looks_like_regex(r"foo.*bar")
        and looks_like_regex(r"\d+")
        and not looks_like_regex("setProperty failed")
    )


def test_cursor_belongs_to_its_query(br: Tools) -> None:
    out = br.search("br", "pid", collapse=False, limit=2)
    cur = out.data.model_dump()["next_cursor"]
    assert cur
    assert len(cites(br.search("br", "pid", collapse=False, limit=2, cursor=cur))) == 2
    with pytest.raises(ToolInputError, match="different query"):
        br.search("br", "com", collapse=False, cursor=cur)
    with pytest.raises(ToolInputError, match="invalid cursor"):
        br.search("br", "pid", cursor="!!")


# -- caps -------------------------------------------------------------------------------------


def test_tiny_caps_hold_for_every_tool(app: App, tmp_path: Path) -> None:
    app.ingestor.add(app.config.case_db("br"), str(make_bugreport_zip(tmp_path)))
    caps = Caps(max_lines=4, max_bytes=1500, max_msg_chars=60)
    t = Tools(App.create(replace(app.config, caps=caps), Policy()))
    outs = [
        t.search("br", "pid"),
        t.search("br", None, collapse=False),
        t.context("br", "log:1#2", before=20, after=20),
        t.list_artifacts("br"),
        t.read_artifact("br", "art:6"),
        t.read_artifact("br", "art:6/threads"),
        t.get_records("br", ["log:3#1", "log:9#1", "log:11#1", "log:7#1"]),
    ]
    for o in outs:
        assert fmt.utf8_len(o.text) <= caps.max_bytes, o.text
    assert "next page" in outs[0].text
    assert "before/after reduced to" in outs[2].text
    assert "more groups" in outs[3].text or "output cap" in outs[3].text or len(outs[3].text.split("\n")) <= 8
    assert "not shown" in outs[5].text and "next: read_artifact(id='art:6/threads:L" in outs[5].text
    msgs = [h["msg"] for h in outs[0].data.model_dump()["hits"]]
    assert all(len(m) <= 60 for m in msgs) and any(m.endswith("…") for m in msgs)


def test_get_records_full_message_and_byte_cap(app: App, tmp_path: Path) -> None:
    p = tmp_path / "long.log"
    frames = "".join(
        f"10-11 08:00:00.000  100  101 E AndroidRuntime: \tat a.b.C.m{i}(C.java:{i})\n" for i in range(400)
    )
    p.write_text(
        "10-11 08:00:00.000  100  101 E AndroidRuntime: FATAL EXCEPTION: main\n"
        + frames
        + "10-11 08:00:01.000  100  101 I Other: after\n",
        encoding="utf-8",
    )
    app.ingestor.add(app.config.case_db("l"), str(p), "logcat")
    t = Tools(app)
    s = t.search("l", "FATAL")
    assert "⏎" in s.text and "…" in s.text
    out = t.get_records("l", ["log:1#1", "log:1#2"])
    assert fmt.utf8_len(out.text) <= 8192
    assert "message cut at the output cap" in out.text and "not shown" in out.text
    small = t.get_records("l", ["log:1#2"])
    assert small.text.endswith("  after")


# -- context ----------------------------------------------------------------------------------


def test_context_byte_cap_keeps_the_anchor_centred(app: App, tmp_path: Path) -> None:
    p = tmp_path / "wide.log"
    p.write_text(
        "".join(
            f"10-11 08:00:{i % 60:02d}.000  100  101 I Wide: {i} " + "x" * 190 + "\n" for i in range(100)
        ),
        encoding="utf-8",
    )
    app.ingestor.add(app.config.case_db("w"), str(p), "logcat")
    out = Tools(app).context("w", "log:1#50", before=24, after=24)
    lines = out.text.split("\n")
    assert fmt.utf8_len(out.text) <= 8192 and "lines left out" in out.text
    recs = [x for x in lines if x.startswith(("> ", "  log:"))]
    idx = next(i for i, x in enumerate(recs) if x.startswith(">"))
    assert abs(idx - (len(recs) - 1 - idx)) <= 1


def test_context_pid_scope_merges_buffers(br: Tools) -> None:
    out = br.context("br", "log:1#2", scope="pid")
    got = [r["cite"] for r in out.data.model_dump()["records"]]
    assert got == ["log:1#1", "log:1#2", "log:3#1", "log:9#1"]  # main, crash, dropbox: same file
    with pytest.raises(ToolInputError, match="has no pid"):
        br.context("br", "log:5#2", scope="pid")
    with pytest.raises(ToolInputError, match="source 5 has 3 records"):
        br.context("br", "log:5#9")


# -- artifacts ----------------------------------------------------------------------------------


def test_list_artifacts_groups_copies_of_one_crash(br: Tools) -> None:
    d = br.list_artifacts("br").data.model_dump()
    assert d["total"] == 7 and len(d["groups"]) == 6
    first = d["groups"][0]
    assert [a["cite"] for a in first["artifacts"]] == ["art:1", "art:4"]
    assert first["artifacts"][1]["same_as"] == "art:1"
    assert "MainActivity.onFanClicked" in first["signature"]  # the fuller signature (dropbox copy)
    assert br.read_artifact("br", "art:4").data.model_dump()["same_crash"] == ["art:1"]


def test_read_artifact_errors(br: Tools) -> None:
    for args, match in [
        ({"id": "art:99"}, "no artifact art:99"),
        ({"id": "art:6/nope"}, "has no section 'nope'; sections: header, registers"),
        ({"id": "art:6/backtrace:L2"}, "outside section 'backtrace' (L16-L22)"),
        ({"id": "art:6", "start_line": 49, "end_line": 60}, "outside art:6 (L1-L50)"),
        ({"id": "art:6/header", "section": "backtrace"}, "conflicts"),
        ({"id": "art:6:L3", "start_line": 4}, "either in the citation"),
    ]:
        with pytest.raises(ToolInputError, match=re.escape(match)):
            br.read_artifact("br", **args)
    one = br.read_artifact("br", "art:6", start_line=48).data.model_dump()
    assert [x["n"] for x in one["lines"]] == [48, 49, 50]


def test_artifact_citations_resolve(br: Tools) -> None:
    """Every art: and log: citation the artifact tools print resolves to an existing line."""
    text = br.list_artifacts("br").text
    for aid in sorted(set(re.findall(r"\bart:(\d+)\b", text))):
        summary = br.read_artifact("br", f"art:{aid}")
        for sec in re.findall(rf"art:{aid}/([a-z0-9_.-]+)\s+L\d+-L\d+", summary.text):
            lines = br.read_artifact("br", f"art:{aid}/{sec}").data.model_dump()["lines"]
            assert lines
            n = lines[0]["n"]
            assert br.read_artifact("br", f"art:{aid}/{sec}:L{n}").data.model_dump()["lines"][0]["n"] == n
    for cite in set(re.findall(r"\blog:\d+#\d+\b", text)):
        assert br.get_records("br", [cite]).data.model_dump()["records"][0]["cite"] == cite


def test_no_artifacts_reason(app: App, tmp_path: Path) -> None:
    app.ingestor.add(app.config.case_db("c"), str(copy_fixture("logcat/time.log", tmp_path)))
    out = Tools(app).list_artifacts("c")
    assert "No artifacts in case 'c'" in out.text and "search(" in out.text
    app.ingestor.add(app.config.case_db("t"), str(copy_fixture("tombstone/tombstone_00", tmp_path)))
    out = Tools(app).list_artifacts("t", "anr")
    assert out.text == "No artifacts of type 'anr'; types in the case: tombstone ×1"


# -- policy -------------------------------------------------------------------------------------


def test_blocked_sources_are_invisible(app: App, tmp_path: Path) -> None:
    app.ingestor.add(app.config.case_db("br"), str(make_bugreport_zip(tmp_path)))
    blocked = App.create(app.config, Policy(blocked_sources=("*tombstone_00*",)))
    t = Tools(blocked)
    assert "art:6" not in t.list_artifacts("br").text
    assert "log:11#" not in t.search("br", "Native").text
    with pytest.raises(ToolInputError):
        t.read_artifact("br", "art:6")
    with pytest.raises(ToolInputError):
        t.context("br", "log:11#1")
    with pytest.raises(ToolInputError):
        t.get_records("br", ["log:11#1"])
    with pytest.raises(ToolInputError, match="unknown source '11'"):
        t.search("br", "x", filter=F(sources=["11"]))


def test_structured_content_matches_text(br: Tools) -> None:
    out = br.search("br", "pid", filter=F(level="W"))
    d = json.loads(json.dumps(out.data.model_dump(mode="json", exclude_none=True)))
    for h in d["hits"]:
        assert h["cite"] in out.text
