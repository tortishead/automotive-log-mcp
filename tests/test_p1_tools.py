"""Tools overview and count.

Golden outputs (text and structured content) on the bugreport zip are in
``tests/golden/tools__{overview,count}.json``; regenerate with
``UPDATE_GOLDEN=1`` and review the diff.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from automotive_log_mcp.app import App
from automotive_log_mcp.config import Caps
from automotive_log_mcp.core import format as fmt
from automotive_log_mcp.policy import Policy
from automotive_log_mcp.tools import ToolInputError, Tools
from automotive_log_mcp.tools import limits as tools_mod

from .conftest import REF_TIME, check_golden
from .test_golden import make_bugreport_zip
from .test_tools import F, br  # noqa: F401  (fixture)

# -- golden outputs ------------------------------------------------------------------------------

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
    ]
    for o in outs:
        assert fmt.utf8_len(o.text) <= caps.max_bytes, o.text
    assert "more values" in outs[1].text
    with pytest.raises(ToolInputError, match="at most 4"):
        t.count("br", bucket="10s")  # 4 buckets at most with max_lines=4


# -- count -------------------------------------------------------------------------------------


def test_count_matches_search_totals(br: Tools) -> None:  # noqa: F811
    for q, mode in (("hvac", "text"), (r"pid \d+", "regex")):
        total = br.search("br", q, mode=mode, count_only=True).data.total
        for by in ("tag", "level", "pid", "source", "template", None):
            assert br.count("br", by=by, text=q, mode=mode).data.total == total
    every = sum(s.records for s in br.app.list_sources("br") or [])
    assert br.count("br", by="source").data.total == every


def test_count_buckets_include_empty_and_reject_too_many(br: Tools) -> None:  # noqa: F811
    out = br.count("br", by=None, bucket="1m")
    buckets = out.data.buckets
    assert len(buckets) == 17 and sum(b.count for b in buckets) == out.data.total
    assert any(b.count == 0 for b in buckets)
    assert buckets[0].ts == "2025-12-31 23:50:00.000"
    # every printed bucket start is accepted back as 'from'
    first = out.text.split("\n")[1].split()[:2]
    assert br.search("br", None, filter=F(from_=" ".join(first), to="+59s"), count_only=True).data.total == 1
    with pytest.raises(ToolInputError, match="use bucket='30s'"):
        br.count("br", bucket="1s")
    with pytest.raises(ToolInputError, match="invalid bucket"):
        br.count("br", bucket="soon")


def test_count_broad_query_is_sampled(br: Tools, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    exact = br.count("br", by="level", text="com").data.total
    monkeypatch.setattr(tools_mod, "AGG_ROWS", 1)
    out = br.count("br", by="level", text="com")
    assert out.data.exact is False and out.text.startswith("≈")
    assert "estimated from every" in out.text
    assert out.data.total > 0 and exact > 0


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
    assert out.data.records == 3 and out.data.mono_from == "0.000000"
    assert "since boot 0.000000 .. 124.000000" in out.text
    with pytest.raises(ToolInputError, match="does not exist"):
        br.overview("nope")
