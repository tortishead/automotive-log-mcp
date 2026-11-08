# aaos-logs: MCP server for defect analysis

Nov 8, 2026 · @Xuan · design proposal for the defect-analysis POC

## Answer in short

**Yes: one common tool API, with per-format connectors behind it.** But the shared part is bigger than the API. Connectors only turn bytes into records. Everything after that is shared and format-blind: one normalized record model, one index, one clock model, one set of tools.

```
 files / zips / bugreports
        │
  ┌─────▼──────┐   sniff + parse (per format, small)
  │ connectors │   logcat · bugreport · dmesg · DLT · tombstone/ANR · regex-YAML
  └─────┬──────┘
        │ LogRecord stream
  ┌─────▼──────────────────────────────┐
  │ shared core                        │
  │  normalize → template → redact     │
  │  SQLite + FTS5 index per case      │
  │  clock model + alignment           │
  │  detectors (YAML rules)            │
  └─────┬──────────────────────────────┘
        │ ~10 MCP tools, capped outputs, citations log:<src>#<seq>
  ┌─────▼──────┐
  │ Copilot CLI / Copilot SDK app │  (+ aaos-search for code)
  └────────────┘
```

Why this split:

- **The agent learns one tool set.** AGENTS.md and skills stay the same when a new format arrives. Cross-source questions ("what happened in DLT when logcat shows the crash?") only work if all sources share fields and a timeline.
- **Connectors stay tiny.** A new format is a `sniff()` and a `parse()` that yields records, usually 100–300 lines. Simple text formats need no code at all: a YAML regex connector covers them.
- **Format-specific detail is not lost.** Fields that only one format has (DLT ECU/app/context ids, logcat uid, kernel facility) go into typed optional columns or `attrs`, and every filter can use them.
- **The server stays deterministic.** It parses, indexes, searches, counts and aligns. It never calls a model itself; summarizing and reasoning are Copilot's job. That keeps it inside the AI usage levels (see below) and makes every result reproducible.

Only add a format-specific tool when a format has an operation the common tools cannot express (example: decoding DLT non-verbose payloads with a FIBEX file is a connector option, not a tool).

## Concepts

- **Case**: a named set of sources for one defect (e.g. `JIRA-4711`). One SQLite file per case, so cases are isolated, easy to delete, and match the Argus case idea.
- **Source**: one logical log stream. A bugreport zip expands into several sources (main, system, events, crash, kernel, …) with a shared `origin`.
- **Record**: one log entry, normalized (next section).
- **Segment**: a stretch of one source between reboots or clock resets. Each record belongs to exactly one segment, so "after the second boot" is expressible.
- **Artifact**: a document that is not a line stream: tombstone, ANR trace, Java crash report, dropbox entry, `dumpsys` output, kernel panic (pstore/ramoops), coredump. Stored whole with parsed key fields and placed on the timeline as one event (next sections).
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
| `level` | enum | `V D I W E F` (DLT verbose→V, debug→D, info→I, warn→W, error→E, fatal→F; kernel 0–2→F, 3→E, 4→W, 5–6→I, 7→D) |
| `tag` | text | logcat tag, DLT `app/ctx`, kernel subsystem prefix if parseable |
| `pid`, `tid`, `uid` | int | null if the format has none |
| `process` | text | from bugreport process list or `ps` section when available |
| `ecu`, `app_id`, `ctx_id` | text | DLT only |
| `buffer` | text | logcat buffer (main, system, events, crash, radio, kernel) |
| `msg` | text | message, after redaction |
| `template_id` | int | template of `msg` |
| `attrs` | json | anything else the connector knows (DLT message type, session id, kernel facility, …) |

Source metadata (one row per source): format, connector version, file path and hash, record count, time range per clock, segments, detected timezone, build fingerprint and device info when found (bugreport header, `ro.build.fingerprint` lines). The fingerprint feeds `resolve_version` in aaos-search, so the code the agent reads matches the build in the log.

## Artifacts: tombstones, ANRs, crash reports, dumps

