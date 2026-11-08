# automotive-log-mcp: design

Nov 8, 2026 · @Xuan · draft

## What it is

automotive-log-mcp is an MCP server that gives any agent tools to work with automotive logs: Android logcat and bugreports, kernel logs, AUTOSAR DLT, and any other text log described by a small config file. Any MCP client can use it (Copilot, Claude, Cursor, or a custom agent built on an SDK).

The first version of the API is designed for **defect analysis**: find what went wrong in a set of logs, read the evidence around it, line up events across sources, and cite every finding to the log line it comes from. Other uses (test-run triage, KPI extraction, regression checks) should fit the same core later, but they do not drive the first API.

## Architecture in short

**One common tool API, with per-format connectors behind it.** Connectors only turn bytes into records. Everything after that is shared and format-blind: one normalized record model, one index, one clock model, one set of tools.

```
 files / zips / bugreports / DLT traces
        │
  ┌─────▼──────┐   sniff + parse (per format, small)
  │ connectors │   logcat · bugreport · kernel · DLT · artifacts · regex-YAML
  └─────┬──────┘
        │ record stream + artifacts
  ┌─────▼──────────────────────────────┐
  │ shared core                        │
  │  normalize → template → redact     │
  │  SQLite + FTS5 index per case      │
  │  clock model + alignment           │
  │  detectors (YAML rules)            │
  └─────┬──────────────────────────────┘
        │ MCP tools, capped outputs, citations log:<src>#<seq>, art:<id>
  ┌─────▼──────┐
  │ any MCP client / agent │
  └────────────┘
```

Why this split:

- **The agent learns one tool set.** Agent prompts and skills stay the same when a new format arrives. Cross-source questions ("what happened in DLT when logcat shows the crash?") only work if all sources share fields and a timeline.
- **Connectors stay tiny.** A new format is a `sniff()` and a `parse()` that yields records, usually 100–300 lines. Simple text formats need no code at all: a YAML regex connector covers them.
- **Format-specific detail is not lost.** Fields that only one format has (DLT ECU/app/context ids, logcat uid, kernel facility) go into typed optional columns or `attrs`, and every filter can use them.
- **The server is deterministic.** It parses, indexes, searches, counts and aligns. It never calls a model; summarizing and reasoning are the agent's job. Every result is reproducible, and the server can be used where model access is restricted.

Only add a format-specific tool when a format has an operation the common tools cannot express. Example: decoding DLT non-verbose payloads with a FIBEX file is a connector option, not a tool.

## Concepts

- **Case**: a named set of sources for one investigation (e.g. a ticket id). One SQLite file per case, so cases are isolated and easy to delete.
- **Source**: one logical log stream. A bugreport zip expands into several sources (main, system, events, crash, kernel, …) with a shared `origin`.
- **Record**: one log entry, normalized (next section).
- **Segment**: a stretch of one source between reboots or clock resets. Each record belongs to exactly one segment, so "after the second boot" is expressible.
- **Artifact**: a document that is not a line stream: tombstone, ANR trace, Java crash report, dropbox entry, `dumpsys` output, kernel panic (pstore/ramoops), coredump. Stored whole with parsed key fields and placed on the timeline as one event.
- **Template**: the message with variable parts (numbers, hex, ids, paths) replaced, e.g. `setProperty failed, status=<*>`. Used for dedup, counting and diffs.

## Normalized record model

