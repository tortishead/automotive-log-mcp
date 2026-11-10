"""M5 clock model: fitting, automatic alignment (same lines, UTC kernel lines, markers, boot
estimate), manual alignment, timeline and context(scope='all')."""

from __future__ import annotations

import dataclasses
import os
import random
from pathlib import Path

import pytest

from automotive_log_mcp.app import App
from automotive_log_mcp.config import Config
from automotive_log_mcp.core import align as al
from automotive_log_mcp.core import clock as ck
from automotive_log_mcp.core.clock import Piece, SegClock
from automotive_log_mcp.core.timeparse import NS, fmt_wall, wall_ns
from automotive_log_mcp.policy import Policy
from automotive_log_mcp.store import db
from automotive_log_mcp.tools import Filter, ToolInputError, Tools

from .conftest import FIXED_SECRET, FIXTURES, REF_TIME

MS = 1_000_000
T0 = wall_ns(2026, 10, 1, 8, 0, 0)  # wall time of the device's boot in the generated logs
WORDS = [
    "alpha",
    "bravo",
    "charlie",
    "delta",
    "echo",
    "foxtrot",
    "golf",
    "hotel",
    "india",
    "juliet",
    "kilo",
    "lima",
    "mike",
    "november",
    "oscar",
    "papa",
    "quebec",
    "romeo",
    "sierra",
    "tango",
    "uniform",
    "victor",
    "whiskey",
    "xray",
    "yankee",
    "zulu",
]
TAGS = ("binder", "usbcore", "wlan", "thermal", "audit", "ion", "sdhci", "rtc")


