# automotive-log-mcp

An MCP server that lets any agent analyse a defect from automotive logs (Android bugreport,
logcat, kernel logs), with every finding cited to a log line or artifact.

Status: **M3 P0 tools**. Logcat (all `-v` formats), kernel logs (`dmesg`, `/dev/kmsg`), Android
bugreports (zip or text), archives (zip, tar, gz) and YAML-defined text formats are indexed into
one SQLite DB per case, with redaction at index time. Tombstones, ANR traces, Java crashes and
dropbox entries become artifacts with a summary, a crash signature and a record on the timeline.
All P0 tools work over MCP: `add_source`, `list_sources`, `search`, `context`, `list_artifacts`,
`read_artifact`, `get_records`, with capped, cited output. Everything is verified on synthetic
fixtures only, not yet on real bugreports; ingest (about 33–42k lines/s, DB 1.9× the input) misses
the M1 targets. See [docs/design.md](docs/design.md) and
[docs/implementation-plan.md](docs/implementation-plan.md).

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
[examples/mcp-config/](examples/mcp-config/). `add_source` indexes in a background thread per
case: it waits up to 10 s, then reports that indexing continues; `list_sources` shows progress.

## Tools

| Tool | What it returns |
| --- | --- |
| `add_source(case, path, format?)` | indexes a file, directory or zip (background thread; waits up to 10 s) and the new source ids |
| `list_sources(case)` | sources with format, record count, time range, fingerprint, stale/missing flags |
| `search(case, query, mode, filter, collapse, count_only, limit, cursor)` | matching records in time order; FTS5 words or a regex over message and tag |
| `context(case, at, before, after, scope)` | lines around `log:<s>#<n>` or an artifact's record `art:<id>`; `scope='pid'` follows one process through all buffers of the same file, merged by time |
| `list_artifacts(case, type?)` | tombstones, ANRs, Java crashes, dropbox entries grouped by crash signature, in time order; copies of one crash marked `same crash as art:<id>` |
| `read_artifact(case, id, section?, start_line?, end_line?)` | summary, signature and section list; then a section or a line range, lines numbered `L<n>` |
| `get_records(case, ids)` | full records: untruncated message, timestamp as written, attributes, template text |

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
  `AND`/`OR`/`NOT`); a text query that looks like a regex gets a warning. Regex mode is Python
  `re`, case-sensitive; literal parts of the pattern prefilter in SQL (FTS and `instr`), then at
  most `regex_scan_rows` candidates are scanned (a capped scan says so and counts are lower
  bounds).
- **Empty results say why**: a word that occurs nowhere, or which filter part removes the matches
  (`3 matches in the case, none with the filter: 3 without the level filter (level F)`).
- **Structured content** holds the same items as the text (citations, full timestamps, counts,
  templates), with a declared output schema per tool.
- Raw values in queries and filters (a MAC, a VIN) are redacted like the logs, so they match
  their tokens.

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
```

- `index` options: `--format <connector>` forces a format instead of sniffing; `--force`
  reindexes a file that is already in the case (adding the same file again is otherwise a no-op).
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

## Formats

| Connector | Input |
| --- | --- |
| `bugreport` | `bugreport-*.txt` (also inside the zip): `SYSTEM LOG` split per buffer (main, system, crash, …), `EVENT LOG`, `KERNEL LOG`, fingerprint, time zone, uptime, process list, binder transactions; ANR, tombstone and dropbox sections go to the artifact parsers |
| `tombstone`, `anr`, `dropbox` | the artifact files listed under [Artifacts](#artifacts) (sniffed by content or name) |
| `logcat` | `threadtime`, `time`, `brief`, `process`, `tag`, `thread`, `long`, `raw`; modifiers `year`, `epoch`, `monotonic`, `usec`, `nsec`, `zone`, `UTC`, `uid`, `printable`, `color`; buffer markers; multi-line messages merged |
| `kernel` | `dmesg` (with or without `<prio>`, caller ids, `-x`, `-T`) and `/dev/kmsg` |
| `archive` | zip, tar (plain, gz, bz2, xz), single-file gzip; nested up to depth 4 |
| `syslog` | BSD syslog / `journalctl -o short` (a YAML connector shipped in the package) |

Own text formats are added as YAML files in a directory listed under `rule_dirs` in
`config.yaml`; the format is documented in
[src/automotive_log_mcp/connectors/regex_yaml.py](src/automotive_log_mcp/connectors/regex_yaml.py).
Python connectors can be registered as entry points (`automotive_log_mcp.connectors`); they are
loaded only when the policy sets `allow_code_connectors: true`. Lines a connector cannot parse are
kept as records with no level, never dropped.

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
| `config.yaml` | optional: `cases_dir`, `policy_file`, `call_log` (null disables), `caps` (`max_lines`, `max_bytes`, `max_msg_chars`, `regex_scan_rows`), `rule_dirs` |
| `policy.yaml` | optional: `allowed_tools`, `blocked_sources` (globs on paths and archive members: refused by `add_source`, hidden from tools), `blocked_ecus`, `byte_budget`, `allow_code_connectors` |
| `cases/<case>.db` | one SQLite DB per case; originals are referenced by path and SHA-256, never copied |
| `secret.key` | redaction key, created on first run; deleting it changes every token of later imports |
| `calllog.jsonl` | one line per tool call: tool, arguments, status, duration, output bytes; never log content |

Example `policy.yaml`:

```yaml
allowed_tools: [list_sources, search, context, list_artifacts, read_artifact, get_records]
byte_budget: 500000
```

## Develop

```bash
ruff check . && ruff format --check . && python -m mypy && python -m pytest
```

Golden records for the fixtures in `tests/fixtures/` are in `tests/golden/`; after an intended
change regenerate them with `UPDATE_GOLDEN=1 python -m pytest tests/test_golden.py` and review the
diff; golden outputs of every tool are in `tests/golden/tools__*.json` (same command with
`tests/test_tools.py`). The performance tests generate a large logcat outside the repository,
index it once and measure ingest and every tool (latency, output bytes):
`PERF_MB=1024 python -m pytest -m perf -s tests/test_perf.py` (`PERF_KEEP=<dir>` keeps the log
and the DB for later runs).