| Field | Type | Notes |
| --- | --- | --- |
| `source_id` | int | which source |
| `seq` | int | record number in the source, 1-based; with `source_id` it is the citation |
| `offset` | int | byte offset in the original (decompressed) file, for jump-to-raw |
| `segment` | int | boot/clock segment within the source |
| `ts_raw` | text | timestamp exactly as written |
| `clock` | enum | `wall_local`, `wall_utc`, `monotonic`, `boottime`, `dlt_ecu_ticks`, `none` |
| `ts_src` | int64 ns | timestamp on the source's own clock |
| `ts` | int64 ns UTC | on the common timeline after alignment; null until aligned |
| `ts_conf` | enum | `exact`, `anchored`, `estimated`, `none` |
| `level` | enum | `V D I W E F` (DLT verbose→V, debug→D, info→I, warn→W, error→E, fatal→F; kernel 0–2→F, 3→E, 4→W, 5–6→I, 7→D); null if unknown |
| `tag` | text | logcat tag, DLT `app/ctx`, kernel subsystem prefix if parseable |
| `pid`, `tid`, `uid` | int | null if the format has none |
| `process` | text | from the bugreport process list or a `ps` section when available |
| `ecu`, `app_id`, `ctx_id` | text | DLT only |
| `buffer` | text | logcat buffer (main, system, events, crash, radio, kernel) |
| `msg` | text | message, after redaction |
| `template_id` | int | template of `msg` |
| `attrs` | json | anything else the connector knows (DLT message type, session id, kernel facility, …) |

Source metadata (one row per source): format, connector version, file path and hash, record count, time range per clock, segments, detected timezone, and build fingerprint and device info when found (bugreport header, `ro.build.fingerprint`). The fingerprint lets an agent pick the matching code version in whatever code-search tool it also uses.

## Artifacts: tombstones, ANRs, crash reports, dumps

Bugreports and DLT traces usually carry more than log lines, and these are often the key evidence. They do not fit the line model: a tombstone is one event with a structured body, and a `dumpsys` section is a snapshot of state at one moment. Forcing them into lines loses the structure, and leaving them out loses the evidence. So they are a second entity next to records.

**Types and sources**

| Type | Typical location | Parsed key fields |
| --- | --- | --- |
| `tombstone` (native crash) | bugreport `FS/data/tombstones/tombstone_NN` (text, and `.pb` on Android 12+) | time, pid/tid, process, build fingerprint, signal and code, fault address, abort message, top frames with library + offset, `Cause:` line |
| `anr` | bugreport `FS/data/anr/anr_*`, `traces.txt`, ANR section in `dumpsys activity` | time, process, pid, reason, main thread state and stack, threads it waits on (lock holder), binder peer |
| `java_crash` | crash log buffer, dropbox `data_app_crash`, `system_server_crash` | time, process, pid, exception class, message, top app frames, `Caused by` chain |
| `dropbox` | bugreport `FS/data/system/dropbox/*` (e.g. `SYSTEM_TOMBSTONE`, `data_app_anr`, `system_server_watchdog`, `SYSTEM_BOOT`, `SYSTEM_LAST_KMSG`) | tag, time, body type; body passed to the matching parser above |
| `kernel_panic` | pstore / `last_kmsg` / `console-ramoops`, dropbox `SYSTEM_LAST_KMSG` | panic reason, last kernel lines; also indexed as kernel records of the previous boot |
| `dumpsys` | bugreport `DUMPSYS` sections per service (`car_service`, `activity`, `meminfo`, `package`, `media.audio_flinger`, …) | service name, dump time, size; body split into subsections by headings |
| `dumpstate_section` | other bugreport sections (`PROCESSES AND THREADS`, `MEMORY INFO`, properties, `CPU INFO`) | section name, dump time; properties and process list also feed source metadata (fingerprint, timezone, pid → process name) |
| `dlt_file` | DLT file transfer messages (`FLST/FLDA/FLFI`), e.g. a coredump or crash report sent over DLT | file name, size, serial, time; reassembled to a file and parsed if it is a known type |
| `coredump` | `.core` / `.gz` files in a bugreport or DLT file transfer | metadata only (process, signal, size, time); not analysed by the server |

**Model**

| Field | Notes |
| --- | --- |
| `artifact_id` | citation `art:<id>`, sections `art:<id>/<section>`, lines `art:<id>:L<line>` |
| `type`, `origin` | as above; `origin` points to the bugreport or DLT source it came from |
| `ts`, `ts_conf` | event time on the common timeline (tombstone `Timestamp:` line, ANR time, dump time) |
| `pid`, `process`, `tid` | when known |
| `summary` | parsed key fields as JSON (signal, abort message, top frames, exception, ANR reason, blocked-on) |
| `signature` | normalized crash signature: type + process + exception/signal + top 3 own frames without offsets; groups repeats and can be matched against known-issue lists |
| `body` | full text after redaction, with section index; FTS-indexed per section |

