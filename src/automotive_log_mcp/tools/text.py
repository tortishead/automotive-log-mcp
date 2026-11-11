"""Small text helpers shared by the tools: joining, cutting, ranges, durations, bars."""

from __future__ import annotations

import re
import sqlite3

from ..core import format as fmt
from ..core.timeparse import NS
from ..model import SEQ_BITS, SEQ_MASK, citation
from ..store import query
from ..store.query import Rec
from .models import RecordOut

_REGEX_HINT = re.compile(
    r"\\[dDwWsSbB.]|\.\*|\.\+|\[[^\]\s]+\]|\(\?|\w\|\w|^\^|\$$|\{\d+(,\d*)?\}|\w\+\s|\)\?"
)


def looks_like_regex(text: str) -> bool:
    return bool(_REGEX_HINT.search(text))


def list_some(items: list[str], n: int = 10) -> str:
    return ", ".join(items[:n]) + (f", … {len(items) - n} more" if len(items) > n else "")


def reserve_bytes(max_bytes: int, want: int) -> int:
    """Bytes kept free for headers and footers: ``want``, but at most a quarter of the cap."""
    return min(want, max_bytes // 4)


def dash(v: int | None) -> str:
    return "-" if v is None else str(v)


def sources_word(ids: list[int]) -> str:
    return f"source {ids[0]} have" if len(ids) == 1 else f"sources {id_ranges(ids)} have"


def id_ranges(ids: list[int]) -> str:
    ids = sorted(set(ids))
    out: list[str] = []
    i = 0
    while i < len(ids):
        j = i
        while j + 1 < len(ids) and ids[j + 1] == ids[j] + 1:
            j += 1
        out.append(str(ids[i]) if i == j else f"{ids[i]}-{ids[j]}")
        i = j + 1
    return ", ".join(out)


def level_order(item: tuple[str, int]) -> int:
    return "FEWIDV-".index(item[0]) if item[0] in "FEWIDV-" else 9


def fts_error(q: str, e: sqlite3.OperationalError) -> str:
    return (
        f'invalid text query {q!r} ({e}). Text queries are words (all must occur), "quoted phrases", '
        "'prefix*', and the upper-case operators AND, OR, NOT, NEAR(); use mode='regex' for patterns"
    )


def scan_note(scan: query.ScanResult, cap: int) -> str:
    where = ""
    if scan.last_scanned is not None:
        where = f" (stopped at {citation(scan.last_scanned >> SEQ_BITS, scan.last_scanned & SEQ_MASK)})"
    return (
        f"regex scan stopped after {cap:,} candidate records{where}: counts are lower bounds; narrow with "
        "filter sources, from/to, level or tags, or add literal text to the regex"
    )


def literal_lines(hits: list[RecordOut], n: int = 5) -> list[str]:
    seen: set[str] = set()
    picked: list[tuple[int, RecordOut]] = []
    for i, h in enumerate(hits):
        if h.literal and h.literal not in seen:
            seen.add(h.literal)
            picked.append((i, h))
    if not picked:
        return []
    picked.sort(key=lambda x: ("FEWIDV".find(x[1].level) if x[1].level else 6, x[0]))
    out = ["code search (constant text of the templates):"]
    for _, h in picked[:n]:
        out.append(f'  "{fmt.clip(h.literal or "", 100)[0]}"  {h.cite}')
    return out


def warn_lines(warnings: list[str]) -> list[str]:
    return [f"warning: {w}" for w in warnings]


def join_lines(lines: list[str]) -> str:
    return "\n".join(lines)


def cut_bytes(text: str, n: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= n:
        return text
    return data[:n].decode("utf-8", errors="ignore") + fmt.ELLIPSIS


_STEPS = [
    *(n * NS // 1000 for n in (1, 2, 5, 10, 20, 50, 100, 200, 500)),
    *(n * NS for n in (1, 2, 5, 10, 15, 30)),
    *(n * 60 * NS for n in (1, 2, 5, 10, 15, 30)),
    *(n * 3600 * NS for n in (1, 2, 3, 6, 12, 24)),
]


def nice_step(min_ns: int) -> int:
    """The smallest round bucket size of at least ``min_ns`` (1 ms .. 1 d, then whole days)."""
    for s in _STEPS:
        if s >= min_ns:
            return s
    day = 86400 * NS
    return -(-min_ns // day) * day


def nice_step_text(min_ns: int) -> str:
    s = nice_step(min_ns)
    for unit, ns in (("d", 86400 * NS), ("h", 3600 * NS), ("m", 60 * NS), ("s", NS), ("ms", NS // 1000)):
        if s % ns == 0:
            return f"{s // ns}{unit}"
    return f"{s // (NS // 1000)}ms"


def dur_text(ns: int) -> str:
    secs = ns / NS
    if secs < 1:
        return f"{secs * 1000:.0f} ms"
    if secs < 120:
        return f"{secs:.1f} s".replace(".0 s", " s")
    if secs < 7200:
        return f"{secs / 60:.1f} min".replace(".0 min", " min")
    if secs < 2 * 86400:
        return f"{secs / 3600:.1f} h".replace(".0 h", " h")
    return f"{secs / 86400:.1f} d".replace(".0 d", " d")


def delta_text(a: Rec, b: Rec) -> str:
    if a.ts is not None and b.ts is not None:
        return f" (+{dur_text(b.ts - a.ts)})"
    return ""


_SPARK = "▁▂▃▄▅▆▇█"


def spark(n: int, peak: int) -> str:
    if n <= 0 or peak <= 0:
        return "·"
    return _SPARK[min(len(_SPARK) - 1, (n * len(_SPARK) - 1) // peak)]


def bar_text(n: int, peak: int, width: int) -> str:
    if n <= 0 or peak <= 0:
        return ""
    return "█" * max(1, round(n * width / peak))


def cite_id(rid: int | None) -> str | None:
    return citation(rid >> SEQ_BITS, rid & SEQ_MASK) if rid else None


def tail_text(name: str, n: int) -> str:
    """The distinguishing end of a source name (after the archive path), cut from the left."""
    name = name.rsplit("!", 1)[-1]
    return name if len(name) <= n else fmt.ELLIPSIS + name[-(n - 1) :]
