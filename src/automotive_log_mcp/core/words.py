"""Words inside CamelCase identifiers, for the ``parts`` column of the full-text index.

FTS5's ``unicode61`` tokenizer splits on punctuation (``_``, ``.``, ``$``, ...) but keeps a
CamelCase identifier as one word: ``EsoCarDelegateDisplayAreaPolicyProvider`` is found by its
full name or ``eso*`` only. :func:`camel_parts` gives, for each such identifier in a text, its
parts (``eso car delegate display area policy provider``), stored lowercased in the ``parts``
column. So ``Delegate`` or ``eso`` finds the record, and :func:`query_alternative` turns a
CamelCase query word into ``(DisplayArea OR "display area")`` (adjacent parts), so a fragment
of an identifier finds it too.
"""

from __future__ import annotations

import re

# A word with a lower->upper step or an acronym followed by a word (HTTPServer).
_CAMEL = re.compile(r"[a-z][A-Z]|[A-Z]{2}[a-z]")  # cheap test first: most texts have none
_CAMEL_WORD = re.compile(r"(?<![A-Za-z0-9])(?=[A-Za-z0-9]*?(?:[a-z][A-Z]|[A-Z]{2}[a-z]))[A-Za-z0-9]+")
_PART = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
MAX_PARTS = 12  # longer identifiers: only the first MAX_PARTS parts
_CACHE_MAX = 50_000

_cache: dict[str, str] = {}


def _word_parts(word: str) -> str:
    got = _cache.get(word)
    if got is not None:
        return got
    parts = _PART.findall(word)[:MAX_PARTS]
    got = " ".join(parts).lower() if len(parts) >= 2 else ""
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[word] = got
    return got


def camel_parts(*texts: str | None) -> str:
    """Parts and suffixes of the CamelCase words in ``texts`` ('' when there are none)."""
    words: list[str] = []
    for text in texts:
        if text and _CAMEL.search(text) is not None:
            words.extend(_CAMEL_WORD.findall(text))
    if not words:
        return ""
    if len(words) == 1:
        return _word_parts(words[0])
    return " ".join(p for p in (_word_parts(w) for w in dict.fromkeys(words)) if p)


def query_alternative(token: str) -> str | None:
    """``(Token OR "to ken")`` for a CamelCase query word (``Token*``: prefix on both); else None."""
    star = token.endswith("*")
    body = token[:-1] if star else token
    if not body.isascii() or not body.isalnum() or _CAMEL.search(body) is None:
        return None
    parts = _word_parts(body)
    if " " not in parts:
        return None
    s = "*" if star else ""
    return f'({body}{s} OR "{parts}"{s})'