Bugreports and DLT traces usually carry more than log lines. These are often the key evidence, and they do not fit the line model: a tombstone is one event with a structured body, and a `dumpsys` section is a snapshot of state at one moment. Forcing them into lines loses the structure, and leaving them out loses the evidence. So they are a second entity next to records.

**What counts as an artifact, and where it comes from**

| Type | Typical location | Parsed key fields |
| --- | --- | --- |
| `tombstone` (native crash) | bugreport `FS/data/tombstones/tombstone_NN` (text, and `.pb` on Android 12+) | time, pid/tid, process, build fingerprint, signal and code, fault address, abort message, top frames with library + offset, `Cause:` line |
| `anr` | bugreport `FS/data/anr/anr_*`, `traces.txt`, ANR section in `dumpsys activity` | time, process, pid, reason (`Input dispatching timed out`, `Broadcast of …`), main thread state and stack, threads it waits on (lock holder), binder peer |
| `java_crash` | crash log buffer, dropbox `data_app_crash`, `system_server_crash` | time, process, pid, exception class, message, top app frames, `Caused by` chain |
| `dropbox` | bugreport `FS/data/system/dropbox/*` (e.g. `SYSTEM_TOMBSTONE`, `data_app_anr`, `system_server_watchdog`, `SYSTEM_BOOT`, `SYSTEM_LAST_KMSG`) | tag, time, body type; body passed to the matching parser above |
| `kernel_panic` | pstore / `last_kmsg` / `console-ramoops`, dropbox `SYSTEM_LAST_KMSG` | panic reason, last kernel lines; also indexed as kernel records of the previous boot (segment) |
| `dumpsys` | bugreport `DUMPSYS` sections per service (`car_service`, `activity`, `meminfo`, `package`, `media.audio_flinger`, …) | service name, dump time, size; body split into subsections by headings |
| `dumpstate_section` | other bugreport sections (`PROCESSES AND THREADS`, `MEMORY INFO`, properties, `CPU INFO`) | section name, dump time; properties and process list are also used as metadata (fingerprint, timezone, pid → process name) |
| `dlt_file` | DLT file transfer messages (`FLST/FLDA/FLFIN`), e.g. a coredump or crash report sent over DLT | file name, size, serial, time; reassembled to a file and parsed if it is a known type |
| `coredump` | `.core` / `.gz` files in a bugreport or DLT file transfer | metadata only (process, signal, size, time); not analysed by the server, pointer for offline `gdb`/`lldb` |

**Model**

| Field | Notes |
| --- | --- |
| `artifact_id` | citation `art:<id>`, sections `art:<id>/<section>`, lines `art:<id>:L<line>` |
| `type`, `origin` | as above; `origin` points to the bugreport or DLT source it came from |
| `ts`, `ts_conf` | event time on the common timeline (tombstone `Timestamp:` line, ANR time, dump time) |
| `pid`, `process`, `tid` | when known |
| `summary` | parsed key fields as JSON (signal, abort message, top frames, exception, ANR reason, blocked-on) |
| `signature` | normalized crash signature: type + process + exception/signal + top 3 own frames without offsets; used to group repeats and to match known issues |
| `body` | full text after redaction, with section index; stored once, FTS-indexed per section |

**How artifacts connect to log lines**

- Each artifact also creates one synthetic record in its origin source (`tag=artifact`, message = one-line summary, `attrs.artifact_id`), so `search`, `context`, `timeline`, `count` and `detect` see it at the right time without special cases.
- Detector rules can link artifacts to lines: a `FATAL EXCEPTION` line in logcat, the `crash` buffer entry, the dropbox entry and a tombstone for the same pid and time window are merged into one crash event with all citations.
- Signatures group repeats ("this native crash happened 7 times in 3 boots") and are the key for matching known issues in the KB later.

**Tools for artifacts** (added to the tool table below)

- `list_artifacts(case, type?, filter?)`: id, type, time, process, one-line summary, signature, repeat count. P0, because a bugreport often holds the answer in a tombstone or ANR trace.
- `read_artifact(case, id, section?, start?, end?)`: parsed summary first, then the requested section or line range, capped like everything else. P0. For `dumpsys`, the section list comes first and the agent reads only the service it needs (a full `dumpsys` is easily several MB).

