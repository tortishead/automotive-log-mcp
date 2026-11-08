"""Message templates by regex masking (D4).

Variable parts (UUIDs, hex numbers, paths, decimal numbers) become ``<*>``; redaction tokens
``<KIND:xxxxxxxx>`` become ``<KIND>``. Messages that differ only in such parts share a template,
which is what collapse and counting use. Masking is idempotent: a template masks to itself.

Templates are computed from the redacted message, so no raw value reaches the templates table.
"""

from __future__ import annotations

import re

from .redact import TOKEN_RE

WILDCARD = "<*>"

_HEX_NUM_RE = re.compile(
    r"\b(?:0[xX][0-9a-fA-F]+"  # 0x-prefixed hex
    r"|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"  # UUID
    r"|[0-9a-fA-F]{8,})\b"  # long bare hex ids (and long numbers)
    r"|(?<![A-Za-z_])-?\d+(?:\.\d+)*"  # decimal numbers, not glued to a preceding word
)
_PATH_RE = re.compile(r"(?<![\w/])(?:/[\w.@+-]+){2,}/?")  # absolute paths, two components or more


def mask(msg: str) -> str:
    """Return the template text of a (redacted) message."""
    if "<" in msg:
        msg = TOKEN_RE.sub(r"<\1>", msg)
    if "/" in msg:
        msg = _PATH_RE.sub(WILDCARD, msg)
    return _HEX_NUM_RE.sub(WILDCARD, msg)
