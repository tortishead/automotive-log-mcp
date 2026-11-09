"""Tool output: timestamps that round-trip, message truncation, line and byte caps, cursors.

Every tool builds its text with :class:`Page`, which stops adding lines at the line or byte cap
and leaves room for a footer that says how much more there is and how to narrow. Timestamps are
printed in two forms, both accepted back as ``from``/``to``:

- short, in text lines: ``MM-DD hh:mm:ss.mmm`` (the year is taken from the case's time range);
- full, in structured content and source ranges: ``YYYY-MM-DD hh:mm:ss.mmm``.

Records with only a monotonic clock (kernel, ``logcat -v monotonic``) print seconds since boot,
``123.456789``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .redact import TOKEN_RE
from .timeparse import NS, fmt_wall

ELLIPSIS = "…"
NEWLINE_MARK = " ⏎ "


def short_ts(ts: int | None, ts_src: int | None = None) -> str:
    """``MM-DD hh:mm:ss.mmm`` for a wall-clock time, ``123.456789`` for a monotonic one, else ``-``."""
    if ts is not None:
        return fmt_wall(ts)[5:]
    if ts_src is not None:
        return mono_ts(ts_src)
    return "-"


def full_ts(ts: int | None, ts_src: int | None = None) -> str | None:
    """``YYYY-MM-DD hh:mm:ss.mmm`` for a wall-clock time, ``123.456789`` for a monotonic one."""
    if ts is not None:
        return fmt_wall(ts)
    if ts_src is not None:
        return mono_ts(ts_src)
    return None


def mono_ts(ns: int) -> str:
    secs, frac = divmod(ns, NS)
    return f"{secs}.{frac // 1000:06d}"


def one_line(text: str) -> str:
    return text.replace("\r", "").replace("\n", NEWLINE_MARK)


def clip(text: str, limit: int) -> tuple[str, bool]:
    """``text`` on one line, cut at ``limit`` characters with ``…``; and whether it was cut."""
    text = one_line(text)
    if len(text) <= limit:
        return text, False
    return text[: max(limit - 1, 0)] + ELLIPSIS, True


def plural(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


_TEMPLATE_SPLIT = re.compile(r"<\*>|<[A-Z][A-Z0-9_]*>")


def template_literal(template: str | None, limit: int = 120) -> str | None:
    """The longest constant part of a template, for a code search (``"setProperty failed, status="``).

    Only the first line counts (later lines of a merged record are stack frames). Parts shorter
    than 4 characters are not worth a search; None then.
    """
    if not template:
        return None
    first = template.split("\n", 1)[0]
    first = TOKEN_RE.sub("<*>", first)
    parts = [p.strip().lstrip(")]}>,;:.").strip() for p in _TEMPLATE_SPLIT.split(first)]
    best = max(parts, key=len, default="")
    if len(best) < 4:
        return None
    return best if len(best) <= limit else best[:limit]


@dataclass
class Page:
    """Accumulates output lines under a line cap and a byte cap.

    ``reserve`` bytes stay free for the footer. :meth:`add` returns False (and adds nothing) once
    a cap is reached; ``full`` then tells callers to stop.
    """

    max_lines: int
    max_bytes: int
    reserve: int = 600
    lines: list[str] = field(default_factory=list)
    size: int = 0
    full: bool = False
    capped_by: str | None = None  # 'lines' | 'bytes'
    counted: int = 0  # lines that count against max_lines

    def add(self, line: str, count_line: bool = True) -> bool:
        if self.full:
            return False
        n = utf8_len(line) + 1
        if count_line and self.counted >= self.max_lines:
            self.full, self.capped_by = True, "lines"
            return False
        if self.size + n > self.max_bytes - self.reserve:
            self.full, self.capped_by = True, "bytes"
            return False
        self.lines.append(line)
        self.counted += 1 if count_line else 0
        self.size += n
        return True

    def pop(self) -> None:
        line = self.lines.pop()
        self.size -= utf8_len(line) + 1
        self.counted -= 1

    def force(self, line: str) -> None:
        """Add a line regardless of caps (headers and footers, which ``reserve`` makes room for)."""
        self.lines.append(line)
        self.size += utf8_len(line) + 1

    def text(self) -> str:
        return "\n".join(self.lines)


def cap_note(page: Page, what: str = "lines") -> str:
    if page.capped_by == "bytes":
        return f"output cap of {page.max_bytes} bytes reached"
    return f"output cap of {page.max_lines} {what} reached"


# -- cursors --------------------------------------------------------------------------------------


class CursorError(ValueError):
    pass


def query_key(params: dict[str, Any]) -> str:
    text = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:10]


def make_cursor(offset: int, key: str) -> str:
    raw = json.dumps({"o": offset, "k": key}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def read_cursor(cursor: str, key: str) -> int:
    try:
        pad = "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(cursor + pad))
        offset, ckey = data["o"], data["k"]
    except (ValueError, KeyError, TypeError):
        raise CursorError(
            "invalid cursor; pass the cursor string exactly as a previous result printed it"
        ) from None
    if not isinstance(offset, int) or offset < 0:
        raise CursorError("invalid cursor")
    if ckey != key:
        raise CursorError(
            "this cursor belongs to a different query; repeat the same query, filter and options "
            "with the cursor, or drop the cursor to start from the first page"
        )
    return offset