**Link to log lines**

- Each artifact also creates one synthetic record in its origin source (`tag=artifact`, message = one-line summary, `attrs.artifact_id`), so `search`, `context`, `timeline`, `count` and `detect` see it at the right time without special cases.
- Detector rules can merge evidence: a `FATAL EXCEPTION` line in logcat, the `crash` buffer entry, the dropbox entry and a tombstone for the same pid and time window become one crash event with all citations.
- Signatures group repeats ("this native crash happened 7 times in 3 boots").

Symbolizing native frames (library + offset to function and line) needs unstripped symbols for the exact build and is out of scope for the first version. The summary keeps library and offset so an agent or engineer can symbolize offline.

## MCP tools

All tools take `case`. All outputs are capped (default 50 lines, about 8 KB) and say when a cap was hit and how to narrow. Every returned line carries a citation (`log:<source>#<seq>` or `art:<id>…`), the log equivalent of `path:line`.

Shared filter object, used by every query tool:

```json
{
  "sources": [1, 3],            "from": "2026-10-11T08:12:00Z",  "to": "+30s",
  "level": "W",                  "tags": ["HvacCtrl", "VHAL*"],   "not_tags": ["chatty"],
  "pid": 1234, "tid": null,      "process": "vendor.hvac*",
  "ecu": "HU1", "app_id": "HVAC", "ctx_id": null,
  "segment": 2,                  "template_id": null
}
```

Times accept absolute values, `+30s`/`-2m` relative to a reference, or citation references (`"from": "log:2#18233-5s"`).

Tools in priority order. **P0** is the smallest set that answers a single-bugreport defect question; build it first and try it on real cases before adding more. **P1** makes agents cheaper and faster on big logs. **P2** is for multi-source cases and final reports.

| Prio | Tool | What it does | Why at this priority |
| --- | --- | --- | --- |
| **P0** | `add_source(case, path, format?, label?, tz?, options?)` | Registers a file, folder or zip; sniffs the format if not given; indexes in the background. Returns source ids. | Nothing works without it |
| **P0** | `list_sources(case)` | Sources with format, record count, time range, segments, index progress, build fingerprint. | First call of every session |
| **P0** | `search(case, query, mode=text\|regex, filter?, collapse=true, count_only?, limit, cursor)` | FTS5 or regex over `msg` and tag. `collapse` folds repeated templates into one line with count and first/last time. | Finds the error; collapse is the main token saver |
| **P0** | `context(case, at, before=20, after=20, scope=source\|pid\|all)` | Lines around a record or a time. | Reading around a hit is most of the analysis |
| **P0** | `list_artifacts(case, type?, filter?)` | Tombstones, ANRs, crash reports, dropbox entries, dumps, with time, process, summary and signature. | Crash evidence often sits in these, not in logcat |
| **P0** | `read_artifact(case, id, section?, start?, end?)` | Parsed summary, then a section or line range. A full `dumpsys` is never returned in one piece. | Read a tombstone backtrace or one `dumpsys` service |
| **P0** | `get_records(case, ids)` | Full records for up to 20 citations (other tools cut messages at about 200 characters, marked `…`). | Stack traces and long payloads |
| P1 | `overview(case, filter?)` | Counts per level, top tags/processes/templates, reboots, gaps, error bursts, coarse histogram, detector hit counts. | Saves several blind searches on a big log |
| P1 | `detect(case, rules?, filter?)` | YAML rules: Java/native crash, ANR, watchdog, LMK, SELinux denials, binder failures, VHAL errors, service restarts, DLT ECU resets. | Turns domain know-how into one call |
| P1 | `count(case, by=tag\|level\|template\|pid\|source, filter?, bucket?)` | Aggregates, optionally as a time series. | "How often, since when?"; until then `search(count_only)` covers simple cases |
| P2 | `timeline(case, from, to, filter?)` | Merged, time-ordered records across sources. | Only useful once there are several aligned sources |
| P2 | `align(case, source, method=auto\|anchor\|offset, …)` | Puts a source on the common timeline; returns offset, drift, confidence. | Needed when sources have different clocks (DLT + logcat); one bugreport shares a clock |
| P2 | `diff(case, a, b, filter?)` | Templates only in A, only in B, and large count changes. | Strong for good-vs-bad runs, but needs a reference log |
| P2 | `check_citations(case, markdown)` | Rejects citations to records or artifacts that don't exist; warns when nothing is cited. | Final reports and UIs |

