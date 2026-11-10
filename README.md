# automotive-log-mcp

An MCP server that lets any agent analyse a defect from automotive logs (Android bugreport,
logcat, kernel logs), with every finding cited to a log line or artifact.

Status: **M5 multi-source (without DLT)**. Logcat (all `-v` formats), kernel logs (`dmesg`,
`/dev/kmsg`), Android bugreports (zip or text), archives (zip, tar, gz), YAML-defined text
formats and `command` connectors (an external decoder writing JSON lines, for company-specific
formats) are indexed into one SQLite DB per case, with redaction at index time. Tombstones, ANR
traces, Java crashes and dropbox entries become artifacts with a summary, a crash signature and a
record on the timeline. All sources of a case are aligned on one case clock (same lines in two
sources, UTC times in kernel lines, marker rules, the bugreport boot time, manual anchors).
Thirteen tools work over MCP: the P0 tools `add_source`, `list_sources`, `search`, `context`,
`list_artifacts`, `read_artifact`, `get_records`, the P1 tools `overview`, `count`, `detect` (10
default detector rules, more from rule directories) and `check_citations`, and the M5 tools
`timeline` and `align`, all with capped, cited output. An evaluation harness ([eval/](eval/))
checks evidence recall without an agent and gives a checklist for agent runs. Everything is
verified on synthetic fixtures and synthetic evaluation cases only, not yet on real bugreports or
real closed defects; ingest (about 29k lines/s on a generated 200 MiB log) misses the M1 target of
100k lines/s; the case DB (schema 5) is 1.34× the input there, within the 1.5× target: record
messages are kept compressed in blocks, not as a column (see [Storage](#storage)). A case DB of an
older schema is migrated on first open, without a re-import (7.5 s for 1.9M records). DLT is not
read (not part of this M5; a company DLT setup can use a `command` connector or
[docs/dlt.md](docs/dlt.md)). See [docs/design.md](docs/design.md),
[docs/implementation-plan.md](docs/implementation-plan.md) and
[docs/connectors.md](docs/connectors.md) (own formats and clock alignment).

## Install

Python 3.11+ whose `sqlite3` module includes FTS5 (the server checks at startup).

```bash
pip install -e ".[dev]"
```

## Run

```bash
automotive-log-mcp serve
```

The server speaks MCP over stdio. Client configuration snippets are in
[examples/mcp-config/](examples/mcp-config/). Tool results are text only by default; `serve
--structured` (or `structured_output: true` in the config) adds `structuredContent` and output
schemas, at a token cost (see [Token use](#token-use)). `add_source` indexes in a background thread per
case: it waits up to 10 s, then reports that indexing continues; `list_sources` shows progress.

**A file is indexed once.** Adding a file that is already indexed (same content, by SHA-256) is a
no-op, also under another case name: then `add_source` names the case that holds it and the agent
uses that one (a new chat often picks a new case name for the same bugreport). `reindex=true`
(MCP) or `index --force` (CLI) indexes it again anyway. An import that did not finish, because
the server was stopped while indexing in the background (chat or VS Code closed), is started
again from the beginning next time; the reply says so. To avoid that for large files, index
them once before the chat and tell the agent the case name:

```powershell
py -m automotive_log_mcp index T-1 C:\logs\bugreport.zip     # Windows; waits until done
```

## Tools

| Tool | What it returns |
| --- | --- |
| `add_source(case, path, format?, reindex?)` | indexes a file, directory or zip (background thread; waits up to 10 s) and the new source ids; a file already indexed, in this or another case, is not indexed again |
| `list_sources(case)` | sources with format, record count, time range, fingerprint, stale/missing flags |
| `search(case, query, mode, filter, collapse, count_only, limit, cursor)` | matching records in time order; FTS5 words or a regex over message and tag |
| `context(case, at, before, after, scope)` | lines around `log:<s>#<n>` or an artifact's record `art:<id>`; `scope='pid'` follows one process through all buffers of the same file, merged by time; `scope='all'` shows every source on the case clock |
| `list_artifacts(case, type?)` | tombstones, ANRs, Java crashes, dropbox entries grouped by crash signature, in time order; copies of one crash marked `same crash as art:<id>` |
| `read_artifact(case, id, section?, start_line?, end_line?)` | summary, signature and section list; then a section or a line range, lines numbered `L<n>` |
| `get_records(case, ids)` | full records: untruncated message, timestamp as written, attributes, template text |
| `overview(case, filter?)` | records and levels, top tags, pids and messages (templates), clock segments, gaps over 30 s, error bursts, a histogram, detector and artifact counts |
| `count(case, by, query?, mode, filter?, bucket?, limit)` | records by `tag`, `level`, `pid`, `source` or `template`, optionally matching a query, optionally per time bucket (`'1m'`, at most 60 buckets, empty ones shown) |
| `detect(case, rules?, filter?, examples)` | hits of the detector rules by severity and time, collapsed by message (or pid), each with a hint what to read next |
| `timeline(case, filter?, query?, limit, cursor)` | records of all sources merged on the case clock in time order; `~` marks times aligned from another clock; `from`/`to` are case-clock times or citations |
| `align(case, mode, source?, seg?, offset?, at?, ref?)` | how each source maps onto the case clock (confidence, offset or boot time, error, what it rests on); `mode='anchor'`/`'offset'`/`'reference'`/`'reset'` set it by hand |
| `check_citations(case, text)` | checks a Markdown answer: every `log:`/`art:` citation must be well-formed and resolve (errors); uncited paragraphs and quotes not in the cited lines are warnings; `PASS`/`FAIL` |

A search hit looks like the design's line format; collapsed repeats add a count and the last
occurrence, and hits that belong to an artifact name it:

```
log:2#18233  10-11 08:12:03.412  E 1234/1250 HvacCtrl  setProperty failed, status=NOT_AVAILABLE  ×37 until 10-11 08:14:51.003 (log:2#19001)
log:3#1  01-01 00:00:01.000  E 1234/1250 AndroidRuntime  FATAL EXCEPTION: main ⏎ Process: … → art:1
```

- **Caps** (`caps` in `config.yaml`): `max_lines` 50, `max_bytes` 8192 of text per result,
  `max_msg_chars` 200 (cut with `…`; `get_records` has the full text), `regex_scan_rows`
  1,000,000 candidate records per regex search. `read_artifact` shows up to twice `max_lines`.
  A capped result ends with `… N more …; next page: cursor="…"; or narrow by level (E 120, W 900),
  tags (…), from/to (…)`.
- **Collapse** groups matches with the same source, template, tag and level (templates mask
  numbers, hex, ids and paths) and lists the groups by first occurrence.
- **Code search**: search hits carry their template and its longest constant text (`"setProperty
  failed, status="`), ready for a search in the source code.
- **Text vs regex**: text mode is FTS5 (whole words, case-insensitive, `"phrases"`, `prefix*`,
  `AND`/`OR`/`NOT`); a text query that looks like a regex gets a warning. The parts of CamelCase
  names are words too: `eso` or `Delegate` finds `EsoCarDelegateDisplayAreaPolicyProvider`, and a
  CamelCase query word also matches its parts in a row (`DisplayArea` is searched as
  `(DisplayArea OR "display area")`). Regex mode is Python
  `re`, case-sensitive; literal parts of the pattern prefilter in SQL (FTS and `instr`), then at
  most `regex_scan_rows` candidates are scanned (a capped scan says so and counts are lower
  bounds).
- **Empty results say why**: a word that occurs nowhere, or which filter part removes the matches
  (`3 matches in the case, none with the filter: 3 without the level filter (level F)`).
- **A sources filter says what it leaves out**: `Outside the sources filter: 3,120 more matches
  (log:4 3,108, log:2 12)` (text queries; a regex search names the sources it did not search).
- **No query lists the filter**: `search` with `query` `''` or `'*'` returns every record that
  passes the filter, e.g. all lines of a tag or of a time window, collapsed or not.
- **Structured content** holds the same items as the text (citations, full timestamps, counts,
  templates), with a declared output schema per tool.
- Raw values in queries and filters (a MAC, a VIN) are redacted like the logs, so they match
  their tokens.

### P1 tools

- **overview** is the first call after `list_sources`: where the errors are and what to look at.
  Gaps are stretches of more than 30 s without any record (wall clock); an error burst is a run of
  10-s buckets whose E/F count is above 5× the median bucket and above 10. The histogram has up to
  60 buckets of a round size; the text shows it as two sparklines (all records, E/F), the
  structured content has the numbers. Each item comes with an example citation.
- **count** without a query groups every record that passes the filter; with `query` it counts
  matches (as `search` would). Broad text queries (over 300k matches) are counted from every k-th
  match and marked `≈`; a regex scan that hits `caps.regex_scan_rows` gives lower bounds
  (`at least`). Bucket starts are printed so that they can be passed back as `from`.
- **detect** runs the rules below (or `rules=[...]`); `filter` narrows the records the rules see
  (a second event of a `followed_by` rule is looked for beyond `to`). The `search` hint of a rule
  lists every hit.
- **check_citations** is what the `defect_triage` prompt calls before an answer is final. Citations
  inside fenced code blocks are not checked; paragraphs that say they are a guess ("probably",
  "unknown", "the logs do not show", …) or are shorter than 8 words need no citation. The call log
  keeps only the text's length and hash.

### Multi-source tools (M5)

- **timeline** merges every source (bugreport buffers, kernel logs, logs of other devices and
  ECUs) on one case clock. Times of the reference clock print as written; times mapped from
  another clock print with `~`, and the footer says how each was aligned and how far it can be
  off. Records of a clock without an anchor are left out and named. Filters as in `search`; a
  text `query` narrows it.
- **align** shows the alignment and sets it by hand; how sources are aligned automatically (and
  marker rules for company logs) is in [docs/connectors.md](docs/connectors.md#clock-alignment-m5).
  The mapping is stored in the case DB and recomputed after every import; records keep their
  times as written, so the other tools and their `from`/`to` are unchanged.
- **context(scope='all')** shows the lines of every source around an event on the case clock.

An order across sources that rests on `~` times closer than their error is a guess; the server
instructions tell agents to say so.

### Detector rules

| Rule | Severity | Matches |
| --- | --- | --- |
| `java_crash` | high | `AndroidRuntime: FATAL EXCEPTION`, Java crash artifacts |
| `native_crash` | high | `libc: Fatal signal`, the debuggerd `*** ***` banner, tombstones |
| `anr` | high | `ActivityManager: ANR in`, ANR artifacts |
| `watchdog` | high | `Watchdog: Blocked in handler/monitor`, `WATCHDOG KILLING SYSTEM PROCESS` |
| `boot_not_completed` | high | `boot_progress_start` not followed within 3 min by `boot_progress_enable_screen`, `sf_stop_bootanim` or boot completed |
| `lmk` | medium | `lowmemorykiller`/`lmkd: Kill '…'`, kernel `Out of memory: Killed process` |
| `binder_failure` | medium | `!!! FAILED BINDER TRANSACTION !!!`, `DeadObjectException`, kernel `binder: … transaction failed` |
| `vhal_error` | medium | E/F from VHAL tags (`*VehicleHal*`, `*Vhal*`, …), `status=NOT_AVAILABLE` (and `TRY_AGAIN`, `INTERNAL_ERROR`, `ACCESS_DENIED`, `INVALID_ARG`) at W or above |
| `service_restart` | medium | `init: Service '…' (pid N) exited/killed/received signal`, `Process … has died`, `Scheduling restart of crashed service` |
| `selinux_denial` | low | `audit(…): avc: denied`, one line per pid |

Rules are YAML files (format in
[src/automotive_log_mcp/detect/engine.py](src/automotive_log_mcp/detect/engine.py)): `match` (a
regex over message and tag), `level`, `tags`, or `any:` alternatives of these; `followed_by` a
second event `within` a time (`absent: true`: a hit when it does not follow); `group_by: pid`;
`severity`, `description`, `note`. The package rules are in
[src/automotive_log_mcp/rules/detect/](src/automotive_log_mcp/rules/detect/). YAML files in the
directories under `rule_dirs` in `config.yaml` add rules; a rule with the name of a package rule
replaces it (later directories win). `automotive-log-mcp rules` lists the rules in effect and where
each comes from. Rule patterns are redacted like queries, so a raw value in a pattern matches its
token.

### Filter

`sources`, `from`/`to`, `level` (minimum), `tags`/`not_tags` (globs, case-sensitive; an unknown
tag gets a warning with similar tags), `pid`, `attrs` (exact match on format attributes, e.g.
`{"uid": "1000"}` or `{"art": 3}`). `from`/`to` accept:

| Form | Example | Meaning |
| --- | --- | --- |
| as printed in text lines | `01-01 00:00:01.000` | year from the case's time range (an error if ambiguous) |
| as printed in structured content | `2026-01-01 00:00:01.000`, `2026-01-01` | |
| time of day | `00:00:01` | the day is taken from the case if it is unique |
| seconds since boot | `123.456789` | records with only a monotonic clock (kernel) |
| relative | `-5m`, `+30s`, `1h30m` | `-`: before the end of the case; `+`: after its start, in `to` after `from` |
| citation | `log:3#120`, `art:2`, `log:3#120-30s` | that record's time, optionally shifted |

`to` is inclusive to the precision given: `to: "08:14"` includes all of 08:14. Times are the
device's wall clock as written (no time-zone conversion).

## Command line (no agent needed)

```bash
automotive-log-mcp index T-1 ~/bugreports/bugreport-2026-10-11.zip   # file, archive or directory
automotive-log-mcp sources T-1                                       # formats, counts, time ranges
automotive-log-mcp query T-1 --search "setProperty failed" --level W --tag 'Hvac*' --limit 20
automotive-log-mcp query T-1 --regex 'status=\w+_AVAILABLE' --count
automotive-log-mcp query T-1 --level W --from=-5m --collapse            # same output as the search tool
automotive-log-mcp artifacts T-1                                     # tombstones, ANRs, crashes, dropbox
automotive-log-mcp artifacts T-1 --type anr
automotive-log-mcp artifacts T-1 --show art:6                        # summary and section list
automotive-log-mcp artifacts T-1 --show art:6/backtrace              # one section, numbered lines
automotive-log-mcp artifacts T-1 --show art:6/backtrace:L17          # one line (or :L17-L20)
automotive-log-mcp relink T-1 3 /new/place/bugreport-2026-10-11.zip  # original moved; hash must match
automotive-log-mcp reindex T-1 3                                     # index an original again
automotive-log-mcp overview T-1                                      # as the overview tool
automotive-log-mcp detect T-1 --rule anr --examples 5                # as the detect tool
automotive-log-mcp count T-1 --by level --bucket 1m --search "setProperty failed"
automotive-log-mcp check T-1 answer.md                               # check_citations; exit 1 on errors
automotive-log-mcp rules                                             # detector rules and their origin
automotive-log-mcp timeline T-1 --level W --from "01-01 00:00:00"    # all sources on the case clock
automotive-log-mcp align T-1                                         # how each source is aligned
automotive-log-mcp align T-1 --anchor log:8#4=log:1#120              # same moment on two clocks
automotive-log-mcp align T-1 --offset 8=+1.5s                        # or a fixed offset (8:2=... per segment)
```

- `index` options: `--format <connector>` forces a format instead of sniffing; `--force`
  reindexes a file that is already in the case, or indexes it here although another case holds it
  (adding the same file again is otherwise a no-op).
- `query` runs the `search` tool and prints its text: `--search` (FTS5 over message and tag) or
  `--regex` (neither: every record that passes the filter), `--level` (minimum), `--tag` and
  `--not-tag` (glob, repeatable), `--source` (id, repeatable), `--pid`, `--from`/`--to` (forms as
  in [Filter](#filter); write `--from=-5m` for values starting with `-`), `--collapse`, `--limit`,
  `--cursor`, `--count`. Raw values in the query (a VIN, a MAC) are redacted the same way as the
  logs, so they still match.
- Output lines are cited as `log:<source>#<seq>` (artifacts as `art:<id>`), e.g.
  `log:2#18233  10-11 08:12:03.412  E 1234/1250 HvacCtrl  setProperty failed, status=NOT_AVAILABLE`.
- `sources` flags sources whose original file is missing (use `relink`) and sources indexed with
  an older connector or redaction rules (`stale: reindex recommended`).
- `overview`, `detect` and `count` take the same filter flags as `query`; `count --by none` gives
  totals only. `check` reads a Markdown file (`-` for standard input) and exits with 1 when a
  citation is malformed or does not resolve. `rules --show <name>` prints a rule's file.
- `artifacts` lists one line per artifact: citation, type, time, pid, process, signature (in time
  order). `--show` takes a citation `art:<id>[/<section>][:L<n>[-L<m>]]`.

## Artifacts

| Type | From | Parsed |
| --- | --- | --- |
| `tombstone` | `FS/data/tombstones/tombstone_NN` in the zip, `TOMBSTONE` sections, `*_native_crash` dropbox entries | process, pid/tid, thread, signal and code, fault address, cause, abort message, backtrace; sections `header`, `registers`, `backtrace`, `memory`, `memory_map`, `threads`, `open_files`, `log` |
| `anr` | `FS/data/anr/anr_*`, the `VM TRACES AT LAST ANR` section, `*_anr` dropbox entries | subject, main thread state and frames, the chain of lock holders (with deadlock detection), a blocked binder call (interface, method) and its peer process from `BINDER TRANSACTIONS`; one section per process header and per thread (`p<pid>.t<tid>.<name>`) |
| `java_crash` | `FATAL EXCEPTION` records in logcat (the crash buffer), `*_crash` dropbox entries | process, pid, thread, exception, causes, root cause and its frames |
| `dropbox` | other dropbox entries (`*_wtf`, `*_strictmode`, `SYSTEM_BOOT`, …), as files `tag@<ms>.txt[.gz]` or `dumpsys dropbox -p` sections | tag, header fields, first line, a Java stack if present |

- Every artifact has a record on the timeline, so `search`, `context` and `query` find it:
  a synthetic record `[art:<id>] <headline>` in the source it came from (tag = type, level F for
  crashes, E for ANRs, W for other dropbox entries), or, for a crash-buffer crash, the
  `FATAL EXCEPTION` record itself (`attrs.art`).
- **Signature** groups the same crash: `<kind> <process>: <frame> < <frame> < <frame>` (kind =
  signal, exception class of the root cause, or `ANR <main thread state>`; frames without
  arguments, line numbers or offsets; abort machinery in `libc`/`libbase`/`liblog` skipped).
- **Citations** `art:<id>[/<section>][:L<line>]`: line numbers count over the whole artifact, so
  for a file they are the line numbers of the original; a section, when given, must contain the
  line.
- **Times** are the device's wall clock as written, like log records: a tombstone's `Timestamp:`,
  an ANR's `----- pid N at …`, a dropbox entry's time (file names carry UTC milliseconds,
  converted with the bugreport's `persist.sys.timezone`; without one the UTC time is kept and
  `fields.ts_zone` says `UTC`).
- In a bugreport zip the main entry is read first, so the fingerprint, time zone and process list
  apply to the other members; the process list fills a missing process name.
- Not parsed yet (M5): tombstone `.pb`, `dumpsys` sections, kernel panics / `pstore`.

## Storage

One SQLite file per case (`<home>/cases/<case>.db`), schema 4:

- `records`: the parsed fields of each record (time, level, tag, pid, tid, template, attributes),
  without its message. Indexes on time, per-source monotonic time and tag only; filters and counts
  by pid, level or template scan the records the time or tag index selects (or all of them).
- `msg_blocks`: the redacted messages of each source, about 64 KB of text per block, zlib
  compressed. SQL reads a message with `rmsg(id)` (`records_v.msg`); a reader caches decoded
  blocks, so scans in id order decompress each block once.
- `records_fts`: the full-text index (message, tag, CamelCase parts), external content, no copy of
  the text.

Measured on a generated 200 MiB logcat (1.9M records), against schema 3 (message column, five
indexes): DB 423 → 268 MiB (2.11× → 1.34× the input), same tool output. Reading text costs
decompression: regex searches and `detect` take 2–3× longer (`detect` 0.9 → 2.6 s, whole-case
`overview` 2.5 → 4.8 s, regex search 0.5 → 1.1 s), `count by template` 0.1 → 0.6 s; text search and
`context` are unchanged. The generated log repeats few templates, so its text compresses better
(about 5×) than real logs will.

## Formats

| Connector | Input |
| --- | --- |
| `bugreport` | `bugreport-*.txt` (also inside the zip): `SYSTEM LOG` as one source named after its buffers (`main+system+crash`: without `-D`, logcat marks only where each buffer first appears, then interleaves them), split per buffer only when logcat ran with `-D`/`--dividers`, `EVENT LOG`, `KERNEL LOG`, fingerprint, time zone, uptime, process list, binder transactions; ANR, tombstone and dropbox sections go to the artifact parsers |
| `tombstone`, `anr`, `dropbox` | the artifact files listed under [Artifacts](#artifacts) (sniffed by content or name) |
| `logcat` | `threadtime`, `time`, `brief`, `process`, `tag`, `thread`, `long`, `raw`; modifiers `year`, `epoch`, `monotonic`, `usec`, `nsec`, `zone`, `UTC`, `uid`, `printable`, `color`; `beginning of`/`switch to` markers (one source per buffer only with dividers); multi-line messages merged |
| `kernel` | `dmesg` (with or without `<prio>`, caller ids, `-x`, `-T`) and `/dev/kmsg` |
| `archive` | zip, tar (plain, gz, bz2, xz), single-file gzip; nested up to depth 4 |
| `syslog` | BSD syslog / `journalctl -o short` (a YAML connector shipped in the package) |

Own formats ([docs/connectors.md](docs/connectors.md)): YAML regex connectors and `command`
connectors (an external program that writes JSON lines: records, artifacts, source metadata) as
files in a directory listed under `rule_dirs` in `config.yaml` (examples: a DLT Viewer text export
in [examples/rules/](examples/rules/dlt_viewer_ascii.yaml), an MCU trace decoder in
[examples/connectors/](examples/connectors/)), or Python connectors registered as entry points
(`automotive_log_mcp.connectors`), loaded only when the policy sets `allow_code_connectors: true`.
Lines a connector cannot parse are kept as records with no level, never dropped.

## Redaction

Always on, at index time, before templates are built: VINs, IMEIs, MAC and IPv4 addresses, GPS
coordinates, e-mail addresses, phone numbers (`+…`), serial numbers and Wi-Fi SSIDs become tokens
like `<MAC:3fa2c1d0>`. The suffix is a keyed hash (secret in `secret.key`), so the same value
always gives the same token but cannot be recovered. Artifacts (section text, summaries, fields,
signatures, process and section names) and call-log arguments are redacted with the same rules.

## Files

All state lives under `~/.automotive-log-mcp/` (override with `AUTOMOTIVE_LOG_MCP_HOME`):

| Path | What |
| --- | --- |
| `config.yaml` | optional: `cases_dir`, `policy_file`, `call_log` (null disables), `caps` (`max_lines`, `max_bytes`, `max_msg_chars`, `regex_scan_rows`), `rule_dirs`, `structured_output` (default false), `rounds_dir` (default `<home>/rounds`, null disables), `round_gap_minutes` (default 10) |
| `policy.yaml` | optional: `allowed_tools`, `blocked_sources` (globs on paths and archive members: refused by `add_source`, hidden from tools), `blocked_ecus`, `byte_budget`, `allow_code_connectors` |
| `cases/<case>.db` | one SQLite DB per case; originals are referenced by path and SHA-256, never copied |
| `secret.key` | redaction key, created on first run; deleting it changes every token of later imports |
| `calllog.jsonl` | one line per tool call: tool, arguments, status, duration, output bytes, and for token analysis call number, text and structured bytes, estimated tokens, argument hashes, caps hit, result counts and the citation ids in the output; the first call of a session also records the server's fixed cost. Never log content (the text given to `check_citations` is logged as its length, hash and the citation ids it contains) |

| `rounds/<date>_<time>_<session>_r<n>.jsonl` | the call-log lines of one round (see [Token use](#token-use)), and `….txt` next to it: its report, rewritten after every call |

Example `policy.yaml`:

```yaml
allowed_tools: [list_sources, search, context, list_artifacts, read_artifact, get_records]
byte_budget: 500000
```

## Token use

Where an agent session spends its tokens, and what to change, from the call log the server
writes anyway (nothing to switch on).

**One log file per round.** Besides `calllog.jsonl`, the server writes every round to a file of
its own in `~/.automotive-log-mcp/rounds/` (Windows: `%USERPROFILE%\.automotive-log-mcp\rounds\`),
named `<date>_<time>_<session>_r<n>.jsonl` (local time), with a readable report `….txt` next to it
that is rewritten after every call. A round starts with the server and again after
`round_gap_minutes` (default 10) without a tool call: VS Code keeps the server running between
chats, so a pause is the only boundary the server can see; leave a pause, or restart the server,
between questions you want to compare. To have a round checked, hand over its `.jsonl` (and the
`.txt` if you like); for a Claude Code run also the session transcript (below). The files hold
what `calllog.jsonl` holds: tool names, redacted arguments (queries, file paths), sizes, hashes
and citation ids, no log lines; look at the arguments before you send a file outside your team.
Whoever gets it runs `automotive-log-mcp calls --log <file>.jsonl [--transcript <session>.jsonl]`.

```powershell
# Windows (PowerShell)
py -m automotive_log_mcp calls --list            # recent sessions: calls, ~tokens, errors, citation check
py -m automotive_log_mcp calls                   # the last session in detail
py -m automotive_log_mcp calls --session 1e9e5c  # one session (id prefix from --list)
py -m automotive_log_mcp calls --fixed           # what the server adds to every model request, per tool
```

```bash
# Linux / macOS
automotive-log-mcp calls --list
automotive-log-mcp calls --transcript ~/.claude/projects/<project>/<session-id>.jsonl
```

The report lists the calls in order with text and JSON bytes, estimated tokens (`~tok`, bytes/4)
and, with a transcript, the measured tokens each result added (`tok`); then the totals, the fixed
cost per request, how many input tokens were tool results re-sent with later requests, and
*problems* (tool errors, calls that never reached the tool, byte budget used up, the run ended
with an error, answer not checked or failing `check_citations`, exact repeats) and *suggestions*
(structured content, unused tools to hide with `allowed_tools`, capped outputs to narrow, paging,
results the answer never cites, the largest results, too many requests). Its output stays short:
at most 40 call rows.

**What the server cannot see:** the model's real token counts and anything outside its tools
(the client's own prompt, other servers, the model's reasoning). Estimates at bytes/4 are low:
Claude Code counted about 2.2 bytes per token for this server's tool definitions and 1.7–2.0 for
its results (log lines, hex, paths), so real numbers are roughly twice the `~tok` column.

**Real token counts from the client:**

- **Claude Code**: every session is saved as JSONL in `~/.claude/projects/<project>/<session-id>.jsonl`
  (Windows: `%USERPROFILE%\.claude\projects\...`; `<project>` is named after the working
  directory, e.g. `-home-me-work` for `/home/me/work`; the newest file there is the last session). Each model response there has `message.usage` (`input_tokens`,
  `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens`). For a scripted run,
  `claude -p "<question>" --output-format stream-json --verbose > run.jsonl` gives the same
  messages plus a final `result` line with totals and `total_cost_usd`; `--output-format json`
  gives only the totals. Pass either file as `--transcript`: `calls` picks the call-log session
  whose calls match, shows real tokens per request and per tool result, and reads the citations
  of the final answer even when `check_citations` was not called. The transcript is only read
  locally; nothing from it is written.
- **VS Code Copilot**: not verified here (the VS Code and GitHub docs were not reachable from the
  environment this was built in, and no Copilot was available to try). Check the current VS Code
  Copilot docs for a chat debug view or chat log and for chat export (the Command Palette lists
  what your version has; search it for "chat debug" and "export chat"), and your GitHub Copilot
  settings for premium-request usage. Whether Copilot sends the text, the structured content or
  both of a tool result to the model is not verified either; `calls` cannot import a Copilot log
  yet. Without a transcript, `calls --reads both` gives an upper bound and
  `--reads text` a lower one when structured output is on.

**What was measured** (Claude Code 2.1.322, Sonnet, `claude -p` with only this server, the
synthetic bugreport `eval/synthetic/bugreport-*.zip`, two runs per question; input tokens summed
over all requests of a run):

| | M4 | now |
| --- | --- | --- |
| fixed cost per request (definitions + instructions) | 21.9 KB, 10.1k tokens | 14.7 KB, 6.9k tokens |
| "Which process crashed and why?" | 60.8k / 84.1k tokens, 4–5 requests | 34.0k / 34.0k, 3 requests |
| root cause, every finding cited and checked | 114.3k / 158.8k, 6–8 requests | 63.3k / 63.0k, 5 requests |

What made the difference: Claude Code passes only `structuredContent` to the model when a result
has it (as JSON, 1.6–1.7× the bytes of the text, without the text's hints), so results are text
only now; the published input schemas dropped generated titles and `anyOf`/`null` noise and long
repeated descriptions (arguments are still validated as before, `null` still accepted); and the
server instructions steer simple questions to `list_artifacts` → `read_artifact` instead of a
nine-step walk through every tool. Further savings that need no code: hide tools a team never uses
with `allowed_tools` in `policy.yaml` (`calls` says how many tokens per request that saves), and
start a fresh chat per question, since every request re-sends the whole conversation.

## Evaluation

[eval/](eval/README.md) holds the M4 evaluation: `cases.yaml` (closed defects with the expected
component and root-cause citations, written before any run), `evidence_recall.py` (do `detect`,
`list_artifacts` and recorded queries show the root-cause lines on their first page; runs in the
test suite on 5 synthetic cases), the agent-run checklist in `eval/agent_runs/` and
`callstats.py` (calls and bytes per session from the call log). Real defects go into the
git-ignored `eval/cases.local.yaml`; [eval/README.md](eval/README.md) has the steps for Windows
(`py -m automotive_log_mcp`, VS Code Copilot agent mode) and Linux (Claude Code).

## Develop

```bash
ruff check . && ruff format --check . && python -m mypy && python -m pytest
```

Golden records for the fixtures in `tests/fixtures/` are in `tests/golden/`; after an intended
change regenerate them with `UPDATE_GOLDEN=1 python -m pytest tests/test_golden.py` and review the
diff; golden outputs of every tool are in `tests/golden/tools__*.json` (same command with
`tests/test_tools.py` and `tests/test_p1_tools.py`). The performance tests generate a large logcat outside the repository,
index it once and measure ingest and every tool (latency, output bytes):
`PERF_MB=1024 python -m pytest -m perf -s tests/test_perf.py` (`PERF_KEEP=<dir>` keeps the log
and the DB for later runs).
