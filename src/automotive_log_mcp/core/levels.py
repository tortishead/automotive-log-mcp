"""Level normalization to ``V D I W E F`` (None when unknown)."""

from __future__ import annotations

_NAMES: dict[str, str] = {
    "V": "V",
    "VERBOSE": "V",
    "TRACE": "V",
    "D": "D",
    "DEBUG": "D",
    "DBG": "D",
    "I": "I",
    "INFO": "I",
    "INF": "I",
    "NOTICE": "I",
    "N": "I",
    "W": "W",
    "WARN": "W",
    "WARNING": "W",
    "WRN": "W",
    "E": "E",
    "ERROR": "E",
    "ERR": "E",
    "F": "F",
    "FATAL": "F",
    "A": "F",  # logcat 'assert'
    "ASSERT": "F",
    "CRIT": "F",
    "CRITICAL": "F",
    "ALERT": "F",
    "EMERG": "F",
    "PANIC": "F",
}

# Kernel printk priorities 0..7 (KERN_EMERG .. KERN_DEBUG).
_KERNEL = ("F", "F", "F", "E", "W", "I", "I", "D")

LEVEL_RANK = {"V": 0, "D": 1, "I": 2, "W": 3, "E": 4, "F": 5}


def normalize(name: str | None) -> str | None:
    if not name:
        return None
    return _NAMES.get(name) or _NAMES.get(name.upper())


def from_kernel_priority(prio: int) -> str:
    """Map a syslog/printk priority (facility bits ignored) to a level."""
    return _KERNEL[prio & 7]
