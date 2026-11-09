# automotive-log-mcp

An MCP server that lets any agent analyse a defect from automotive logs (Android bugreport,
logcat, kernel logs), with every finding cited to a log line or artifact.

Status: **M2 artifacts** (ingest measured at about 42k lines/s and a DB of 1.9× the input on a generated 1 GiB logcat in M1; targets not met yet, see the plan). Logcat (all `-v` formats), kernel logs (`dmesg`, `/dev/kmsg`),
Android bugreports (zip or text), archives (zip, tar, gz) and YAML-defined text formats are
indexed into one SQLite DB per case, with redaction at index time. Tombstones, ANR traces, Java
crashes and dropbox entries become artifacts with a summary, a crash signature and a record on the
timeline. `add_source` and `list_sources` work over MCP, and the CLI can index, query and read
artifacts without an agent. The other tools (`search`, `context`, `list_artifacts`,
`read_artifact`, …) report that they arrive in M3. All parsers are verified on synthetic
fixtures only, not yet on real bugreports. See
[docs/design.md](docs/design.md) and [docs/implementation-plan.md](docs/implementation-plan.md).

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

## Command line (no agent needed)

```bash
automotive-log-mcp index T-1 ~/bugreports/bugreport-2026-10-11.zip   # file, archive or directory
automotive-log-mcp sources T-1                                       # formats, counts, time ranges
automotive-log-mcp query T-1 --search "setProperty failed" --level W --tag 'Hvac*' --limit 20
automotive-log-mcp query T-1 --regex 'status=\w+_AVAILABLE' --count
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
- `query` options: `--search` (FTS5 over message and tag), `--regex`, `--level` (minimum),
  `--tag` (glob, repeatable), `--source` (id, repeatable), `--pid`, `--limit`, `--count`. Raw
  values in the query (a VIN, a MAC) are redacted the same way as the logs, so they still match.
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

- Every artifact has a record on the timeline, so `query` (and in M3 `search`/`context`) finds it:
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
| `config.yaml` | optional: `cases_dir`, `policy_file`, `call_log` (null disables), `caps`, `rule_dirs` |
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
diff. The ingest performance test generates a large logcat outside the repository:
`PERF_MB=1024 python -m pytest -m perf -s tests/test_perf.py`.