Rules for all tools:

- `search` has `count_only=true`, so the agent can size a query before pulling lines.
- Empty results say why: "no match", "offset beyond result set", or "query looks like a regex but mode=text".
- Every timestamp printed in an output can be passed back as `from`/`to` unchanged, so the agent never converts time formats.
- Invalid parameters (level out of range, `from` after `to`) fail with a clear message instead of an empty result.
- Results are returned as compact text for the model and as structured content (JSON per line, with citation fields) for clients that render or post-process them.
- All tools except `add_source` and `align` are marked read-only in their MCP annotations.

Deliberately not tools:

- **No `summarize`.** `overview`, `count` and `detect` give compact facts; the agent writes the summary with citations. A server-side summary would hide which lines it used and would need a model.
- **No "read the whole log".** Raw access is always a capped window (`context`, `timeline`, `read_artifact`).

The server also ships usage guidance for agents: the MCP `instructions` text (analysis order, citation rule, alignment honesty) and an MCP prompt `defect_triage` with the same steps, so any client gets sensible defaults without its own setup.

Output format, short for the model and clickable for a UI:

```
log:2#18233  10-11 08:12:03.412  E 1234/1250 HvacCtrl  setProperty failed, status=NOT_AVAILABLE   ×37 (08:12:03–08:14:51)
log:5#90211  10-11 08:12:03.398~ W HU1/HVAC/CTRL        CAN timeout on node 0x2A
```

`~` marks a timestamp that is `anchored` or `estimated` rather than exact.

## Connector interface

```python
@dataclass
class SourceContext:
    path: str
    label: str | None
    tz: str | None            # from user, bugreport or config
    year_hint: int | None     # logcat threadtime has no year
    options: dict             # e.g. {"fibex": ["path/to/ecu.xml"]} for DLT

class Connector(Protocol):
    name: str                 # "logcat", "dlt", ...
    version: str              # stored per source; bump to force reindex

    def sniff(self, head: bytes, filename: str) -> float: ...
        # confidence 0..1 from the first ~64 KB

    def parse(self, stream: BinaryIO, ctx: SourceContext) -> Iterator[RawRecord | RawArtifact]: ...
        # streaming; never reads the whole file into memory

    def metadata(self) -> dict: ...
        # after parse: fingerprint, device, timezone, segments, clock anchors found

class Container(Protocol):    # zip, tar, gz, bugreport, split DLT files
    def expand(self, path: str) -> Iterator[tuple[str, BinaryIO, str | None]]: ...
        # (member name, stream, suggested connector)
```

The core does everything else: level normalization, templates, redaction, segment detection, batched inserts, FTS indexing.

**Connectors**

