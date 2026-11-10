# Connectors and clock alignment

How to add a log format (for example a company-specific ECU or vendor log) and how to place it on
the same timeline as the Android logs. A connector only turns bytes into records; search,
collapse, redaction, citations, detector rules and the timeline then work on it like on any
other source (design: one tool API, per-format connectors behind it).

Three ways, from least to most code:

| Kind | Where | When | Policy |
| --- | --- | --- | --- |
| YAML regex connector | a `.yaml` file in a rule directory | one line per record, a regex covers it | always allowed |
| `command` connector | a `.yaml` file plus any program (any language) | binary formats, an existing decoder, logic a regex cannot express | always allowed |
| Python entry point | an installed Python package | full control, streaming parse in-process, containers (formats holding several logs) | `allow_code_connectors: true` |

Rule directories are listed under `rule_dirs` in `config.yaml`; one directory may hold regex
and command connectors, detector rules and marker rules side by side (the top-level key decides:
`connector:` with `line:` for a regex connector or `command:` for a command connector,
`rule:`/`rules:`, `marker:`/`markers:`; detector and marker rules are described in
[rules.md](rules.md)). An invalid file stops the server at startup with the file name and the
reason.

A connector named like an existing one is an error unless it says `replace: true` (D13). Bump
`version` whenever the output changes: sources indexed with an older version are flagged
`stale: reindex recommended` in `list_sources`.

## The record model

What a connector produces per record (`RawRecord`, see `src/automotive_log_mcp/model.py`):

| Field | Meaning |
| --- | --- |
| `msg` | the message (required); multi-line records keep their lines joined with `\n` |
| `ts` | wall-clock time **as the device wrote it**, ns since the epoch computed as if it were UTC (no zone conversion; the zone goes into source metadata `timezone`) |
| `ts_src` | the source's own clock: equal to `ts` for wall-clock logs, ns since boot (or since ECU start) for monotonic logs; set `monotonic=True` and leave `ts` empty for those |
| `ts_raw` | the timestamp text as written (shown by `get_records`) |
| `level` | `V D I W E F` or empty |
| `tag`, `pid`, `tid` | empty when the format has none |
| `attrs` | other fields (scalars), e.g. `ecu`, `apid`, `channel`; filterable with `filter={'attrs': {...}}` |
| `stream` | sub-source name: records of each stream become their own source (`name:stream`) |
| `boot` | a boot marker: starts a new segment (own clock for monotonic sources) |
| `unparsed` | the connector could not parse the line; `msg` holds the raw text |
| `lines` | physical lines the record stands for (a merged multi-line message), default 1 |
| `artifact` | this record *is* an artifact's line (as a crash-buffer `FATAL EXCEPTION` is); no extra record is added for it |

Artifacts (`RawArtifact`): a document that is not a line stream (a crash dump, a coredump
summary, a fault record) with `type`, `headline`, `summary`, `sections` (named line lists that
cover the text in order, so `art:<id>:L<n>` is line `n` of the original), `signature` (groups
copies of one crash), `ts`, `ts_raw`, `pid`, `tid`, `process`, `level` (default `F`), `fields`.
Each gets a record on the timeline (`[art:<id>] <headline>` in the current source, unless a
record carries it) and is read with `read_artifact`.

Everything is redacted at index time (D5), so a connector never needs to redact.

## YAML regex connector

```yaml
# the shipped syslog.yaml, plus the optional keys it does not use (levels, replace)
connector: syslog                 # [a-z][a-z0-9_]*, at most 32 characters
version: "1"
description: BSD syslog / journalctl short output
sniff:
  filenames: ["syslog*", "messages*", "*.syslog", "journal*"]   # optional: +0.05 on a match
  min_fraction: 0.6                     # share of the first lines that must match `line`
line: '^(?P<ts>[A-Z][a-z]{2} [ \d]\d \d\d:\d\d:\d\d(?:\.\d+)?) (?P<host>\S+) (?P<tag>[^\s\[:]+)(?:\[(?P<pid>\d+)\])?: ?(?P<msg>.*)$'
continuation: '^\s'               # optional: such lines are appended to the previous record
timestamp:
  format: '%b %d %H:%M:%S'        # strptime format, or epoch | epoch_ms | epoch_us | monotonic
levels: {err: E, warning: W}      # optional: raw level -> V D I W E F
attrs: [host]                     # optional: groups to keep as attributes (default: all others)
replace: false                    # true: replace a connector with the same name
```

