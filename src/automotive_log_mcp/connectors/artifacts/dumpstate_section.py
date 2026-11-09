"""Dumpstate sections of a bugreport text: what each section is, and the metadata some give.

A section starts ``------ TITLE (command) ------``. For files dumped by dumpstate the command
part is ``path: mtime`` (``------ VM TRACES AT LAST ANR (/data/anr/anr_...: 2026-01-01 00:00:03)
------``); :func:`header` returns that path and time, which become source metadata
(``section_file``, ``section_time``) and the fallback time of an artifact without its own.

:func:`format_for` maps a section to the connector that reads it (logs and artifacts).
Metadata-only sections are read here: ``SYSTEM PROPERTIES`` (fingerprint, time zone, a few build
properties), ``PROCESSES``/``ps`` (pid -> process name), ``UPTIME`` and ``BINDER TRANSACTIONS``
(outgoing transactions, used for the binder peer of an ANR).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ...model import ProcessInfo

SECTION = re.compile(r"------ (.*?)(?: \((.*)\))? ------$")
SECTION_END = re.compile(r"------ [\d.]+s was the duration of '(.*)' ------$")
FILE_CMD = re.compile(r"(/\S+): (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)$")
PROP = re.compile(r"\[([^\]]+)\]: \[(.*)\]$")
UPTIME_WORDS = re.compile(
    r"up (\d+) weeks?, (\d+) days?, (\d+) hours?, (\d+) minutes?(?:, (\d+) seconds?)?", re.IGNORECASE
)
UPTIME_TOYBOX = re.compile(r"up (?:(\d+) days?,\s+)?(?:(\d+):(\d\d)|(\d+) min)", re.IGNORECASE)
LOGCAT_BUFFER = re.compile(r"-b\s+(\w+)")
OUTGOING = re.compile(r"outgoing transaction \d+: \S+ from (\d+):(\d+) to (\d+):(\d+)(?: code (\w+))?")

KEEP_PROPS = (
    "ro.build.fingerprint",
    "ro.product.model",
    "ro.product.device",
    "ro.product.manufacturer",
    "ro.build.version.release",
    "ro.build.version.sdk",
    "ro.build.version.incremental",
    "ro.build.type",
    "ro.hardware",
    "persist.sys.timezone",
)


@dataclass(frozen=True)
class Header:
    title: str
    command: str
    file: str | None = None  # for sections that dump a file
    file_time: str | None = None  # its mtime as printed (device wall clock)

    def meta(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.file:
            out["section_file"] = self.file
        if self.file_time is not None:
            out["section_time"] = self.file_time
        return out


def header(line: str) -> Header | None:
    """Parse a section header line; None for duration lines and other text."""
    if not line.startswith("------ ") or SECTION_END.match(line):
        return None
    m = SECTION.match(line)
    if m is None:
        return None
    title, command = m.group(1), (m.group(2) or "").strip()
    fm = FILE_CMD.match(command)
    if fm is None:
        return Header(title, command)
    return Header(title, command, fm.group(1), fm.group(2))


def format_for(h: Header) -> tuple[str, str | None] | None:
    """(connector, stream) for sections indexed as sources; None for the others."""
    cmd, title = h.command, h.title
    if cmd.startswith("logcat"):
        buffers = LOGCAT_BUFFER.findall(cmd)
        if len(buffers) == 1 and buffers[0] != "all":
            return "logcat", buffers[0]
        return "logcat", "main" if title == "SYSTEM LOG" else None
    if cmd.startswith("dmesg") or title in ("KERNEL LOG", "LAST KMSG") or "kmsg" in cmd:
        return "kernel", "last_kmsg" if title == "LAST KMSG" else "kernel"
    if title == "VM TRACES AT LAST ANR":
        return "anr", None
    if title.startswith("TOMBSTONE"):
        return "tombstone", None
    if title.startswith("DROPBOX") or ("dumpsys" in cmd and " dropbox" in cmd):
        return "dropbox", None
    return None


def is_processes(h: Header) -> bool:
    return h.title.startswith("PROCESSES") or h.command.startswith("ps ")


def is_properties(h: Header) -> bool:
    return h.title == "SYSTEM PROPERTIES" or h.command == "getprop"


def is_binder_transactions(h: Header) -> bool:
    return h.title == "BINDER TRANSACTIONS"


def uptime_seconds(line: str) -> int | None:
    m = UPTIME_WORDS.search(line)
    if m:
        w, dd, hh, mm, ss = (int(x) if x else 0 for x in m.groups())
        return (((w * 7 + dd) * 24 + hh) * 60 + mm) * 60 + ss
    m = UPTIME_TOYBOX.search(line)
    if m:
        d, h, mi, only_min = m.groups()
        days = int(d) if d else 0
        if only_min:
            return days * 86400 + int(only_min) * 60
        return days * 86400 + int(h) * 3600 + int(mi) * 60
    return None


def parse_ps(lines: Iterable[str]) -> list[ProcessInfo]:
    """Parse ``ps`` output by its header (``USER PID [TID] PPID ... NAME|CMD``)."""
    out: list[ProcessInfo] = []
    cols_header: list[str] | None = None
    for line in lines:
        if not line.strip():
            continue
        cols = line.split()
        if cols_header is None:
            if "PID" in cols:
                cols_header = cols
            continue
        if len(cols) < len(cols_header):
            continue
        # The last column (NAME/CMD) may contain spaces.
        row = line.split(None, len(cols_header) - 1)
        rec = dict(zip(cols_header, row, strict=False))
        try:
            pid = int(rec["PID"])
        except (KeyError, ValueError):
            continue
        name = row[-1].strip()

        def num(key: str, rec: dict[str, str] = rec) -> int | None:
            v = rec.get(key)
            return int(v) if v is not None and v.isdigit() else None

        out.append(ProcessInfo(pid=pid, name=name, tid=num("TID"), ppid=num("PPID"), user=rec.get("USER")))
    return out


def parse_props(lines: Iterable[str], meta: dict[str, Any]) -> None:
    props = meta.setdefault("props", {})
    for line in lines:
        m = PROP.match(line)
        if m and m.group(1) in KEEP_PROPS:
            props[m.group(1)] = m.group(2)
    if "fingerprint" not in meta and "ro.build.fingerprint" in props:
        meta["fingerprint"] = props["ro.build.fingerprint"]
    if "persist.sys.timezone" in props:
        meta["timezone"] = props["persist.sys.timezone"]


def parse_binder_transactions(lines: Iterable[str]) -> list[dict[str, int]]:
    out = []
    for line in lines:
        m = OUTGOING.search(line)
        if m is not None:
            out.append(
                {
                    "from_pid": int(m[1]),
                    "from_tid": int(m[2]),
                    "to_pid": int(m[3]),
                    "to_tid": int(m[4]),
                }
            )
    return out