Symbolization of native frames (library + offset to function and line) needs unstripped symbols for the exact build. That is out of scope for the POC; the summary keeps library and offset, and aaos-search can find the library's source. Later, a `symbolize` step can use the build's symbol archive if one is reachable.

## MCP tools

Fourteen tools in three priority tiers (table below). All take `case`, all outputs are capped (default 50 lines, about 8 KB) and say when a cap was hit and how to narrow. Every returned line carries a citation `log:<source>#<seq>`, the log equivalent of `path:line`.

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

Times accept absolute values, `+30s`/`-2m` relative to a reference, or `seq` references (`"from": "log:2#18233-5s"`).

Tools in priority order. **P0** is the smallest set that already answers a single-log defect question; build it first and try it on real tickets before adding more. **P1** makes the agent cheaper and faster on big logs. **P2** is for multi-source cases and final answers.

| Prio | Tool | What it does | Why at this priority |
| --- | --- | --- | --- |
| **P0** | `add_source(case, path, format?, label?, tz?, options?)` | Registers a file, folder or zip; sniffs the format if not given; indexes in the background. Returns source ids. | Nothing works without it |
| **P0** | `list_sources(case)` | Sources with format, record count, time range, segments, index progress, build fingerprint. | First call of every session; fingerprint links to the right code version |
| **P0** | `search(case, query, mode=text\|regex, filter?, collapse=true, count_only?, limit, cursor)` | FTS5 or regex over `msg` and tag. `collapse` folds repeated templates into one line with count and first/last time. | Finds the error; collapse is the main token saver |
| **P0** | `context(case, at, before=20, after=20, scope=source\|pid\|all)` | Lines around a record or a time. | Reading around a hit is most of the analysis |
| **P0** | `list_artifacts(case, type?, filter?)` | Tombstones, ANRs, crash reports, dropbox entries, dumps, with time, process, summary and signature. | Crash evidence often sits in these, not in logcat |
| **P0** | `read_artifact(case, id, section?, start?, end?)` | Parsed summary, then a section or line range of the artifact. | Read a tombstone backtrace or one `dumpsys` service |
| **P0** | `get_records(case, ids)` | Full records for up to 20 citations (other tools cut messages at about 200 characters, marked `…`). | Stack traces and long payloads |
| P1 | `overview(case, filter?)` | Counts per level, top tags/processes/templates, reboots, gaps, error bursts, coarse histogram. | Saves several blind searches on a big log |
| P1 | `detect(case, rules?, filter?)` | YAML rules: Java/native crash, ANR, watchdog, LMK, SELinux denials, binder failures, VHAL errors, service restarts, DLT ECU resets. | Turns team know-how into one call; rules grow in the KB |
| P1 | `count(case, by=tag\|level\|template\|pid\|source, filter?, bucket?)` | Aggregates, optionally as a time series. | "How often, since when?"; until then `search(count_only)` covers simple cases |
| P2 | `timeline(case, from, to, filter?)` | Merged, time-ordered records across sources. | Only useful once there are several aligned sources |
| P2 | `align(case, source, method=auto\|anchor\|offset, …)` | Puts a source on the common timeline; returns offset, drift, confidence. | Needed with DLT + logcat together; P0 logs from one bugreport share a clock |
| P2 | `diff(case, a, b, filter?)` | Templates only in A, only in B, and large count changes. | Strong for good-vs-bad boot, but needs a reference log |
| P2 | `check_citations(case, markdown)` | Rejects citations to records that don't exist; warns when nothing is cited. | Matters for final reports and the frontend; the skill rule covers the pilot |

The P0 set is seven tools, which keeps the first build to about a week and a half and gives the agent few choices to get wrong. Add P1 tools when the call log shows the agent doing many searches just to get oriented.

Rules for all tools (several taken from dlt-mcp-server, see below):

- `search` has `count_only=true`, so the agent can size a query before pulling lines.
- Empty results say why: "no match", "offset beyond result set", or "query looks like a regex but mode=text".
- Every timestamp printed in an output can be pasted back as `from`/`to` unchanged, so the agent never converts time formats itself.
- Invalid parameters (level out of range, `from` after `to`) fail with a clear message instead of an empty result.

