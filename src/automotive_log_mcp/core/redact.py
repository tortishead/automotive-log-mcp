"""Redaction at index time (D5).

Every message, tag and string attribute passes through :meth:`Redactor.redact` before it is
stored or templated, and so do call-log arguments. A matched value is replaced by a token
``<KIND:xxxxxxxx>`` whose suffix is a keyed hash (HMAC-SHA256 with a per-installation secret) of
the value: the same value gives the same token, so an agent can still correlate occurrences, but
the raw value cannot be recovered without the secret. There is no raw mode.

The secret is created on first use in ``<home>/secret.key``. Deleting it changes every token for
new imports.

Rules are deliberately conservative about what they leave alone: build fingerprints, package
names, versions, timestamps and hex addresses must not be touched (tests check this).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

TOKEN_RE = re.compile(r"<([A-Z][A-Z0-9_]*):([0-9a-f]{8})>")
RULES_VERSION = "1"


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _vin(s: str) -> str | None:
    letters = sum(c.isalpha() for c in s)
    return "VIN" if letters >= 2 and len(s) - letters >= 3 else None


def _ipv4_ok(s: str) -> bool:
    parts = s.split(".")
    if len(parts) != 4 or any(not p or len(p) > 3 or (len(p) > 1 and p[0] == "0") for p in parts):
        return False
    nums = [int(p) for p in parts]
    if any(n > 255 for n in nums):
        return False
    # Keep addresses that identify nothing: unspecified, loopback, broadcast.
    return not (nums == [0, 0, 0, 0] or nums[0] == 127 or nums == [255, 255, 255, 255])


def _mac(s: str) -> str | None:
    hexdigits = s.replace(":", "").replace("-", "").lower()
    # All-zero, broadcast and the Android placeholder 02:00:00:00:00:00 identify nothing.
    return None if hexdigits in ("000000000000", "020000000000", "ffffffffffff") else "MAC"


def _gps_pair_ok(s: str) -> bool:
    lat_s, lon_s = re.split(r",\s?", s, maxsplit=1)
    try:
        lat, lon = float(lat_s), float(lon_s)
    except ValueError:
        return False
    return abs(lat) <= 90 and abs(lon) <= 180 and not (lat == 0 and lon == 0)


def _number(s: str) -> str | None:
    """Digit runs: IMEI (15 digits, Luhn), IPv4 address, or a 'lat, lon' pair."""
    if "," in s:
        return "GPS" if _gps_pair_ok(s) else None
    if s.isdigit():
        return "IMEI" if len(s) == 15 and _luhn_ok(s) else None
    if s.count(".") == 3 and _ipv4_ok(s):
        return "IP"
    return None


def _const(kind: str) -> Callable[[str], str | None]:
    def classify(_: str) -> str | None:
        return kind

    return classify


@dataclass(frozen=True)
class Rule:
    """A redaction rule.

    ``pattern`` has one group ``v``: the value that is replaced (key-based rules keep the key).
    ``classify`` returns the token kind for a matched value, or None to keep it.
    ``guard`` is a cheap test on the line (and its lower-case form) that must pass before the
    regex runs; it is only a speed-up and must accept every line the pattern can match.
    """

    name: str
    pattern: str
    classify: Callable[[str], str | None]
    guard: Callable[[str, str], bool] | None = None
    flags: int = 0


# Earlier rules win where values could overlap.
DEFAULT_RULES: tuple[Rule, ...] = (
    Rule(
        "email",
        r"(?P<v>\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b)",
        _const("EMAIL"),
        lambda s, low: "@" in s,
    ),
    Rule(
        "serial",
        r"(?<!<)(?:\bserialno|\bserial(?:_?number)?|\bandroid_id)\]?\s*[:=]\s*\[?(?P<v>[A-Za-z0-9]{6,})",
        _const("SERIAL"),
        lambda s, low: "serial" in low or "android_id" in low,
        re.IGNORECASE,
    ),
    Rule(
        "ssid",
        r"(?<!<)\bssid\s*[:=]\s*\"?(?P<v>[^\"<,\s}\]][^\",\s}\]]{0,31})",
        _const("SSID"),
        lambda s, low: "ssid" in low,
        re.IGNORECASE,
    ),
    Rule(
        "gps_key",
        r"\b(?:lat|latitude|lon|lng|longitude)\s*[:=]\s*(?P<v>-?\d{1,3}\.\d{3,})",
        _const("GPS"),
        lambda s, low: "lat" in low or "lon" in low or "lng" in low,
        re.IGNORECASE,
    ),
    Rule(
        "number",  # IMEI, IPv4, 'lat, lon' pairs
        r"(?<![\w.+-])(?P<v>-?\d{1,2}\.\d{5,},\s?-?\d{1,3}\.\d{5,}|\d[\d.]{5,}\d)(?![\w.])",
        _number,
    ),
    Rule("vin", r"(?<![A-Za-z0-9])(?P<v>[A-HJ-NPR-Z0-9]{17})(?![A-Za-z0-9])", _vin),
    Rule(
        "mac",
        r"(?<![0-9A-Fa-f:-])(?P<v>[0-9A-Fa-f]{2}([:-])(?:[0-9A-Fa-f]{2}\2){4}[0-9A-Fa-f]{2})(?![0-9A-Fa-f:-])",
        _mac,
        lambda s, low: ":" in s or "-" in s,
    ),
    Rule("phone", r"(?<![\w+])(?P<v>\+\d{8,15})(?!\d)", _const("PHONE"), lambda s, low: "+" in s),
)


def load_secret(home: Path) -> bytes:
    """Read the redaction secret, creating it on first run (mode 0600 where supported)."""
    path = home / "secret.key"
    try:
        data = path.read_bytes().strip()
        if len(data) >= 32:
            return data
    except FileNotFoundError:
        pass
    home.mkdir(parents=True, exist_ok=True)
    value = secrets.token_hex(32).encode("ascii")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, value)
    finally:
        os.close(fd)
    return value


class Redactor:
    def __init__(self, secret: bytes, rules: tuple[Rule, ...] = DEFAULT_RULES) -> None:
        self._key = secret
        self.rules = rules
        self._compiled = [(r, re.compile(r.pattern, r.flags)) for r in rules]
        self.rules_hash = hashlib.sha256(
            (RULES_VERSION + "\n" + "\n".join(f"{r.name}\t{r.pattern}\t{r.flags}" for r in rules)).encode()
        ).hexdigest()[:16]
        self._cache: dict[str, str] = {}
        self._tokens: dict[tuple[str, str], str] = {}

    def token(self, kind: str, value: str) -> str:
        key = (kind, value)
        tok = self._tokens.get(key)
        if tok is None:
            digest = hmac.new(self._key, f"{kind}\0{value}".encode(), hashlib.sha256).hexdigest()
            tok = f"<{kind}:{digest[:8]}>"
            if len(self._tokens) < 100_000:
                self._tokens[key] = tok
        return tok

    def redact(self, text: str) -> str:
        low = text.lower()
        for rule, rx in self._compiled:
            if rule.guard is not None and not rule.guard(text, low):
                continue
            if rx.search(text) is None:
                continue
            text = rx.sub(lambda m, rule=rule: self._replace(m, rule), text)  # type: ignore[misc]
        return text

    def redact_cached(self, text: str) -> str:
        """For short, often repeated values such as tags."""
        out = self._cache.get(text)
        if out is None:
            out = self.redact(text)
            if len(self._cache) < 100_000:
                self._cache[text] = out
        return out

    def _replace(self, m: re.Match[str], rule: Rule) -> str:
        value = m.group("v")
        kind = rule.classify(value)
        if kind is None:
            return m.group(0)
        g = m.group(0)
        start, end = m.span("v")
        offset = m.start(0)
        return g[: start - offset] + self.token(kind, value) + g[end - offset :]

    def find(self, text: str) -> list[tuple[str, str]]:
        """Return (kind, value) for every value a rule would redact. Used by tests and checks."""
        out: list[tuple[str, str]] = []
        for rule, rx in self._compiled:
            for m in rx.finditer(text):
                kind = rule.classify(m.group("v"))
                if kind is not None:
                    out.append((kind, m.group("v")))
        return out


def redactor_for_home(home: Path) -> Redactor:
    return Redactor(load_secret(home))
