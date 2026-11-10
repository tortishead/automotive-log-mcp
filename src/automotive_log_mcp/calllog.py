"""JSONL call log: one line per tool call, without any log content.

Each line records when, which tool, the (redacted) arguments, the outcome, the duration and the
number of output bytes. Tool output itself is never written. Optional fields (added after M4, all
derived from sizes, counts and citation ids, never from log text) explain token use; see
``usage.py`` and the ``calls`` CLI command:

- ``seq``: call number within the server session (1, 2, …);
- ``text_bytes``, ``structured_bytes``: the text content and the structured content (compact
  JSON, 0 when the server sends none) of the result; ``tokens_est``: about bytes/4 of what the
  model is likely to read (the structured content if present, as Claude Code sends it, else the
  text);
- ``args_hash``, ``near_hash``: hashes of the normalized arguments, the second without paging and
  size arguments (``limit``, ``cursor``, ``before``, ``after``, ``examples``), to spot repeated and
  near-duplicate calls;
- ``capped``: which output caps were hit (``lines``, ``bytes``, ``limit``); ``results``,
  ``total``, ``exact``: items returned, matches in all, and whether counts are exact;
- ``cites``: citation ids in the output, compacted to ranges (``log:3#1-5``, ``art:4``), and for
  ``check_citations`` ``answer_cites`` (the ids the answer cites) and ``ok``;
- ``round``: the round number within the session; ``server`` (first call of each round): version,
  whether structured output is on, and the fixed bytes the server adds to every model request.

Rounds: a round starts with the server and again after a pause of ``round_gap`` seconds without a
call (an MCP server outlives a chat in VS Code, so a pause is the boundary the server can see).
Each round is also written to a file of its own in ``rounds_dir``,
``<date>_<time>_<session>_r<n>.jsonl``, the same lines as the call log, plus a short report
``…_r<n>.txt`` rewritten after every call. Hand the ``.jsonl`` over for analysis
(``automotive-log-mcp calls --log <file>``).

Arguments pass through a redactor before they are written (D5).
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

Redactor = Callable[[str], str]
Status = Literal["ok", "error", "refused"]


def _no_redaction(s: str) -> str:
    return s


def _redact_value(value: Any, redact: Redactor) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_value(v, redact) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact_value(v, redact) for v in value]
    return value


Reporter = Callable[[Path], str]


class CallLog:
    def __init__(
        self,
        path: Path | None,
        session: str,
        redact: Redactor = _no_redaction,
        rounds_dir: Path | None = None,
        round_gap: float = 600.0,
        reporter: Reporter | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = path
        self.session = session
        self._redact = redact
        self._lock = threading.Lock()
        self.rounds_dir = rounds_dir
        self.round_gap = round_gap
        self.reporter = reporter  # the report text for a round file; errors never fail a call
        self._clock = clock
        self._last: float | None = None
        self.round = 0
        self.round_file: Path | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def _next_round(self, now: float) -> bool:
        """Whether this call starts a new round (first call, or after a pause)."""
        new = self._last is None or now - self._last >= self.round_gap
        self._last = now
        if new:
            self.round += 1
            self.round_file = None
            if self.rounds_dir is not None:
                stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")  # local time; no ':' (Windows)
                self.round_file = self.rounds_dir / f"{stamp}_{self.session}_r{self.round}.jsonl"
        return new

    def _write_round(self, line: str) -> None:
        f = self.round_file
        if f is None:
            return
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            with f.open("a", encoding="utf-8") as out:
                out.write(line)
            if self.reporter is not None:
                text = self.reporter(f)
                tmp = f.with_suffix(".txt.tmp")
                tmp.write_text(text + "\n", encoding="utf-8")
                os.replace(tmp, f.with_suffix(".txt"))
        except Exception:
            pass

    def write(
        self,
        tool: str,
        args: dict[str, Any],
        status: Status,
        duration_ms: float,
        bytes_out: int,
        error: str | None = None,
        extra: dict[str, Any] | None = None,
        round_info: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        """``round_info``: the ``server`` field, written on the first call of each round."""
        if self.path is None and self.rounds_dir is None:
            return
        entry: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "session": self.session,
            "tool": tool,
            "args": _redact_value(args, self._redact),
            "status": status,
            "duration_ms": round(duration_ms, 1),
            "bytes_out": bytes_out,
        }
        if error is not None:
            entry["error"] = self._redact(error)
        if extra:
            entry.update(extra)  # sizes, counts, hashes and citation ids only (no log text)
        with self._lock:
            if self._next_round(self._clock()) and round_info is not None:
                entry["server"] = round_info()
            entry["round"] = self.round
            line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
            # Open per write: keeps the file closed between calls, which matters on Windows.
            if self.path is not None:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line)
            self._write_round(line)