def _write(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(path, (REF_TIME, REF_TIME))
    return path


def _logcat_line(t: int, tag: str, msg: str, level: str = "I", pid: int = 0) -> str:
    return f"{fmt_wall(t)[5:]}  {pid:4d}  {pid:4d} {level} {tag:<8}: {msg}"


def _dmesg_line(mono: int, tag: str, msg: str) -> str:
    secs, frac = divmod(mono, NS)
    return f"[{secs:5d}.{frac // 1000:06d}] {tag}: {msg}"


def _kernel_events(n: int, seed: int = 1) -> list[tuple[int, str, str]]:
    """(mono ns, tag, message) with distinct words, so every line has its own template."""
    rnd = random.Random(seed)
    out = []
    used = set()
    t = 1 * NS
    while len(out) < n:
        t += rnd.randint(50, 4000) * MS
        words = " ".join(rnd.sample(WORDS, 3))
        if words in used:
            continue
        used.add(words)
        out.append((t, rnd.choice(TAGS), f"{words} ready"))
    return out


@pytest.fixture
def tools(app: App) -> Tools:
    return Tools(app)


def _app_with_rules(config: Config, rule_dir: Path) -> App:
    cfg = dataclasses.replace(config, rule_dirs=(rule_dir,))
    cfg.home.mkdir(parents=True, exist_ok=True)
    (cfg.home / "secret.key").write_bytes(FIXED_SECRET)
    return App.create(cfg, Policy())


def _case_times(app: App, case: str, sid: int) -> dict[int, int | None]:
    with db.opened(app.config.case_db(case)) as conn:
        cc = ck.load(conn)
        rows = conn.execute(
            "SELECT seq, seg, ts, ts_src FROM records WHERE id >> 40 = ? ORDER BY id", (sid,)
        ).fetchall()
    return {seq: cc.case_time(sid, seg, ts, ts_src) for seq, seg, ts, ts_src in rows}


# -- fitting ----------------------------------------------------------------------------------------


def test_fit_constant_offset_with_jitter() -> None:
    rnd = random.Random(3)
    pairs = [(x * NS, x * NS + 5 * NS + rnd.randint(-2, 2) * MS) for x in range(1, 40)]
    fit = al.fit_pairs(pairs)
    assert fit is not None and len(fit.pieces) == 1
    assert abs(fit.pieces[0].offset_ns - 5 * NS) <= 2 * MS
    assert fit.error_ns <= 4 * MS


def test_fit_finds_drift() -> None:
    ppm = 40.0
    pairs = [(x * NS, x * NS + 2 * NS + round(ppm * 1e-6 * (x - 10) * NS)) for x in range(10, 3600, 60)]
    fit = al.fit_pairs(pairs)
    assert fit is not None
    p = fit.pieces[0]
    assert p.drift_ppm == pytest.approx(ppm, rel=0.01)
    assert fit.error_ns < MS
    # Without the drift term the error would be ~140 ms at the end.
    assert max(abs(p.map(x) - y) for x, y in pairs) < MS


def test_fit_splits_where_a_monotonic_clock_stopped() -> None:
    first = [(x * NS, x * NS + 100 * NS) for x in range(1, 20)]
    second = [(x * NS, x * NS + 130 * NS) for x in range(20, 40)]  # 30 s of suspend
    fit = al.fit_pairs(first + second)
    assert fit is not None
    assert [p.offset_ns for p in fit.pieces] == [100 * NS, 130 * NS]
    sc = SegClock(1, 1, "mono", 1, 40, fit.pieces, "paired", "")
    assert sc.map(5 * NS) == 105 * NS and sc.map(25 * NS) == 155 * NS


def test_fit_rejects_offsets_that_do_not_agree() -> None:
    rnd = random.Random(5)
    pairs = [(x * NS, x * NS + rnd.randint(0, 3600) * NS) for x in range(1, 50)]
    assert al.fit_pairs(pairs) is None


def test_fit_ignores_a_few_outliers() -> None:
    pairs = [(x * NS, x * NS + 7 * NS) for x in range(1, 30)]
    pairs[10] = (pairs[10][0], pairs[10][0] + 900 * NS)
    pairs[20] = (pairs[20][0], pairs[20][0] - 50 * NS)
    fit = al.fit_pairs(pairs)
    assert fit is not None and len(fit.pieces) == 1 and fit.pieces[0].offset_ns == 7 * NS
    assert fit.inliers == 27


def test_invert_and_compose_round_trip() -> None:
    pieces = [Piece(0, 5 * NS, 20.0), Piece(100 * NS, 9 * NS, 20.0)]
    back = al.invert(pieces)
    sc, inv = SegClock(1, 1, "mono", 1, 1, pieces, "", ""), SegClock(1, 1, "mono", 1, 1, back, "", "")
    for x in (1 * NS, 50 * NS, 150 * NS, 400 * NS):
        assert abs(inv.map(sc.map(x)) - x) <= 2
    composed = al.compose(pieces, [Piece(0, -1 * NS)])
    for x in (1 * NS, 150 * NS):
        assert SegClock(1, 1, "mono", 1, 1, composed, "", "").map(x) == sc.map(x) - NS


def test_native_windows_cover_every_piece() -> None:
    sc = SegClock(1, 1, "mono", 1, 9, [Piece(0, 100 * NS), Piece(50 * NS, 200 * NS)], "paired", "")
    # Case 120..130 s: native 20..30 s (first piece); case 255..260: native 55..60 (second piece).
    wins = sc.native_windows(120 * NS, 130 * NS)
    assert any(a is None or a <= 20 * NS for a, _ in wins) and any(
        b is not None and b >= 30 * NS for _, b in wins
    )
    wins = sc.native_windows(255 * NS, 260 * NS)
    assert all(a is not None and a >= 50 * NS for a, _ in wins)


# -- automatic alignment ---------------------------------------------------------------------------------


def _kernel_pair(tmp_path: Path, n: int = 60, suspend_at: int | None = None) -> tuple[Path, Path, list]:
    """dmesg (monotonic) and a logcat with the same lines from the kernel buffer (wall clock)."""
    events = _kernel_events(n)
    rnd = random.Random(7)
    dmesg, logcat, truth = [], [], []
    for i, (mono, tag, msg) in enumerate(events):
        wall = T0 + mono + (30 * NS if suspend_at is not None and i >= suspend_at else 0)
        dmesg.append(_dmesg_line(mono, tag, msg))
        logcat.append(_logcat_line(wall + rnd.randint(-500, 500) * 1000, tag, msg))
        truth.append(wall)
        if i % 5 == 0:
            logcat.append(_logcat_line(wall + 3 * MS, "CarService", f"user line {i}"))
    return _write(tmp_path / "dmesg.txt", dmesg), _write(tmp_path / "logcat.txt", logcat), truth


def test_same_lines_place_a_kernel_log_within_10ms(app: App, tools: Tools, tmp_path: Path) -> None:
    dmesg, logcat, truth = _kernel_pair(tmp_path)
    app.ingestor.add(app.config.case_db("k"), str(logcat))
    app.ingestor.add(app.config.case_db("k"), str(dmesg))
    times = _case_times(app, "k", 2)
    assert len(times) == len(truth)
    assert max(abs(times[i + 1] - t) for i, t in enumerate(truth)) < 10 * MS  # type: ignore[operator]
    out = tools.align("k").text
    assert "2  paired" in out and "same lines" in out, out
    with db.opened(app.config.case_db("k")) as conn:
        sc = ck.load(conn).get(2, 1)
    assert sc is not None and sc.conf == "paired" and sc.error_ns is not None and sc.error_ns < 10 * MS


def test_same_lines_across_a_suspend_give_two_pieces(app: App, tmp_path: Path) -> None:
    dmesg, logcat, truth = _kernel_pair(tmp_path, n=80, suspend_at=50)
    app.ingestor.add(app.config.case_db("s"), str(logcat))
    app.ingestor.add(app.config.case_db("s"), str(dmesg))
    with db.opened(app.config.case_db("s")) as conn:
        sc = ck.load(conn).get(2, 1)
    assert sc is not None and len(sc.pieces) == 2
    times = _case_times(app, "s", 2)
    assert max(abs(times[i + 1] - t) for i, t in enumerate(truth)) < 10 * MS  # type: ignore[operator]


def test_two_boots_are_not_paired_with_each_other(app: App, tmp_path: Path) -> None:
    events = _kernel_events(20)
    lines = ["[    0.000000] Booting Linux on physical CPU 0x0"] + [_dmesg_line(*e) for e in events]
    lines += ["[    0.000000] Booting Linux on physical CPU 0x0"] + [_dmesg_line(*e) for e in events]
    app.ingestor.add(app.config.case_db("b"), str(_write(tmp_path / "dmesg.txt", lines)))
    with db.opened(app.config.case_db("b")) as conn:
        cc = ck.load(conn)
    # The first boot is the reference (monotonic case clock); the second has no anchor.
    assert cc.kind == "mono"
    assert len(cc.segs) == 1


def test_utc_times_in_kernel_lines_beat_the_boot_estimate(app: App, tools: Tools, tmp_path: Path) -> None:
    text = (FIXTURES / "bugreport/bugreport-sdk_car-UAA1.250512.001-2026-01-01-00-10-00.txt").read_text()
    text = text.replace(
        "<6>[  124.000000] wlan0: associated with 3c:5a:b4:01:02:03\n",
        "<6>[  124.000000] wlan0: associated with 3c:5a:b4:01:02:03\n"
        "<6>[  200.000000] PM: suspend exit 2025-12-31 23:01:20.000000000 UTC\n",
    )
    path = _write(tmp_path / "bugreport-x.txt", text.splitlines())
    app.ingestor.add(app.config.case_db("u"), str(path))
    rows = app.list_sources("u") or []
    kernel = next(s for s in rows if s.stream == "kernel")
    times = _case_times(app, "u", kernel.id)
    # Europe/Berlin is UTC+1 in winter: 200 s since boot = 2026-01-01 00:01:20 on the device clock.
    assert times[4] == wall_ns(2026, 1, 1, 0, 1, 20)
    assert times[2] == wall_ns(2026, 1, 1, 0, 0, 3) + 456789000
    out = tools.align("u").text
    assert "paired" in out and "UTC" in out, out


def test_bugreport_kernel_log_gets_the_boot_estimate(app: App, tools: Tools, tmp_path: Path) -> None:
    src = FIXTURES / "bugreport/bugreport-sdk_car-UAA1.250512.001-2026-01-01-00-10-00.txt"
    path = _write(tmp_path / src.name, src.read_text().splitlines())
    app.ingestor.add(app.config.case_db("e"), str(path))
    out = tools.align("e").text
    assert "estimate" in out and "boot at 2025-12-31 23:58:00.000" in out and "±60 s" in out, out
    tl = tools.timeline("e").text
    assert "~01-01 00:00:03.456" in tl, tl  # the kernel line, between the crash and the ANR


def _markers(tmp_path: Path) -> Path:
    d = tmp_path / "rules"
    d.mkdir()
    (d / "vhal.yaml").write_text(
        "marker: vhal_set\n"
        "a: {match: 'setProperty prop=(?P<prop>0x[0-9a-f]+) seq=(?P<seq>\\d+)', tags: [VehicleHal]}\n"
        "b: {match: 'SET prop=(?P<prop>0x[0-9a-f]+) seq=(?P<seq>\\d+)', sources: ['*mcu*']}\n"
        "delay: 2ms\n",
        encoding="utf-8",
    )
    return d


def test_marker_rules_align_another_device(config: Config, tmp_path: Path) -> None:
    app = _app_with_rules(config, _markers(tmp_path))
    tools = Tools(app)
    hu, mcu, truth = [], [], []
    offset, ppm = 3200 * MS, 40.0
    for i in range(40):
        t = T0 + i * 30 * NS
        hu.append(_logcat_line(t, "VehicleHal", f"setProperty prop=0x11400400 seq={i}", pid=900))
        hu.append(_logcat_line(t + 5 * MS, "CarService", f"applied {i}", pid=901))
        tb = t + 2 * MS + offset + round(ppm * 1e-6 * (t - T0))
        mcu.append(_logcat_line(tb, "MCU", f"SET prop=0x11400400 seq={i}", pid=1))
        truth.append(t + 2 * MS)
    app.ingestor.add(app.config.case_db("m"), str(_write(tmp_path / "headunit.log", hu)))
    app.ingestor.add(app.config.case_db("m"), str(_write(tmp_path / "mcu.log", mcu)))
    times = _case_times(app, "m", 2)
    assert max(abs(times[i + 1] - t) for i, t in enumerate(truth)) < 10 * MS  # type: ignore[operator]
    out = tools.align("m").text
    assert "marker rule 'vhal_set'" in out and "drift" in out, out


def test_unrelated_wall_clock_is_assumed_as_written(app: App, tools: Tools, tmp_path: Path) -> None:
    a = [_logcat_line(T0 + i * NS, "A", f"first device line {WORDS[i]}") for i in range(10)]
    b = [_logcat_line(T0 + i * NS + 500 * MS, "B", f"second device {WORDS[i]}") for i in range(5)]
    app.ingestor.add(app.config.case_db("w"), str(_write(tmp_path / "a.log", a)))
    app.ingestor.add(app.config.case_db("w"), str(_write(tmp_path / "b.log", b)))
    out = tools.align("w").text
    assert "2  assumed  +0 ns  error unknown" in out, out
    tl = tools.timeline("w").text
    assert "log:2#1  ~10-01 08:00:00.500" in tl, tl


# -- manual alignment -----------------------------------------------------------------------------------


def _two_devices(app: App, tmp_path: Path) -> None:
    a = [_logcat_line(T0 + i * NS, "A", f"first device line {WORDS[i]}") for i in range(10)]
    b = [_logcat_line(T0 + i * NS, "B", f"second device {WORDS[i]}") for i in range(5)]
    app.ingestor.add(app.config.case_db("x"), str(_write(tmp_path / "a.log", a)))
    app.ingestor.add(app.config.case_db("x"), str(_write(tmp_path / "b.log", b)))


def test_manual_offset_anchor_reference_and_reset(app: App, tools: Tools, tmp_path: Path) -> None:
    _two_devices(app, tmp_path)
    out = tools.align("x", "offset", source="2", offset="-250ms").text
    assert "2  manual  -250 ms" in out, out
    assert _case_times(app, "x", 2)[1] == T0 - 250 * MS
    # An anchor replaces nothing but ranks like the offset; the later entry of the same kind wins
    # only by error, so reset first to see the anchor alone.
    tools.align("x", "reset")
    out = tools.align("x", "anchor", at="log:2#3", ref="log:1#5").text
    assert "2  manual  +2 s" in out and "1 anchor(s)" in out, out
    assert _case_times(app, "x", 2)[3] == T0 + 4 * NS
    out = tools.align("x", "reference", source="2").text
    assert "case clock: wall clock of source 2" in out and "1  manual  -2 s" in out, out
    tools.align("x", "reset")
    assert "1 manual entr" not in tools.align("x").text


def test_manual_errors(app: App, tools: Tools, tmp_path: Path) -> None:
    _two_devices(app, tmp_path)
    with pytest.raises(ToolInputError, match="reference clock"):
        tools.align("x", "offset", source="1", offset="1s")
    with pytest.raises(ToolInputError, match="same clock"):
        tools.align("x", "anchor", at="log:2#1", ref="log:2#2")
    with pytest.raises(ToolInputError, match="duration"):
        tools.align("x", "offset", source="2", offset="soon")
    with pytest.raises(ToolInputError, match="unknown source"):
        tools.align("x", "reference", source="9")


def test_offset_of_a_kernel_log_as_boot_time(app: App, tools: Tools, tmp_path: Path) -> None:
    a = [_logcat_line(T0 + i * NS, "A", f"line {WORDS[i]}") for i in range(10)]
    k = [_dmesg_line(e[0], e[1], e[2]) for e in _kernel_events(5)]
    app.ingestor.add(app.config.case_db("o"), str(_write(tmp_path / "a.log", a)))
    app.ingestor.add(app.config.case_db("o"), str(_write(tmp_path / "dmesg.txt", k)))
    assert "2  not placed" in tools.align("o").text
    out = tools.align("o", "offset", source="2", offset="2026-10-01 08:00:00.000").text
    assert "boot at 2026-10-01 08:00:00.000" in out, out


# -- timeline and context ----------------------------------------------------------------------------------


def test_timeline_merges_and_pages_in_case_time(app: App, tools: Tools, tmp_path: Path) -> None:
    dmesg, logcat, _ = _kernel_pair(tmp_path, n=40)
    app.ingestor.add(app.config.case_db("t"), str(logcat))
    app.ingestor.add(app.config.case_db("t"), str(dmesg))
    seen: list[tuple[str, str]] = []
    cursor = None
    for _ in range(20):
        out = tools.timeline("t", limit=17, cursor=cursor)
        seen += [(r.ts or "", r.cite) for r in out.data.records]  # type: ignore[attr-defined]
        cursor = out.data.next_cursor  # type: ignore[attr-defined]
        if cursor is None:
            break
    assert cursor is None
    total = sum(s.records for s in app.list_sources("t") or [])
    assert len(seen) == total == len({c for _, c in seen})
    assert [t for t, _ in seen] == sorted(t for t, _ in seen)
    out = tools.timeline("t", Filter.model_validate({"sources": ["2"], "from": "log:2#10", "to": "+2s"}))
    recs = out.data.records  # type: ignore[attr-defined]
    assert recs and recs[0].cite == "log:2#10" and all(r.approx for r in recs)
    assert "~" in out.text and "~ source 2: paired" in out.text
    out = tools.timeline("t", query_text="user line")
    assert out.data.records and all("user line" in r.msg for r in out.data.records)  # type: ignore[attr-defined]


def test_timeline_from_to_errors(app: App, tools: Tools, tmp_path: Path) -> None:
    _two_devices(app, tmp_path)
    with pytest.raises(ToolInputError, match="case clock uses wall-clock"):
        tools.timeline("x", Filter.model_validate({"from": "12.5"}))
    with pytest.raises(ToolInputError, match="cursor"):
        tools.timeline("x", cursor="nonsense")


def test_context_all_shows_other_sources_around_an_event(app: App, tools: Tools, tmp_path: Path) -> None:
    dmesg, logcat, _ = _kernel_pair(tmp_path, n=40)
    app.ingestor.add(app.config.case_db("c"), str(logcat))
    app.ingestor.add(app.config.case_db("c"), str(dmesg))
    out = tools.context("c", "log:2#20", 4, 4, "all")
    lines = out.text.splitlines()
    assert lines[0].startswith("all sources around log:2#20 on the case clock")
    marked = [ln for ln in lines if ln.startswith("> ")]
    assert marked == [ln for ln in lines[1:] if "log:2#20 " in ln]
    cites = {ln.split()[0] if not ln.startswith(">") else ln.split()[1] for ln in lines[1:10]}
    assert any(c.startswith("log:1#") for c in cites) and any(c.startswith("log:2#") for c in cites)
    assert len(out.data.records) == 9  # type: ignore[attr-defined]


def test_context_all_refuses_an_unplaced_record(app: App, tools: Tools, tmp_path: Path) -> None:
    a = [_logcat_line(T0 + i * NS, "A", f"line {WORDS[i]}") for i in range(10)]
    k = [_dmesg_line(e[0], e[1], e[2]) for e in _kernel_events(5)]
    app.ingestor.add(app.config.case_db("p"), str(_write(tmp_path / "a.log", a)))
    app.ingestor.add(app.config.case_db("p"), str(_write(tmp_path / "dmesg.txt", k)))
    with pytest.raises(ToolInputError, match="not on the case clock"):
        tools.context("p", "log:2#1", scope="all")
    assert "not on the case clock (no anchor): sources 2" in tools.timeline("p").text


def test_alignment_is_recomputed_when_sources_change(app: App, tools: Tools, tmp_path: Path) -> None:
    _two_devices(app, tmp_path)
    with db.opened(app.config.case_db("x")) as conn:
        sig = ck.stored_sig(conn)
        db.delete_sources(conn, [2])
    tools.align("x")
    with db.opened(app.config.case_db("x")) as conn:
        assert ck.stored_sig(conn) != sig
        assert {sid for sid, _ in ck.load(conn).segs} == {1}


def test_timeline_and_align_over_mcp(config: Config, tmp_path: Path) -> None:
    import anyio
    from mcp import Client

    from automotive_log_mcp.server import build_server

    src = FIXTURES / "bugreport/bugreport-sdk_car-UAA1.250512.001-2026-01-01-00-10-00.txt"
    path = _write(tmp_path / src.name, src.read_text().splitlines())
    dmesg = _write(tmp_path / "dmesg.txt", [_dmesg_line(*e) for e in _kernel_events(5)])

    async def main() -> None:
        async with Client(build_server(config, Policy())) as client:
            for p in (path, dmesg):
                r = await client.call_tool("add_source", {"case": "mc", "path": str(p)})
                assert not r.is_error, r
            r = await client.call_tool("list_sources", {"case": "mc"})
            text = r.content[0].text  # type: ignore[union-attr]
            assert "clock ~estimate ±60 s" in text and "clock not placed" in text, text
            r = await client.call_tool("timeline", {"case": "mc", "filter": {"level": "E", "from": "art:1"}})
            text = r.content[0].text  # type: ignore[union-attr]
            assert (
                not r.is_error and "log:3#1  01-01 00:00:01.000" in text and "~01-01 00:00:03.456" in text
            ), text
            r = await client.call_tool(
                "align", {"case": "mc", "mode": "anchor", "at": "log:8#1", "ref": "log:5#1"}
            )
            assert not r.is_error and "8  estimate" in r.content[0].text  # type: ignore[union-attr]
            r = await client.call_tool(
                "context", {"case": "mc", "at": "log:8#1", "scope": "all", "before": 2}
            )
            assert not r.is_error and "> log:8#1" in r.content[0].text  # type: ignore[union-attr]
            r = await client.call_tool(
                "align", {"case": "mc", "mode": "offset", "source": "1", "offset": "1s"}
            )
            assert r.is_error

    anyio.run(main)
