"""Artifact parsers (M2): tombstone (text), anr, java_crash, dropbox; dumpstate_section metadata.

``tombstone``, ``anr`` and ``dropbox`` are also connectors (files in a bugreport zip, sections of
the bugreport text); ``java_crash`` is reached through dropbox entries and logcat
``FATAL EXCEPTION`` records. ``dumpsys``, ``kernel_panic`` and tombstone ``.pb`` are M5.

:func:`complete` runs once per imported file, after every member has been read: it fills what
only the whole bugreport knows (process names from the process list, the binder peer of an ANR).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ...model import ProcessInfo, RawArtifact
from . import anr
from .common import Parsed
from .dropbox import DESCRIBE

TYPES = ("tombstone", "anr", "java_crash", "dropbox")


def process_names(processes: Iterable[ProcessInfo], artifacts: Iterable[RawArtifact]) -> dict[int, str]:
    """pid -> process name: the process list (``ps`` at capture time), then what artifacts say
    (a crashed process is usually gone from ``ps``; an ANR trace names every process it dumps)."""
    names: dict[int, str] = {}
    arts = list(artifacts)
    for a in arts:
        for pid, name in a.fields.get("pids", {}).items():
            if name:
                names.setdefault(int(pid), name)
        if a.pid is not None and a.process:
            names[a.pid] = a.process
    procs = list(processes)
    for p in procs:  # 'ps -T' rows of other threads carry thread names: main threads first
        if p.tid is None or p.tid == p.pid:
            names.setdefault(p.pid, p.name)
    for p in procs:
        names.setdefault(p.pid, p.name)
    return names


def complete(artifacts: list[RawArtifact], processes: list[ProcessInfo], facts: dict[str, list[Any]]) -> None:
    names = process_names(processes, artifacts)
    outgoing = facts.get("binder_outgoing", [])
    for a in artifacts:
        changed = False
        if a.process is None and a.pid is not None and a.pid in names:
            a.process = names[a.pid]
            a.fields["process_from"] = "process list"
            changed = True
        if a.type == "anr" and anr.complete(a, outgoing, names):
            changed = True
        if changed and a.type in DESCRIBE:
            p = Parsed(a.type, a.sections, a.fields, a.ts, a.ts_raw, a.pid, a.tid, a.process, a.level)
            # The headline is already on the timeline (the record); summary and signature follow.
            _, a.summary, a.signature = DESCRIBE[a.type](p)


__all__ = ["TYPES", "complete", "process_names"]
