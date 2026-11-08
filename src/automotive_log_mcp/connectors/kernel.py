"""Linux kernel log: ``dmesg`` and ``/dev/kmsg`` text.

Variants: ``[    1.234567] msg``, with a printk priority ``<6>[    1.234567] msg`` (``dmesg -r``,
Android bugreports), with a caller id ``[    1.234567][  T123] msg``, facility/level prefixes
(``dmesg -x``: ``kern  :info  : [ ... ] msg``), human times (``dmesg -T``:
``[Sat Oct 11 08:12:03 2026] msg``) and ``/dev/kmsg`` records (``6,1234,5678901,-;msg``, with
indented ``KEY=value`` continuation lines kept in ``attrs``).

Timestamps are monotonic seconds since boot (``ts_src``); ``ts`` is filled only when the
container knows the boot time, otherwise it is null and the clock model (M5) maps it later.
``Booting Linux`` / ``Linux version`` lines are boot markers.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from ..core.levels import from_kernel_priority, normalize
from ..core.timeparse import NS, frac_ns, wall_ns
from ..model import RawRecord
from .base import ParseItem, SourceContext, TextConnector, head_lines

DMESG = re.compile(r"(?:<(\d{1,3})>)?(?:(\w+)\s*:(\w+)\s*: )?\[\s*(\d+)\.(\d+)\](?:\s*\[\s*([TC])(\d+)\])? ?")
HUMAN = re.compile(r"(?:<(\d{1,3})>)?\[(\w{3}) (\w{3}) +(\d{1,2}) (\d\d):(\d\d):(\d\d) (\d{4})\] ?")
KMSG = re.compile(r"(\d{1,3}),(\d+),(\d+),[^;]*;")
TAG = re.compile(r"([A-Za-z][\w.\-]{0,31}): ")
BOOT = ("Booting Linux", "Linux version ")
MONTHS = {
    m: i
    for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1
    )
}


def _tag(msg: str) -> str | None:
    m = TAG.match(msg)
    return m.group(1) if m else None


class KernelConnector(TextConnector):
    name = "kernel"
    version = "1"

    def sniff(self, head: bytes, filename: str) -> float:
        lines = [ln for ln in head_lines(head, 60) if ln.strip()]
        if not lines:
            return 0.0
        hits = sum(1 for ln in lines if DMESG.match(ln) or HUMAN.match(ln) or KMSG.match(ln))
        if hits == 0:
            return 0.0
        score = 0.3 + 0.65 * hits / len(lines)
        name = filename.lower()
        if any(k in name for k in ("dmesg", "kmsg", "kernel")):
            score += 0.05
        return min(score, 0.95)

    def parse_lines(self, lines: Iterator[str], ctx: SourceContext) -> Iterator[ParseItem]:
        boot_ns: int | None = ctx.meta.get("boot_time_ns")
        pending: RawRecord | None = None
        for line in lines:
            m = DMESG.match(line)
            if m is not None:
                prio, _fac, lvl_name, sec, frac, caller_kind, caller = m.groups()
                msg = line[m.end() :]
                mono = int(sec) * NS + frac_ns(frac)
                level = from_kernel_priority(int(prio)) if prio else normalize(lvl_name)
                if pending is not None:
                    yield pending
                if len(frac) == 6 and (sec == "0" or sec[0] != "0"):
                    ts_raw = None  # reproducible from ts_src (ts_format sec_us)
                    ctx.meta.setdefault("ts_format", "sec_us")
                else:
                    ts_raw = f"{sec}.{frac}"
                pending = RawRecord(
                    msg=msg,
                    ts_raw=ts_raw,
                    ts_src=mono,
                    ts=boot_ns + mono if boot_ns is not None else None,
                    level=level,
                    tag=_tag(msg),
                    pid=int(caller) if caller_kind == "T" else None,
                    attrs={"cpu": int(caller)} if caller_kind == "C" else None,
                    monotonic=True,
                    boot=msg.startswith(BOOT),
                )
                continue
            m = KMSG.match(line)
            if m is not None:
                prio, kseq, usec = m.groups()
                msg = line[m.end() :]
                mono = int(usec) * 1000
                if pending is not None:
                    yield pending
                pending = RawRecord(
                    msg=msg,
                    ts_raw=f"{int(usec) // 1_000_000}.{int(usec) % 1_000_000:06d}",
                    ts_src=mono,
                    ts=boot_ns + mono if boot_ns is not None else None,
                    level=from_kernel_priority(int(prio)),
                    tag=_tag(msg),
                    attrs={"kseq": int(kseq)},
                    monotonic=True,
                    boot=msg.startswith(BOOT),
                )
                continue
            if (
                pending is not None
                and pending.attrs is not None
                and "kseq" in pending.attrs
                and line[:1] == " "
            ):
                key, _, value = line.strip().partition("=")
                pending.attrs[key] = value
                continue
            m = HUMAN.match(line)
            if m is not None:
                prio, _wd, mon, day, hh, mi, ss, year = m.groups()
                msg = line[m.end() :]
                ts = wall_ns(int(year), MONTHS.get(mon, 1), int(day), int(hh), int(mi), int(ss))
                if pending is not None:
                    yield pending
                pending = RawRecord(
                    msg=msg,
                    ts_raw=f"{_wd} {mon} {day} {hh}:{mi}:{ss} {year}",
                    ts=ts,
                    ts_src=ts,
                    level=from_kernel_priority(int(prio)) if prio else None,
                    tag=_tag(msg),
                    boot=msg.startswith(BOOT),
                )
                continue
            if pending is not None:
                yield pending
                pending = None
            yield RawRecord(msg=line, unparsed=True)
        if pending is not None:
            yield pending
