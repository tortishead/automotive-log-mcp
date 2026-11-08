"""JSONL call log: one line per tool call, without any log content.

Each line records when, which tool, the (redacted) arguments, the outcome, the duration and the
number of output bytes. Tool output itself is never written. Arguments pass through a redactor
before they are written (D5); until the redaction rules land in M1 the default is the identity.
"""

from __future__ import annotations

import json
import threading
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


class CallLog:
    def __init__(self, path: Path | None, session: str, redact: Redactor = _no_redaction) -> None:
        self.path = path
        self.session = session
        self._redact = redact
        self._lock = threading.Lock()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        tool: str,
        args: dict[str, Any],
        status: Status,
        duration_ms: float,
        bytes_out: int,
        error: str | None = None,
    ) -> None:
        if self.path is None:
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
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        # Open per write: keeps the file closed between calls, which matters on Windows.
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line)