Named groups: `msg` (required), `ts`, `level`, `tag`, `pid`, `tid`; any other group becomes an
attribute. A format without a year gets one as logcat does (December → January increments it).
Lines that do not match `line` (and are no continuation) are kept as unparsed records. Examples:
[src/automotive_log_mcp/rules/connectors/syslog.yaml](../src/automotive_log_mcp/rules/connectors/syslog.yaml)
and [examples/rules/dlt_viewer_ascii.yaml](../examples/rules/dlt_viewer_ascii.yaml).

## `command` connector

A program decodes the file and writes one JSON object per line to its standard output:

```yaml
connector: mcu_trace
version: "1"
description: MCU trace decoded by mcu2jsonl
command: ["{python}", "{dir}/mcu2jsonl.py", "{path}"]
sniff:
  filenames: ["*.mcutrace"]
  head_regex: '^MCU TRACE v1\b'      # optional; also magic: "<hex bytes>"
  score: 0.9
timeout: 600                       # seconds; default 3600
replace: false                     # true: replace a connector with the same name
```

- Placeholders: `{path}` (the file; an archive member is written to a temporary file first),
  `{python}` (the interpreter running the server, so a Python decoder needs no PATH setup),
  `{dir}` (the YAML file's directory, for a decoder kept next to it), `{name}` (the source's
  display name). Without `{path}` the file is written to the program's standard input.
- Sniffing never runs the program: `filenames`, `magic` and `head_regex` must all hold (those
  given). With none of them the connector is used only with `format=<name>`.
- Output lines (fields as in the record model; see `connectors/command.py` for the details):

  ```json
  {"type": "meta", "values": {"timezone": "Europe/Berlin", "ecu": "MCU1"}}
  {"ts": "2026-10-01 08:00:01.123", "level": "E", "tag": "VHAL", "pid": 7, "msg": "...", "attrs": {"ecu": "MCU1"}}
  {"mono": "123.456789", "level": "I", "tag": "CAN", "msg": "bus up"}
  {"mono_ns": 123456789000, "msg": "...", "stream": "core1", "boot": true}
  {"type": "artifact", "artifact_type": "mcu_fault", "headline": "MCU hardfault", "summary": "...",
   "sections": [{"name": "registers", "lines": ["r0=0x0", "r1=0x2000"]}], "mono": "6.0", "signature": "hardfault"}
  ```

  `ts` is a string `YYYY-MM-DD hh:mm:ss[.fffffffff]` or an integer (ns); `mono` is seconds since
  boot (string or number), `mono_ns` an integer; `ts_raw` optionally keeps the time as the format
  wrote it; `level` is `V D I W E F` or a name (`error`, `warning`, `info`, …). A line that is not
  a JSON object becomes an unparsed record. A program that exits with a non-zero status (or runs past `timeout`) fails the
  import with the end of its standard error; the source is listed as `failed` with that error.
- Example: [examples/connectors/](../examples/connectors/) (`mcu_trace.yaml`, the decoder
  `mcu2jsonl.py` and a sample file). Copy the directory into a rule directory or list it under
  `rule_dirs`.

## Python entry point

```python
# mycompany_logs/connector.py
from collections.abc import Iterator
from automotive_log_mcp.connectors.base import ParseItem, SourceContext, TextConnector, head_lines
from automotive_log_mcp.model import RawRecord

class EcuLog(TextConnector):
    name = "ecu_log"
    version = "1"

    def sniff(self, head: bytes, filename: str) -> float:
        lines = head_lines(head)
        return 0.9 if lines and lines[0].startswith("ECU LOG") else 0.0

    def parse_lines(self, lines: Iterator[str], ctx: SourceContext) -> Iterator[ParseItem]:
        for line in lines:
            ...
            yield RawRecord(msg=..., ts_src=..., monotonic=True, level=..., tag=...)
```

```toml
# pyproject.toml of the package
[project.entry-points."automotive_log_mcp.connectors"]
ecu_log = "mycompany_logs.connector:EcuLog"
```