| Connector | Phase | Notes |
| --- | --- | --- |
| `logcat` | 1 | `threadtime` (default), `time`, `brief`, `long`, plus `-v year`, `-v epoch`, `-v monotonic`, `-v uid`, `-v UTC/zone`. Buffer markers and multi-line messages (stack traces continue the previous record). |
| `bugreport` (container) | 1 | Zip or `.txt`: splits sections (`SYSTEM LOG`, `EVENT LOG`, `KERNEL LOG`, `crash`, `DUMPSYS`, other dumpstate sections), reads fingerprint, uptime, timezone and the process list; sends tombstones, ANR traces, dropbox and pstore files to the artifact parsers. |
| `kernel` | 1 | `dmesg` (`[  12.345678]`), `/proc/kmsg` / `dmesg -r` (`<6>`), `dmesg -T`. |
| `regex-yaml` | 1 | A YAML file per format with a line regex, named groups mapped to record fields, timestamp format and clock. Covers vendor service logs, QNX `slog2info` text output, test-runner logs. |
| artifact parsers: `tombstone`, `anr`, `java_crash`, `dropbox`, `dumpsys`, `kernel_panic` | 1 | Text tombstones first; tombstone `.pb` (Android 12+) via the AOSP protobuf schema when no text is present. |
| `dlt` | 2 | Storage format (`DLT\x01` header): storage time, ECU ticks (0.1 ms), ECU/app/context ids, verbose arguments; non-verbose via FIBEX files in `options`. Parsing by ESR Labs `dlt-core` (Rust, Apache-2.0) in a sidecar binary `dlt2jsonl`; the Python connector reads its JSON lines. Split/rotated DLT files are one source via a container. |
| `dlt_file` (artifact parser) | 2 | Reassembles DLT file transfers; parses known types, stores coredumps as metadata only. |
| later | — | CAN traces (`.asc`/`.blf`) if bus timing matters, perfetto/systrace summaries, QNX binary slog2, remote fetch from ticket systems or test benches. |

Example `regex-yaml` connector:

```yaml
name: hvac-service-log
sniff: '^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} \[(DBG|INF|WRN|ERR)\]'
line: '^(?P<ts>\S+ \S+) \[(?P<level>\w+)\] (?P<tag>[\w.]+): (?P<msg>.*)$'
continuation: '^\s'            # lines starting with whitespace belong to the previous record
timestamp: { format: '%Y-%m-%d %H:%M:%S.%f', clock: wall_local }
levels: { DBG: D, INF: I, WRN: W, ERR: E }
```

Default rules and connectors ship in the package. Users add their own from configured rule directories (for example a team repo), which override defaults by name.

## Large files

Logcat from a long test drive is easily 0.5–2 GB; DLT traces from all ECUs can be several GB. The design keeps memory and tokens flat.

- **Stream, never slurp.** Connectors are generators over a buffered stream; gz/zip members are decompressed on the fly. `offset` is tracked per record so the raw line can always be shown.
- **Index once, query many.** Records go into SQLite in batches of ~50k per transaction with WAL; FTS5 (external-content table over `msg` and `tag`) is filled in the same pass. Target at least 100k lines/s; measure on real traces.
- **Background indexing.** `add_source` returns at once; `list_sources` shows progress, and queries on a source still indexing work on what is committed and say so.
- **Dedup by template.** `collapse=true` turns 40k identical lines into one; this is the biggest token saver.
- **Hash-based reuse.** Sources are keyed by file hash plus connector version, so loading the same file into a second case can reuse the index.
- **Hard caps everywhere.** Line count, byte budget and cursors. When a query would return too much, the tool returns counts by tag/template and suggests filters.
- **Beyond SQLite** (tens of GB per case): DuckDB over Parquet behind the same store interface. Not needed at first.

## Time alignment

Each format has its own clock, so alignment is an explicit step, not a guess the model makes.

