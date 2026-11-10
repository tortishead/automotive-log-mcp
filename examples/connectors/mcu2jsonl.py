"""Example decoder for the `mcu_trace` command connector: made-up MCU trace -> JSON lines.

Input (text, for the example):

    MCU TRACE v1 ecu=MCU1 tz=Europe/Berlin
    1200 I CAN: bus up
    5300 E VHAL: SET prop=0x11400400 seq=17 rejected
    FAULT 6000 hardfault pc=0x0800f00d
      r0=0x00000000 r1=0x20001000
    END

The first column is milliseconds since MCU start (a monotonic clock); the server aligns it with
the other sources (marker rules, anchors). Output: one JSON object per line, see
connectors/command.py for the fields.
"""

from __future__ import annotations

import json
import re
import sys

LINE = re.compile(r"^(\d+) ([VDIWEF]) (\w+): (.*)$")


def main(path: str) -> int:
    out = sys.stdout
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    head = lines[0].split() if lines else []
    values = dict(p.split("=", 1) for p in head[3:] if "=" in p)
    meta = {"ecu": values.get("ecu", "MCU")}
    if "tz" in values:
        meta["timezone"] = values["tz"]
    out.write(json.dumps({"type": "meta", "values": meta}) + "\n")
    start = {"msg": "MCU start", "mono": "0.0", "boot": True, "attrs": {"ecu": meta["ecu"]}}
    out.write(json.dumps(start) + "\n")
    i = 1
    while i < len(lines):
        line = lines[i]
        if line.startswith("FAULT "):
            _, ms, what, *rest = line.split(" ", 3)
            body = [line]
            i += 1
            while i < len(lines) and lines[i] != "END":
                body.append(lines[i].strip())
                i += 1
            out.write(
                json.dumps(
                    {
                        "type": "artifact",
                        "artifact_type": "mcu_fault",
                        "headline": f"MCU {what} {' '.join(rest)}".strip(),
                        "summary": "\n".join(body[:5]),
                        "sections": [{"name": "registers", "lines": body}],
                        "mono": f"{int(ms) / 1000:.3f}",
                        "signature": what,
                    }
                )
                + "\n"
            )
        else:
            m = LINE.match(line)
            if m:
                ms, level, tag, msg = m.groups()
                rec = {"mono": f"{int(ms) / 1000:.3f}", "level": level, "tag": tag, "msg": msg}
                rec["attrs"] = {"ecu": meta["ecu"]}
                out.write(json.dumps(rec) + "\n")
            elif line.strip():
                out.write(line + "\n")  # not JSON: kept as an unparsed record
        i += 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