Install the package into the server's environment and set `allow_code_connectors: true` in
`policy.yaml`. A binary format implements `parse(stream, ctx)` instead of `parse_lines`; a format
that holds several logs implements a container (`expand(member, ctx)`, see
`connectors/archive.py` and `connectors/bugreport.py`). `ctx.meta` is stored as source metadata:
set `ctx.meta["timezone"]` when the format knows the device's zone.

Test a connector like the package's own: a small synthetic fixture per format variant and a golden
file of the records (`tests/test_golden.py`); the CLI shows the result without an agent:

```bash
automotive-log-mcp index T-1 trace.mcutrace --format mcu_trace
automotive-log-mcp sources T-1
automotive-log-mcp query T-1 --level W
```

## Clock alignment (M5)

Records keep their times as written, so every tool except `timeline` and `context(scope='all')`
reads each source on its own clock. Those two use the **case clock**: by default the wall clock
of the largest wall-clock source (all wall-clock sources of one file share it), changed with
`align(case, mode='reference', source=...)`. Every other clock is mapped onto it per segment:

```
case time = native + offset + drift_ppm·1e-6·(native - start)     (piecewise: one piece per stretch)
```

The mapping is recomputed after every import and when manual settings or marker rules change.
`align(case)` shows it; `list_sources` flags each source that is not exact. Automatic anchors,
best first:

| Confidence | Found from |
| --- | --- |
| `exact` | the reference clock (wall-clock sources of the reference file) |
| `manual` | `align` offsets and anchors |
| `paired` | the same line in two sources (dmesg and `logcat -b kernel`, two captures of one log), or UTC times in kernel lines (`PM: suspend exit … UTC`), agreeing within 10 ms (the error is never below the timestamps' resolution: 1 ms for logcat) |
| `marker` | the same, or marker rules, with a larger spread |
| `estimate` | a bugreport's kernel log: dumpstate time minus uptime (±1 min) |
| `assumed` | another device's wall clock as written (shifted by the time-zone difference when both zones are known) |

A monotonic clock without an anchor is not placed: `timeline` names it and `align` says how to
place it. Same lines on two different devices can agree by chance (two units with the same
software print the same boot lines a fixed time after boot); pairs must make up at least half of
the candidates, and `align` prints how many lines a mapping rests on.

### Marker rules

For a company log the useful anchors are events that both sides log: a request and its
reception, a CAN signal sent and received, a sequence number. A marker rule pairs them by key
(full reference: [rules.md](rules.md#marker-rules)):

```yaml
marker: vhal_set_property
description: VHAL SET request logged by the head unit and received by the MCU
a:
  match: 'setProperty prop=(?P<prop>0x[0-9a-f]+) seq=(?P<seq>\d+)'
  tags: [VehicleHal*]
b:
  match: 'SET prop=(?P<prop>0x[0-9a-f]+) seq=(?P<seq>\d+)'
  sources: [mcu_trace]      # globs on source name or format
delay: 2ms                  # b is logged this long after a
tolerance: 200ms            # offsets of one stretch must agree within this
```

Both regexes need the same named groups; a key seen twice on one side is not used. At least two
pairs are needed (three when there are six or more candidates), and half of the pairs must agree.
With enough pairs over a minute or more, a drift is fitted. Marker patterns run over the redacted
messages and are not redacted themselves (detector patterns are). Example:
[examples/rules/markers_vhal_set.yaml](../examples/rules/markers_vhal_set.yaml).

### Manual alignment

```text
align(case, mode='anchor', at='log:5#120', ref='log:1#3400')          # same moment
align(case, mode='anchor', at='log:5#120', ref='log:1#3400', offset='2ms')   # ref 2 ms after at
align(case, mode='offset', source='5', offset='+1.5s')                # case = source time + 1.5 s
align(case, mode='offset', source='6', seg=2, offset='2026-01-01 07:59:58.000')  # boot time of a kernel segment
align(case, mode='reference', source='5')                             # use source 5's clock
align(case, mode='reset')                                             # drop manual settings
```

Two or more anchors between the same two clocks also fit a drift. The CLI has the same as
`automotive-log-mcp align T-1 --anchor log:5#120=log:1#3400`, `--offset 5=+1.5s`,
`--reference 5`, `--reset`.