| Source | Clock as written | Problem |
| --- | --- | --- |
| logcat threadtime | local wall time, no year, no zone | year and zone unknown; jumps when the clock is set (NTP, GPS, RTC) |
| logcat `-v monotonic` | seconds since boot | needs boot time to map to wall |
| kernel dmesg | monotonic since boot | same; resets every boot |
| DLT | storage time (logger's wall clock) and ECU ticks since ECU start | each ECU has its own start; logger clock may differ from the head unit's |
| other text logs | anything | declared in the YAML |

**Model.** Each source segment gets a mapping `ts = a · ts_src + b` (offset `b`, drift `a`, usually 1). `ts` and `ts_conf` are recomputed when the mapping changes; the raw timestamp never changes.

**Automatic anchors, tried in order:**

1. **Same clock already.** Sources in one bugreport share the device's clocks; the bugreport header gives timezone and year, and uptime/boot time maps monotonic kernel lines to wall time (`exact`).
2. **Paired timestamps in one record.** Logcat with wall and monotonic time, kernel lines with Android-time printk, DLT records with storage time and ECU ticks: fit `a, b` by least squares per ECU and segment (`exact` or `anchored`).
3. **Shared events.** Known boot markers in both sources (kernel `Booting Linux`, `init: starting service`, `boot_completed`, DLT ECU startup messages, or a marker line written by test scripts): match them and take the offset (`anchored`).
4. **Wall clock jumps** inside a logcat (e.g. RTC at 2000-01-01 until GPS/NTP sets the time) start a new segment, aligned from monotonic lines around the jump.

**Manual fallback.** `align(method=anchor, a="log:2#18233", b="log:5#90211")` says two records are the same moment; `align(method=offset, seconds=-3.2)` sets a known skew. Results are stored per segment and shown in `list_sources`.

**Honesty rule.** Cross-source outputs report alignment confidence, and the server's agent instructions say: do not claim "A caused B" from order alone when either side is `estimated` or the gap is smaller than the alignment error.

Missing year and zone come from the bugreport, then the case's `tz`, then file mtime, in that order; the source is marked `estimated` if none exist.

## Privacy and access policy

Logs hold personal data: VINs, IMEI/serials, MAC addresses, GPS coordinates, phone numbers, account emails, Wi-Fi SSIDs. Many companies also restrict what may reach an AI model. The server supports this without knowing any company's rules:

- **Redaction at index time**, so no tool can return the raw value. Rules are a YAML list of regexes with replacement tokens (`<VIN>`, `<GPS>`). Redacted values carry a keyed hash suffix (`<VIN#a3f1>`, HMAC with a per-installation secret) so the agent can see that two lines mention the same vehicle without the value being recoverable.
- **Policy file**: blocked sources or ECUs, allowed tools, byte budget per session, output caps.
- **Call log**: one JSONL line per tool call (case, tool, arguments, result size, duration), without log content, for audit and cost tracking.
- **CLI without an agent**: the same queries from the command line, for engineers or environments where no model may be used.
- The original files are never modified or copied; engineers can still open them with their usual viewers.

## Transport and deployment

- **Local stdio first.** The MCP client starts the server; logs stay on the user's machine and case databases sit in `~/.automotive-log-mcp/cases/`.
- **HTTP later** (streamable HTTP, token auth), same code, for shared servers next to test benches or ticket attachments. Remote use then needs an upload or a server-side path policy for `add_source`.
- Python 3.11+, stdlib `sqlite3` with FTS5 (checked at startup), official `mcp` SDK, `pyyaml`; `dlt2jsonl` as a prebuilt static binary per platform (Linux, Windows, macOS).

## Related work

- **[Ulenspiegel/dlt-mcp-server](https://github.com/Ulenspiegel/dlt-mcp-server)** (MPL-2.0): a DLT Viewer plugin serving `get_log_summary`, `search`, `get_messages`, `get_selection` and `set_report` from a running DLT Viewer. Ideas taken: payload previews plus a full-record tool (`get_records`), `count_only`, timestamps that round-trip into filters, explicit empty-result reasons and regex warnings, strict parameter validation, and validating report citations (`check_citations`). Not taken: running inside a GUI, DLT only, literal-only search, no auth. It remains a good option for DLT-only work inside DLT Viewer.
- **[esrlabs/dlt-core](https://github.com/esrlabs/dlt-core)** (Apache-2.0): Rust DLT parser with FIBEX support, used for the DLT connector. No file-transfer or ARXML support; file transfer is reassembled in the connector.
- **COVESA DLT Viewer**: reference viewer; its decoded text export is a fallback for setups that only have ARXML.

## Open questions

- Which log types typically come with a defect: bugreport zip, plain logcat, multi-ECU DLT, vendor logs? This sets connector order.
- DLT: verbose or non-verbose, and are FIBEX files available?
- Typical and worst-case sizes per case.
- Where logs live (ticket attachments, test bench shares, laptops): decides when HTTP and remote fetch are needed.
- Default redaction list: which fields must be masked out of the box.