Deliberately not tools:

- **No `summarize` tool.** `overview`, `count` and `detect` give the agent compact facts; the summary is written by Copilot with citations. A server-side summary would hide which lines it used and would be an extra AI call to govern.
- **No "read the whole log".** Raw access is always a window (`context`, `timeline`) with a cap.

Output format, chosen to be short for the model and clickable for a frontend:

```
log:2#18233  10-11 08:12:03.412  E 1234/1250 HvacCtrl  setProperty failed, status=NOT_AVAILABLE   ×37 (08:12:03–08:14:51)
log:5#90211  10-11 08:12:03.398~ W HU1/HVAC/CTRL        CAN timeout on node 0x2A
```

`~` marks a timestamp that is `anchored` or `estimated` rather than exact.

## Ideas taken from dlt-mcp-server

[Ulenspiegel/dlt-mcp-server](https://github.com/Ulenspiegel/dlt-mcp-server) (reviewed Nov 8, 2026 at `c4ed7b7`) is a DLT Viewer plugin in C++/Qt, about 4.3k lines, MPL-2.0, mainly one author, active Jun–Aug 2026. It serves six tools over HTTP on `localhost:9500` from inside a running DLT Viewer: `get_log_summary`, `search`, `get_messages`, `get_selection`, `set_report` and report history. It confirms the shape of our API; its tools are a DLT-only subset of ours.

Taken into this design:

1. **Preview vs full record.** `search` truncates payloads and marks the cut; `get_messages(ids)` returns up to 20 full messages. Our `get_records` copies this.
2. **Count before fetch.** `search(count=true)`.
3. **Timestamps that round-trip.** Each output line carries relative time from log start (`@12.345678`), which is accepted as a filter input as is, plus ECU uptime (`T…`). Our lines show the common-timeline time and accept it back.
4. **Helpful empty results and regex warning**, strict parameter validation, hard limits (100 lines, 20 full messages).
5. **Domain context in the summary.** A configured context file (e.g. an APID/CTID glossary) is appended to `get_log_summary`. For us: `overview` returns pointers to the matching KB notes (e.g. `notes/dlt-apids.md`) instead of a free text file, so the glossary is reviewed like other notes.
6. **Citation validation for reports.** `set_report` takes Markdown with `jump://msg/<id>` links and rejects malformed links and ids that do not exist, and warns when a report has no links at all. We add this as `check_citations(case, markdown)` in the server, used by the frontend and by the triage skill before an answer is final. It enforces the "every claim cited" rule from the Argus evaluation mechanically.
7. **Reports bound to evidence.** Saved reports are matched to the loaded files by path and message count, so reopening the log shows the earlier analysis. For us the case database holds the reports.
8. **"What am I looking at".** `get_selection` returns the line the engineer selected in the viewer. Worth adding to our frontend later (selected line passed as session context), not to the MCP server.

Not taken:

- **Running inside DLT Viewer.** It needs DLT Viewer built from source with Qt and Boost, works only while the GUI is open, and has one file set and no logcat, so cross-source correlation is out of reach.
- **In-memory trigram Bloom index** (blocks of 500 messages). Good for a GUI plugin; our SQLite FTS5 index persists per case and needs no tuning.
- **Literal substring search only**; we keep regex mode.
- **No authentication** on the HTTP port; fine for localhost, not for a shared server.

Worth using directly:

- **As the DLT tool for the pilot, before our DLT connector exists.** Engineers who already open traces in DLT Viewer can connect Copilot CLI to it over HTTP and get DLT search today. Our server then covers logcat/bugreport/kernel in parallel.
- **Its decoding path for our DLT connector.** The plugin gets payloads after DLT Viewer's decoder plugins have run (non-verbose/FIBEX, decryption), which is the hard part of DLT. Our connector could import text exported by DLT Viewer with those decoders on (its command-line conversion mode, to be verified) instead of decoding non-verbose DLT itself.

Licence: MPL-2.0 is file-level copyleft. Copying ideas and API shape is free; copying code files means keeping those files under MPL-2.0. We need no code from it.

## Connector interface

Python, same language as aaos-search.

```python
@dataclass
class SourceContext:
    path: str
    label: str | None
    tz: str | None            # from user, bugreport or config
    year_hint: int | None     # logcat threadtime has no year
    options: dict             # e.g. {"fibex": "path/to/ecu.xml"} for DLT

class Connector(Protocol):
    name: str                 # "logcat", "dlt", ...
    version: str              # stored per source; bump to force reindex

    def sniff(self, head: bytes, filename: str) -> float: ...
        # confidence 0..1 from the first ~64 KB

    def parse(self, stream: BinaryIO, ctx: SourceContext) -> Iterator[RawRecord]: ...
        # streaming; never reads the whole file into memory
        # yields: seq, offset, ts_raw, clock, ts_src, level, tag, pid, tid, uid,
        #         ecu, app_id, ctx_id, buffer, msg, attrs

    def metadata(self) -> dict: ...
        # after parse: fingerprint, device, timezone, segments, clock anchors found

class Container(Protocol):    # zip, tar, gz, bugreport
    def expand(self, path: str) -> Iterator[tuple[str, BinaryIO, str | None]]: ...
        # (member name, stream, suggested connector)
```

The core does everything else: normalizing levels, template extraction, redaction, segment detection, batching inserts, FTS indexing.

**Connectors for the POC and later**

| Connector | Phase | Notes |
| --- | --- | --- |
| `logcat` | 1 | `threadtime` (default), `time`, `brief`, `long`, plus `-v year`, `-v epoch`, `-v monotonic`, `-v uid`, `-v UTC/zone`. Handles `--------- beginning of main` buffer markers and multi-line messages (stack traces continue the previous record). |
| `bugreport` (container) | 1 | Zip or `.txt`: splits sections (`SYSTEM LOG`, `EVENT LOG`, `KERNEL LOG`, `crash`, `DUMPSYS`, other dumpstate sections), reads fingerprint, uptime, timezone and the process list; sends `FS/data/tombstones`, `FS/data/anr`, `FS/data/system/dropbox` and pstore files to the artifact parsers. |
| `kernel` | 1 | `dmesg` (`[  12.345678]` monotonic), `/proc/kmsg` / `dmesg -r` (`<6>` priority), and `dmesg -T`. |
| `regex-yaml` | 1 | Generic: a YAML file per format with a line regex, named groups mapped to record fields, timestamp format and clock. Covers vendor service logs, QNX `slog2info` text output, test-runner logs without code. |
| `tombstone`, `anr`, `java_crash` (artifact parsers) | 1 | Text tombstones and ANR traces first; tombstone `.pb` (Android 12+) via the protobuf schema from AOSP if text is missing. |
| `dropbox`, `dumpsys`, `kernel_panic` (artifact parsers) | 1 | Dropbox by tag, body handed to the matching parser; `dumpsys` split per service and subsection; pstore as kernel records of the previous boot plus a panic artifact. |
| `dlt_file` (artifact parser) | 2 | Reassembles DLT file transfers; parses known types, stores coredumps as metadata only. |
| `dlt` | 2 | Storage format (`DLT\x01` header): storage timestamp (wall), ECU timestamp (0.1 ms ticks), ECU/app/context ids, verbose arguments decoded. Non-verbose needs a FIBEX file passed in `options`. Parsing is done by ESR Labs `dlt-core` (Rust, Apache-2.0, about 400 MB/s, FIBEX support) in a small sidecar binary `dlt2jsonl`; the Python connector reads its JSON lines. ARXML-only setups would need a conversion to FIBEX or DLT Viewer's export. |
| later | — | Jira attachment fetch (container), CAN traces (`.asc`/`.blf`) if bus timing matters, perfetto/systrace summaries. |

Example `regex-yaml` connector:

```yaml
name: hvac-service-log
sniff: '^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} \[(DBG|INF|WRN|ERR)\]'
line: '^(?P<ts>\S+ \S+) \[(?P<level>\w+)\] (?P<tag>[\w.]+): (?P<msg>.*)$'
continuation: '^\s'            # lines starting with whitespace belong to the previous record
timestamp: { format: '%Y-%m-%d %H:%M:%S.%f', clock: wall_local }
levels: { DBG: D, INF: I, WRN: W, ERR: E }
```

These YAML files and the detector rules live in the knowledge repo (`aaos-kb/logs/`), so they are reviewed like notes and grow with the team.

## Large files

Logcat from a long test drive is easily 0.5–2 GB; DLT traces from all ECUs can be several GB. The design keeps both memory and tokens flat.

- **Stream, never slurp.** Connectors are generators over a buffered stream; gz/zip members are decompressed on the fly. `offset` is tracked per record so the raw line can always be shown.
- **Index once, query many.** Records go into SQLite in batches of ~50k in one transaction with WAL mode; FTS5 (external-content table over `msg` and `tag`) is built in the same pass. Expect roughly 100–300k lines/s on a normal machine, so 1 GB of logcat (~8–10 M lines) indexes in about one minute. Measure this on a real trace in the POC.
- **Indexing is a background job.** `add_source` returns immediately; `list_sources` shows progress, and queries over a source still indexing work on what is done so far and say so.
- **Dedup by template.** Template extraction is cheap (regex masking) and gives a `templates` table with counts. `collapse=true` turns 40k identical lines into one; this is the biggest token saver.
- **Hash-based reuse.** Sources are keyed by file hash plus connector version, so loading the same bugreport into a second case reuses the index.
- **Hard caps everywhere.** Line count, byte budget and a cursor for paging. When a query would return too much, the tool returns counts by tag/template instead and suggests filters.
- **When SQLite stops being enough** (tens of GB per case), swap the store for DuckDB over Parquet behind the same core interface. Not needed for the POC.

## Time alignment

Each format speaks a different clock, so alignment is a first-class step, not a guess the model makes.

| Source | Clock as written | Problem |
| --- | --- | --- |
| logcat threadtime | local wall time, no year, no zone | year and zone unknown; jumps when the clock is set (NTP, GPS, RTC) |
| logcat `-v monotonic` | seconds since boot | needs boot time to map to wall |
| kernel dmesg | monotonic since boot | same; resets every boot |
| DLT | storage time (logger's wall clock) and ECU ticks since ECU start | each ECU has its own start; logger clock may differ from the head unit's |
| vendor logs | anything | declared in the YAML |

**Model.** Each source segment gets a mapping `ts = a · ts_src + b` (offset `b`, drift `a`, usually 1). `ts` and `ts_conf` are recomputed when the mapping changes; the raw timestamp never changes.

**Automatic anchors, tried in order:**

1. **Same clock already.** Sources in one bugreport share the device's clocks; bugreport header gives timezone and year, and the `uptime`/boot time lets monotonic kernel lines map to wall time (`exact`).
2. **Paired timestamps in one record.** Logcat with both wall and monotonic, kernel lines with Android-time printk, DLT records with both storage time and ECU ticks: fit `a, b` by least squares per ECU and segment (`exact` or `anchored`).
3. **Shared events.** Known boot markers in both sources (kernel `Booting Linux`, `init: starting service`, `boot_completed`, DLT ECU startup messages, a custom marker line the team adds to test scripts): match them and take the offset (`anchored`).
4. **Wall clock jumps** inside a logcat (e.g. RTC at 2000-01-01 until GPS/NTP sets the time) start a new segment; the segment is aligned from monotonic lines around the jump.

**Manual fallback.** `align(method=anchor, a="log:2#18233", b="log:5#90211")` says these two records are the same moment; `align(method=offset, seconds=-3.2)` for a known skew. The result is stored per segment and shown in `list_sources`.

**Honesty rule.** Every cross-source answer reports alignment confidence. AGENTS.md says: do not claim "A caused B" from order alone when either side is `estimated`, or when the gap is smaller than the alignment error.

Missing year and zone: take them from the bugreport, then from the case's `tz` setting, then from file mtime, in that order, and mark the source `estimated` if none of those exist.

## Redaction and AI usage levels

Logs hold more personal data than code: VINs, IMEI/serials, MAC addresses, GPS coordinates, phone numbers, account emails, Wi-Fi SSIDs. Redaction runs in the core at index time, so no tool can return the raw value.

- Rules are a reviewed YAML list of regexes with replacement tokens (`<VIN>`, `<GPS>`), stored with the policy file. Redacted values keep a stable hash suffix (`<VIN#a3f1>`) so the agent can still see that two lines mention the same vehicle.
- The original file stays on disk; an engineer can open it locally without AI.

Mapped to the proposed AI usage levels:

| Level | Log server behaviour |
| --- | --- |
| 0 search only | Tools are usable from a CLI/web page by engineers, no model attached. |
| 1 cited read-only answers | Copilot may call all tools; outputs redacted and capped; every call logged (case, tool, args, line count, bytes) to JSONL. |
| 2 drafts to inbox | Agent may write a case summary or a new detector/regex-YAML proposal into `inbox/`; human review. |
| 3+ | Out of scope for the log server; Jira/Gerrit write-backs belong to the frontend. |

The policy file can also block sources (e.g. "no DLT from ECU X") and cap the bytes per session.

## Working together with aaos-search

The defect flow crosses both servers; they stay separate MCP servers that the agent uses together.

1. `list_sources` → fingerprint → aaos-search `resolve_version` → the analysed release.
2. `detect` / `search` find the failing line and its template.
3. aaos-search `search_code` with the template's literal part (`"setProperty failed, status="`) and the tag finds the log statement in code, at that version.
4. `context` and `timeline` around the hit; `diff` against a good log if one exists.
5. The answer cites both `log:<src>#<seq>` and `path:line`, with each finding marked hypothesis or confirmed.

To make step 3 easy, `search` results include the template literal ready to paste into a code query. The triage skill in the KB spells out this order.

## Transport and deployment

- **POC: local stdio**, started by Copilot CLI from `~/.copilot/mcp-config.json`. Logs stay on the engineer's machine, no server or upload needed, and the case databases sit in `~/.aaos-logs/cases/`.
- **Later: HTTP**, same code (the Python MCP SDK serves both), behind the Copilot SDK frontend, with cases on a shared server when logs come from Jira or test benches.
- Python 3.11+, stdlib `sqlite3` (FTS5 is in standard builds), `mcp` SDK. No other heavy dependency for phase 1.

## POC plan

Rough efforts for one person.

**Phase 1 (about 1.5–2 weeks): logcat-first defect analysis**

1. Core: record model, SQLite schema with FTS5, batch writer, template extraction, redaction, call log (3 days).
2. Connectors: `logcat`, `bugreport` container, `kernel`, `regex-yaml` (3 days).
3. P0 tools (add_source, list_sources, search, context, get_records) with caps, cursors and citations (2–3 days); then P1 tools overview, detect and count (2 days).
4. Detector rules for `detect`: 8–10 YAML rules for crashes, ANR, LMK, SELinux, binder, watchdog (1 day).
5. Triage skill + AGENTS.md rules for logs; try on 5 closed defects with known root cause (1–2 days).

**Phase 2 (about 1.5 weeks): multi-source**

6. DLT connector (verbose first; for non-verbose, import DLT Viewer's decoded export). Until then, pilot users can use dlt-mcp-server for DLT.
7. P2: `align` with automatic anchors 1–3 and manual fallback; `timeline` across sources.
8. P2: `diff` by template; tombstone/ANR connectors; `check_citations` for final answers.

**Done when** the 5 (then 10) historical defects get an analysis where the engineer agrees with the root-cause direction, every finding cites log lines that open correctly, and tokens per case stay within budget (track from the call log, same as the Q&A MVP).

## Open questions

- Which log types come with a typical ticket today: bugreport zip, plain logcat, DLT from the whole car, vendor logs? This sets the connector order.
- DLT: verbose or non-verbose? Are FIBEX/ARXML files available, and may they be used?
- Typical and worst-case sizes per ticket.
- Where do logs live (Jira attachments, test bench share, engineer laptops)? Decides when HTTP and a Jira connector are needed.
- Redaction list: which fields compliance requires to be masked before anything reaches Copilot.
- 5–10 closed defects with known root cause for the evaluation set.
