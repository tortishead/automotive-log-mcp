"""M6 diff: only-in-A, only-in-B and changed messages between two sides (sources, segments, time
windows), every item cited, capped, on sides with different clocks; the CLI and MCP surface."""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import anyio
import pytest
from mcp import Client

from automotive_log_mcp.app import App
from automotive_log_mcp.cli import main as cli_main
from automotive_log_mcp.config import Caps, Config
from automotive_log_mcp.core import format as fmt
from automotive_log_mcp.difftool import DiffResult
from automotive_log_mcp.policy import Policy
from automotive_log_mcp.server import build_server
from automotive_log_mcp.tools import DiffSide, ToolInputError, Tools

from .conftest import REF_TIME


def _logcat(t: int, level: str, tag: str, msg: str, pid: int = 100) -> str:
    return f"10-01 08:{t // 60:02d}:{t % 60:02d}.000  {pid:4d}  {pid:4d} {level} {tag:<8}: {msg}"


def _write(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(path, (REF_TIME, REF_TIME))
    return path


def _good_bad(tmp_path: Path) -> tuple[Path, Path]:
    good, bad = [], []
    for i in range(40):
        good.append(_logcat(i, "I", "CarService", f"heartbeat {i} ok"))
        bad.append(_logcat(i, "I", "CarService", f"heartbeat {i} ok"))
    good += [_logcat(50 + i, "W", "VehicleHal", f"retry {i} for prop 0x1560050{i}") for i in range(3)]
    bad += [_logcat(50 + i // 2, "W", "VehicleHal", f"retry {i} for prop 0x156005{i % 9}") for i in range(30)]
    good.append(_logcat(90, "I", "BootReceiver", "boot completed in 812 ms"))
    bad.append(_logcat(85, "E", "AudioHal", "firmware load failed for /vendor/firmware/dsp7.bin (-2)"))
    bad.append(_logcat(86, "E", "AudioHal", "firmware load failed for /vendor/firmware/dsp8.bin (-2)"))
    bad.append(_logcat(87, "I", "AudioHal", "using fallback route speaker"))
    return _write(tmp_path / "good.log", good), _write(tmp_path / "bad.log", bad)


@pytest.fixture
def tools(app: App) -> Tools:
    return Tools(app)


def side(**kw: object) -> DiffSide:
    return DiffSide.model_validate(kw)


def _resolve_all(tools: Tools, case: str, d: DiffResult) -> None:
    """Every citation in the result resolves, and to a record of the item's message."""
    items = [*d.only_a, *d.only_b, *d.changed]
    cites = [c for it in items for c in (it.cite_a, it.cite_b) if c]
    assert cites
    recs = tools.get_records(case, cites).data.records  # type: ignore[attr-defined]
    assert [r.cite for r in recs] == cites
    by_cite = {r.cite: r for r in recs}
    for it in items:
        for c in (it.cite_a, it.cite_b):
            if c:
                assert by_cite[c].tag == it.tag and by_cite[c].level == it.level
                assert by_cite[c].template.split("<*>")[0] == it.template.split("<*>")[0]


def test_two_runs_only_in_each_and_changed(app: App, tools: Tools, tmp_path: Path) -> None:
    good, bad = _good_bad(tmp_path)
    db = app.config.case_db("d")
    app.ingestor.add(db, str(good))
    app.ingestor.add(db, str(bad))
    out = tools.diff("d", side(sources=["1"]), side(sources=["2"]))
    d = out.data
    assert isinstance(d, DiffResult)
    assert (d.a.records, d.b.records) == (44, 73)
    assert [(i.tag, i.level, i.count_b, i.cite_b) for i in d.only_b] == [
        ("AudioHal", "E", 2, "log:2#71"),  # most severe first
        ("AudioHal", "I", 1, "log:2#73"),
    ]
    assert [(i.tag, i.count_a, i.cite_a, i.cite_b) for i in d.only_a] == [
        ("BootReceiver", 1, "log:1#44", None)
    ]
    assert len(d.changed) == 1
    ch = d.changed[0]
    assert (ch.tag, ch.count_a, ch.count_b, ch.cite_a, ch.cite_b) == (
        "VehicleHal",
        3,
        30,
        "log:1#41",
        "log:2#41",
    )
    # shares, not counts: (30/73) / (3/44)
    assert ch.ratio == round((30 / 73) / (3 / 44), 2)
    assert d.same == 2 and (d.only_b_total, d.only_a_total, d.changed_total) == (2, 1, 1)
    assert "log:2#71  E AudioHal  firmware load failed for <*> (<*>)  ×2" in out.text
    assert "×3 → ×30 (6×)  log:1#41 / log:2#41  W VehicleHal" in out.text
    _resolve_all(tools, "d", d)
    # the text's citations pass check_citations
    assert tools.check_citations("d", out.text).data.ok  # type: ignore[attr-defined]
    # min_ratio above the change: not listed; reversed sides swap the lists
    assert tools.diff("d", side(sources=["1"]), side(sources=["2"]), min_ratio=7).data.changed == []  # type: ignore[attr-defined]
    r = tools.diff("d", side(sources=["2"]), side(sources=["1"])).data
    assert [i.cite_a for i in r.only_a] == ["log:2#71", "log:2#73"]  # type: ignore[attr-defined]
    assert r.changed[0].ratio is not None and r.changed[0].ratio < 1  # type: ignore[attr-defined]
    assert "(1/6×)" in tools.diff("d", side(sources=["2"]), side(sources=["1"])).text


def test_level_filter_per_side_and_no_difference(app: App, tools: Tools, tmp_path: Path) -> None:
    good, bad = _good_bad(tmp_path)
    db = app.config.case_db("d")
    app.ingestor.add(db, str(good))
    app.ingestor.add(db, str(bad))
    with pytest.raises(ToolInputError, match="a selects no records"):
        tools.diff("d", side(sources=["1"], level="E"), side(sources=["2"], level="E"))
    d = tools.diff("d", side(sources=["1"], level="W"), side(sources=["2"], level="W")).data
    assert [i.tag for i in d.only_b] == ["AudioHal"] and d.only_a == []  # type: ignore[attr-defined]
    same = tools.diff("d", side(sources=["1"], to="08:00:39"), side(sources=["2"], to="08:00:39"))
    assert "No difference" in same.text


def test_two_boots_of_a_kernel_log_by_segment(app: App, tools: Tools, tmp_path: Path) -> None:
    boot = "[    0.000000] Booting Linux on physical CPU 0x0"
    common = [f"[    {i}.100000] usb {i}-1: new device ready" for i in range(1, 6)]
    lines = [boot, *common, "[    7.000000] wlan: firmware loaded ver 12"]
    lines += [boot, *common, "[    7.000000] wlan: firmware load failed, error -2"]
    lines += ["[    7.500000] wlan: firmware load failed, error -2"]
    path = _write(tmp_path / "dmesg.txt", lines)
    app.ingestor.add(app.config.case_db("k"), str(path))
    out = tools.diff("k", side(sources=["1"], seg=1), side(sources=["1"], seg=2))
    d = out.data
    assert d.a.selects == "source 1 segment 1"  # type: ignore[attr-defined]
    assert [(i.template, i.count_b, i.cite_b) for i in d.only_b] == [  # type: ignore[attr-defined]
        ("wlan: firmware load failed, error <*>", 2, "log:1#14")
    ]
    assert [(i.template, i.cite_a) for i in d.only_a] == [("wlan: firmware loaded ver <*>", "log:1#7")]  # type: ignore[attr-defined]
    assert "different clocks" not in out.text
    _resolve_all(tools, "k", d)  # type: ignore[arg-type]
    with pytest.raises(ToolInputError, match="segments 1, 2"):
        tools.diff("k", side(sources=["1"], seg=1), side(sources=["1"], seg=3))
    with pytest.raises(ToolInputError, match="exactly one source"):
        tools.diff("k", side(seg=1), side(sources=["1"], seg=2))
    with pytest.raises(ToolInputError, match="same records"):
        tools.diff("k", side(sources=["1"], seg=2), side(sources=["1"], seg=2))
    with pytest.raises(ToolInputError, match="min_ratio"):
        tools.diff("k", side(sources=["1"], seg=1), side(sources=["1"], seg=2), min_ratio=1)


def test_sides_on_different_clocks(app: App, tools: Tools, tmp_path: Path) -> None:
    """A dmesg (seconds since boot, never aligned) against a logcat (wall clock): each side's
    from/to is read on its own clock; no alignment is needed."""
    dmesg = _write(
        tmp_path / "dmesg.txt",
        [
            "[   10.000000] can0: bus-off, restarting",
            "[   20.000000] can0: controller restarted",
            "[   30.000000] usb 1-1: new device ready",
        ],
    )
    good, _ = _good_bad(tmp_path)
    db = app.config.case_db("c")
    app.ingestor.add(db, str(good))
    app.ingestor.add(db, str(dmesg))
    out = tools.diff("c", side(sources=["2"], to="25.0"), side(sources=["1"], **{"from": "08:00:30"}))
    d = out.data
    assert d.a.records == 2 and d.b.records == 14  # type: ignore[attr-defined]
    assert "different clocks" in out.text
    assert {i.cite_a for i in d.only_a} == {"log:2#1", "log:2#2"}  # type: ignore[attr-defined]
    # a wall-clock time without a date cannot be read on the monotonic side; the error names it
    with pytest.raises(ToolInputError, match=r"^a: to='08:00:30' has no date"):
        tools.diff("c", side(sources=["2"], to="08:00:30"), side(sources=["1"]))


def test_caps_hold(app: App, tmp_path: Path) -> None:
    lines_a = [_logcat(i % 60, "I", f"TagA{i}", f"only in a number {i}") for i in range(80)]
    lines_b = [_logcat(i % 60, "E", f"TagB{i}", f"only in b number {i} " + "x" * 150) for i in range(80)]
    a = _write(tmp_path / "a.log", lines_a)
    b = _write(tmp_path / "b.log", lines_b)
    caps = Caps(max_lines=4, max_bytes=1500, max_msg_chars=60)
    small = App.create(dataclasses.replace(app.config, caps=caps), Policy())
    small.ingestor.add(small.config.case_db("cap"), str(a))
    small.ingestor.add(small.config.case_db("cap"), str(b))
    t = Tools(small)
    out = t.diff("cap", side(sources=["1"]), side(sources=["2"]), limit=50)
    assert fmt.utf8_len(out.text) <= caps.max_bytes, out.text
    assert "more (" in out.text and "Only in A" in out.text and "Only in B" in out.text
    d = out.data
    assert len(d.only_b) <= 4 and d.only_b_total == 80  # type: ignore[attr-defined]
    assert all(len(i.template) <= 60 for i in d.only_b)  # type: ignore[attr-defined]
    assert "limit 50 reduced to 4" in out.text
    # default caps: 80 + 80 items still fit 8 KB
    full = Tools(app)
    app.ingestor.add(app.config.case_db("cap"), str(a))
    app.ingestor.add(app.config.case_db("cap"), str(b))
    out = full.diff("cap", side(sources=["1"]), side(sources=["2"]), limit=100)
    assert fmt.utf8_len(out.text) <= 8192


def test_cli_diff(app: App, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good, bad = _good_bad(tmp_path)
    assert cli_main(["index", "d", str(good), "-q"]) == 0
    assert cli_main(["index", "d", str(bad), "-q"]) == 0
    capsys.readouterr()
    assert cli_main(["diff", "d", "--a", "1", "--b", "2", "--level", "W"]) == 0
    text = capsys.readouterr().out
    assert "Only in B, most severe first (1):" in text and "log:2#71" in text
    assert cli_main(["diff", "d", "--a", "1", "--b", "2", "--b-from", "08:01:00"]) == 0
    assert "B: source 2 from 08:01:00" in capsys.readouterr().out
    assert cli_main(["diff", "d", "--a", "x", "--b", "2"]) == 2
    assert "--a takes" in capsys.readouterr().err


def test_diff_over_mcp(config: Config, tmp_path: Path) -> None:
    good, bad = _good_bad(tmp_path)
    cfg = dataclasses.replace(config, structured_output=True)

    async def main() -> None:
        async with Client(build_server(cfg, Policy())) as client:
            for p in (good, bad):
                r = await client.call_tool("add_source", {"case": "m", "path": str(p)})
                assert not r.is_error
            r = await client.call_tool(
                "diff", {"case": "m", "a": {"sources": ["1"]}, "b": {"sources": ["2"]}}
            )
            assert not r.is_error, r
            assert r.structured_content is not None
            DiffResult.model_validate(r.structured_content)
            assert r.structured_content["only_b"][0]["cite_b"] == "log:2#71"
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert tools["diff"].annotations is not None and tools["diff"].annotations.read_only_hint
            assert "seg" in json.dumps(tools["diff"].input_schema)
            bad_args = await client.call_tool(
                "diff", {"case": "m", "a": {"sources": ["1"]}, "b": {"sources": ["2"]}, "level": "W"}
            )
            assert bad_args.is_error
            assert "'level' belongs inside a and b" in bad_args.content[0].text  # type: ignore[union-attr]

    anyio.run(main)
