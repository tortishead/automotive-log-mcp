# Reference

Details for each part of automotive-log-mcp. Start with the [README](../README.md).

- [Run](#run) · [Command line](#command-line) · [HTTP](#http)
- [Tools](#tools) · [Filter](#filter) · [Artifacts](#artifacts) · [Formats](#formats)
- [Redaction](#redaction) · [Files and config](#files-and-config) · [Storage](#storage)
- [Token use](#token-use) · [Status](#status) · [Design](#design) · [Develop](#develop)

## Run

```bash
automotive-log-mcp              # MCP over stdio (default)
automotive-log-mcp --http       # MCP over HTTP, see below
```

- Results are plain text. `--structured` (or `structured_output: true` in `config.yaml`) also
  sends JSON, which costs more tokens (see [Token use](#token-use)).
- `add_source` indexes in the background. It waits up to 10 s, then says indexing goes on;
  `list_sources` shows the progress.
- **A file is indexed once.** If the same file (same SHA-256) is already in this or another case,
  `add_source` does nothing and names that case. `reindex=true` indexes it again.
- If the server stopped during indexing, the import starts again from the beginning next time.
- A case file of an older schema is upgraded when it is opened. No re-import needed.

### Command line

Every tool is a command: `automotive-log-mcp <tool> <case> ...`. It calls the same MCP tool, so
checks, `allowed_tools`, the byte budget and the call log (session `cli-<pid>`) all apply, and the
output is what an agent sees.

```bash
automotive-log-mcp add_source br ~/bugs/bugreport.zip      # waits until done; progress on stderr (-q: quiet)
automotive-log-mcp list-sources br                          # '-' or '_' both work
automotive-log-mcp search br "setProperty failed" --level E --tag 'Hvac*' --from -5m
automotive-log-mcp count br --by none --bucket 1m           # 'none' means null
automotive-log-mcp read_artifact br art:1/exception
automotive-log-mcp get_records br log:1#2 log:3#1
automotive-log-mcp check_citations br answer.md             # '-' reads stdin
automotive-log-mcp search br crash --json                   # JSON output
```

- Required parameters are positional (`case`, `path`, `at`, `id`, `ids`). `query` is an optional
  positional. `check_citations` reads its text from a file.
- All other parameters are options (`not_tags` → `--not-tags`). Filter fields are options too
  (`--level`, `--from`, `--to`, `--sources`, `--tag`, `--pid`, …). List options repeat
  (`--tag A --tag B`). `--attrs KEY=VALUE` repeats.
- `<tool> --help` shows every option.
- Exit code: 0 ok, 1 tool error or `check_citations` FAIL, 2 wrong usage. Server options
  (`--http`, `--host`, `--port`, `--structured`) are not allowed with a command.
- Hints in the output name the next call in MCP form, e.g. `read_artifact(id='art:1')`.

### HTTP

`--http` serves the same tools over streamable HTTP, for a server on a shared machine next to the
logs.

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))" > ~/.automotive-log-mcp/token.txt
cp examples/http/config.yaml examples/http/policy.yaml ~/.automotive-log-mcp/   # then set allowed_roots
automotive-log-mcp --http                       # http://127.0.0.1:8765/mcp
automotive-log-mcp --http --host 0.0.0.0 --port 9000
```

- **Token:** every request needs `Authorization: Bearer <token>`. The token comes from
  `AUTOMOTIVE_LOG_MCP_TOKEN` or from the file `http.token_file` (at least 16 characters). Without a
  token the server does not start. A wrong token gets 401. There is one token and no users: whoever
  has it can call every allowed tool.
- **Paths:** `add_source` reads paths on the server machine, only below `allowed_roots` in
  `policy.yaml`. Without that key, HTTP refuses every path. A refused path does not reveal whether
  it exists. Symbolic links that point outside are refused. With roots set, relative paths are
  refused. `allowed_roots` also applies over stdio when set.
- **Network:** on `127.0.0.1`, DNS-rebinding protection is on. On another address, list the Host
  names clients use in `http.allowed_hosts`. There is no TLS: outside loopback the token is sent in
  clear text. Use a TLS proxy or an SSH tunnel (`ssh -L 8765:127.0.0.1:8765 logs-host`).
- The call log, byte budget and `allowed_tools` work as over stdio. They belong to the server
  process, so all clients of one server share them.
- Client setup: [examples/mcp-config/](../examples/mcp-config/README.md#over-http).

## Tools

| Tool | Returns |
| --- | --- |
| `add_source(case, path, format?, reindex?)` | indexes a file, folder or zip; returns the new source ids |
| `list_sources(case)` | sources with format, line count, time range, device fingerprint; flags stale or missing files |
| `overview(case, filter?)` | levels, top tags, pids and messages, time gaps over 30 s, error bursts, a histogram, crash counts |
| `list_artifacts(case, type?)` | tombstones, ANRs, Java crashes and dropbox entries in time order, grouped by crash; copies say `same crash as art:<id>` |
| `read_artifact(case, id, section?, start_line?, end_line?)` | summary, crash signature and section list; then a section or a line range (`L<n>`) |
| `search(case, query, mode, filter, collapse, count_only, limit, cursor)` | matching lines in time order; words or a regex over message and tag |
| `context(case, at, before, after, scope)` | lines around `log:<s>#<n>` or `art:<id>`; `scope='pid'` follows one process across all buffers of a file |
| `get_records(case, ids)` | full lines: whole message, time as written, attributes, message template |
| `count(case, by, query?, mode, filter?, bucket?, limit)` | lines per `tag`, `level`, `pid`, `source` or `template`; optional query; optional time slots (`'1m'`, up to 60) |
| `check_citations(case, text)` | checks a Markdown answer; prints `PASS` or `FAIL` |

Example hits. A repeated line shows a count and its last time. A line that belongs to a crash
names it:

```
log:2#18233  10-11 08:12:03.412  E 1234/1250 HvacCtrl  setProperty failed, status=NOT_AVAILABLE  ×37 until 10-11 08:14:51.003 (log:2#19001)
log:3#1  01-01 00:00:01.000  E 1234/1250 AndroidRuntime  FATAL EXCEPTION: main ⏎ Process: … → art:1
```

### Output rules

- **Limits** (`caps` in `config.yaml`): `max_lines` 50 and `max_bytes` 8192 per result;
  `max_msg_chars` 200 per message (cut with `…`; `get_records` shows all); `regex_scan_rows`
  1,000,000 lines per regex search. `read_artifact` shows up to 100 lines.
- **When cut**, a result ends with a cursor for the next page and ways to narrow the search
  (levels, tags, times with counts).
- **Collapse** groups lines with the same source, message template, tag and level. Templates hide
  numbers, hex, ids and paths.
- **Code search:** each hit carries its template's longest fixed text
  (`"setProperty failed, status="`), ready to search in the source code.
- **Empty results say why:** a word that occurs nowhere, or which filter removed the matches.
- **Sources filter:** the result says how many matches are in the other sources.
- **No query** (`''` or `'*'`) lists every line that passes the filter.
- Raw values in queries (a MAC, a VIN) are redacted like the logs, so they still match.

### Text and regex search

- **Text** (default): SQLite FTS5. Whole words, not case-sensitive, `"phrases"`, `prefix*`,
  `AND`/`OR`/`NOT`. Parts of CamelCase names are words too: `Delegate` finds
  `EsoCarDelegateDisplayAreaPolicyProvider`. A query that looks like a regex gets a warning.
- **Regex:** Python `re`, case-sensitive. Fixed parts of the pattern narrow the lines first; then
  at most `regex_scan_rows` lines are scanned. If that limit is hit, the result says so and counts
  are minimums.

### overview, count, check_citations

- **overview** is the first call after `list_sources`. A gap is over 30 s with no line. An error
  burst is a run of 10-s slots where E/F lines are above 5× the median and above 10. The
  histogram is shown as two small bar lines (all lines, E/F). Each item has an example citation.
- **count** without a query counts every line that passes the filter. With a query it counts
  matches. Over 300k text matches are sampled and marked `≈`. Slot start times can be passed back
  as `from`.
- **check_citations**: every `log:`/`art:` citation must be valid and exist (else error).
  Paragraphs without a citation, and quotes not found in the cited lines, are warnings. Citations
  in code blocks are skipped. Paragraphs under 8 words, or that say they are a guess ("probably",
  "unknown", "the logs do not show"), need no citation. The call log keeps only the text's length
  and hash.

### Filter

| Key | Meaning |
| --- | --- |
| `sources` | source ids, or globs on name or path, not case-sensitive (`*ivi*`, `*/sys/*`, `*:crash`). A path glob also matches parent folders, so `*/ivi/*` is more exact than `*ivi*`. |
| `from`, `to` | time range, forms below. `to` includes the whole unit given: `to: "08:14"` includes all of 08:14. |
| `level` | minimum level (`V D I W E F`) |
| `tags`, `not_tags` | globs, case-sensitive. An unknown tag gets a warning with similar tags. |
| `pid`, `tid` | process and thread id |
| `attrs` | exact match on extra fields, e.g. `{"uid": "1000"}` or `{"art": 3}` |

Time forms for `from` and `to`:

| Form | Example | Meaning |
| --- | --- | --- |
| as shown in text | `01-01 00:00:01.000` | year taken from the case (error if unclear) |
| as shown in JSON | `2026-01-01 00:00:01.000`, `2026-01-01` | |
| time of day | `00:00:01` | day taken from the case if it has only one |
| seconds since boot | `123.456789` | for logs with only a boot clock (kernel) |
| relative | `-5m`, `+30s`, `1h30m` | `-`: before the case end; `+`: after its start (in `to`: after `from`) |
| citation | `log:3#120`, `art:2`, `log:3#120-30s` | that line's time, optionally shifted |

Times are the device clock as written. No time-zone conversion. Each source keeps its own clock;
sources are not aligned to each other.

## Artifacts

An artifact is a crash or ANR file, or a dropbox entry.

| Type | Comes from | What is parsed |
| --- | --- | --- |
| `tombstone` | `FS/data/tombstones/tombstone_NN`, `TOMBSTONE` sections, `*_native_crash` dropbox entries | process, pid/tid, thread, signal, fault address, cause, abort message, backtrace; sections `header`, `registers`, `backtrace`, `memory`, `memory_map`, `threads`, `open_files`, `log` |
| `anr` | `FS/data/anr/anr_*`, `VM TRACES AT LAST ANR`, `*_anr` dropbox entries | subject, main thread state and frames, lock-holder chain (finds deadlocks), a blocked binder call and the process on the other end; one section per process and thread (`p<pid>.t<tid>.<name>`) |
| `java_crash` | `FATAL EXCEPTION` lines in logcat, `*_crash` dropbox entries | process, pid, thread, exception, causes, root cause and its frames |
| `dropbox` | other dropbox entries (`*_wtf`, `*_strictmode`, `SYSTEM_BOOT`, …) | tag, header fields, first line, Java stack if any |

- **On the timeline:** each artifact has a line `[art:<id>] <headline>` in the source it came
  from (level F for crashes, E for ANRs, W for others), so `search` and `context` find it. A
  logcat crash uses its own `FATAL EXCEPTION` line.
- **Signature** groups the same crash: `<kind> <process>: <frame> < <frame> < <frame>`. Kind is
  the signal, the root exception class, or `ANR <main thread state>`. Frames drop arguments, line
  numbers and offsets; abort helpers in `libc`/`libbase`/`liblog` are skipped.
- **Citations** `art:<id>[/<section>][:L<line>]`. Line numbers count over the whole file, so they
  match the original. A given section must contain the line.
- **Times** are the device clock as written. Dropbox file names are in UTC and are converted with
  the bugreport's time zone; without one the time stays UTC (`fields.ts_zone: UTC`).
- In a bugreport zip the main file is read first, so its fingerprint, time zone and process list
  apply to the other files.
- Not parsed yet: tombstone `.pb`, `dumpsys` sections, kernel panics (`pstore`).

## Formats

| Connector | Input |
| --- | --- |
| `bugreport` | `bugreport-*.txt` (also inside the zip): system log, event log, kernel log, fingerprint, time zone, uptime, process list, binder transactions; crash sections go to the artifact parsers. The system log is one source named after its buffers (`main+system+crash`); it is split per buffer only if logcat ran with `-D`/`--dividers`. |
| `tombstone`, `anr`, `dropbox` | the artifact files above |
| `logcat` | formats `threadtime`, `time`, `brief`, `process`, `tag`, `thread`, `long`, `raw`; modifiers `year`, `epoch`, `monotonic`, `usec`, `nsec`, `zone`, `UTC`, `uid`, `printable`, `color`; multi-line messages are merged |
| `kernel` | `dmesg` (with or without `<prio>`, caller ids, `-x`, `-T`) and `/dev/kmsg` |
| `archive` | zip, tar (plain, gz, bz2, xz), gzip; nested up to 4 deep |
| `syslog` | BSD syslog and `journalctl -o short` |

Your own formats: see [connectors.md](connectors.md). Lines a connector cannot parse are kept
with no level, never dropped.

## Redaction

Always on, when a log is indexed. VINs, IMEIs, MAC and IPv4 addresses, GPS coordinates, e-mail
addresses, phone numbers (`+…`), serial numbers and Wi-Fi SSIDs become tokens like
`<MAC:3fa2c1d0>`. The suffix is a keyed hash (key in `secret.key`): the same value always gives
the same token, but the value cannot be recovered. Artifacts and call-log arguments are redacted
the same way.

## Files and config

Everything lives in `~/.automotive-log-mcp/` (change it with `AUTOMOTIVE_LOG_MCP_HOME`):

| Path | Content |
| --- | --- |
| `config.yaml` | optional, see below |
| `policy.yaml` | optional, see below |
| `cases/<case>.db` | one SQLite file per case. Log files are referenced by path and SHA-256, never copied. |
| `secret.key` | redaction key, made on first run. Deleting it changes all tokens of later imports. |
| `calllog.jsonl` | one line per tool call: tool, redacted arguments, status, time, output size, estimated tokens, citations in the output. No log content. |
| `rounds/` | the call log split per round, with a report (see [Token use](#token-use)) |

`config.yaml` keys:

| Key | Meaning |
| --- | --- |
| `cases_dir`, `policy_file` | other locations |
| `call_log` | path; `null` turns it off |
| `caps` | `max_lines`, `max_bytes`, `max_msg_chars`, `regex_scan_rows` (see [Output rules](#output-rules)) |
| `rule_dirs` | folders with your own connectors |
| `structured_output` | also send JSON (default false) |
| `rounds_dir`, `round_gap_minutes` | round files (default `<home>/rounds`, `null` off) and the pause that starts a new round (default 10) |
| `http` | `host`, `port`, `path`, `token_file`, `allowed_hosts` (see [HTTP](#http)) |

`policy.yaml` keys:

| Key | Meaning |
| --- | --- |
| `allowed_tools` | only these tools are offered |
| `blocked_sources` | globs on paths and archive members: refused by `add_source`, hidden from tools |
| `byte_budget` | total output bytes per server session; later calls are refused |
| `allow_code_connectors` | allow Python connectors (default false) |
| `allowed_roots` | folders `add_source` may read (required over HTTP) |

Example:

```yaml
allowed_tools: [list_sources, search, context, list_artifacts, read_artifact, get_records]
byte_budget: 500000
```

## Storage

One SQLite file per case, three main tables:

- `records`: time, level, tag, pid, tid, template and attributes of each line, without the
  message. Indexed by time and tag only; filters on pid, level or template scan.
- `msg_blocks`: the redacted messages, about 64 KB per block, zlib-compressed.
- `records_fts`: the full-text index, without a copy of the text.

On a generated 200 MiB logcat (1.9M lines) the case file is 268 MiB, 1.34× the input. Indexing
runs at about 26–29k lines/s. Regex searches and `overview` pay for decompression (whole-case
`overview` about 5 s); text search and `context` do not.

## Token use

The server writes enough to see where an agent's tokens go. Nothing to turn on.

**Rounds.** Each round is saved in `~/.automotive-log-mcp/rounds/` as
`<date>_<time>_<session>_r<n>.jsonl`, with a readable report `….txt` next to it. A round starts
with the server and again after `round_gap_minutes` (default 10) with no call. VS Code keeps the
server running between chats, so leave a pause, or restart the server, between questions you
want to compare.

The report lists each call with its size and estimated tokens, the totals, the fixed cost per
request, and:

- **problems:** tool errors, byte budget used up, answer not checked or failing
  `check_citations`, repeated calls;
- **suggestions:** tools to hide with `allowed_tools`, results to narrow, results the answer never
  used, the largest results.

The files hold tool names, redacted arguments (queries, paths), sizes and citations, no log
lines. Check the arguments before you share a file outside your team.

**Estimates are low.** The server guesses tokens as bytes/4. Claude Code counted about 2.2 bytes
per token for the tool definitions and 1.7–2.0 for results, so real numbers are about twice the
estimate. The server cannot see the client's own prompt, other servers or the model's thinking.

**Real counts from the client:**

- **Claude Code:** sessions are saved in `~/.claude/projects/<project>/<session-id>.jsonl`. Each
  model response has `message.usage`. For a scripted run,
  `claude -p "<question>" --output-format stream-json --verbose > run.jsonl` adds a final `result`
  line with totals and cost.
- **VS Code Copilot:** not checked. Look for "chat debug" and "export chat" in the Command Palette,
  and at premium-request usage in your GitHub settings.

**Measured** (Claude Code, Sonnet, only this server, a synthetic test bugreport, input tokens summed
over a run):

| | Tokens |
| --- | --- |
| fixed cost per request (tool definitions + instructions) | 14.7 KB, about 6.9k |
| "Which process crashed and why?" | about 34k, 3 requests |
| root cause, every finding cited and checked | about 63k, 5 requests |

**To save tokens:** hide tools you do not use with `allowed_tools`; keep structured output off
(Claude Code then gets JSON only, 1.6× the size of the text); start a new chat per question,
because every request re-sends the whole chat. Using the CLI instead of MCP, with short
instructions, costs an estimated 0.5k tokens per request instead of 6.9k.

## Status

- Built: the 10 tools, stdio and HTTP, the CLI, redaction, policy, call log and round reports.
- Tested on synthetic logs only, not yet on real logs.
- **Not met:** indexing speed (26–29k lines/s; the target was 100k).
- **Not built:** tombstone `.pb`, `dumpsys` and kernel panic parsers.
- Removed on purpose: aligning sources on one clock, `timeline`, `align`, `diff`, `detect`, DLT.
  Real cases usually hold one log, and every extra tool costs tokens in every request.

## Design

- Python 3.11+, the official MCP SDK, SQLite with FTS5. One worker thread per case for indexing.
- One SQLite file per case. Log files are referenced by path and hash, never copied.
- Redaction is always on and happens at index time, so raw values never reach the database, the
  templates or the call log.
- Citations: `log:<source>#<n>` (n counts from 1 per source) and
  `art:<id>[/section][:L<line>]`.
- Message templates are made by masking numbers, hex, UUIDs and paths.
- Built-in connectors ship in the package. Your own come as YAML, a command, or a Python package.
- No UI, no ticket-system link, no model calls from the server, no multi-user mode, no native
  symbolization.
- No network calls over stdio. HTTP listens on loopback by default and never calls out.
- Tool names and parameters follow semver.

## Develop

```bash
ruff check . && ruff format --check . && python -m mypy && python -m pytest
```

- **Golden files** in `tests/golden/` hold the expected records and tool outputs. After an
  intended change, regenerate and review the diff:
  `UPDATE_GOLDEN=1 python -m pytest tests/test_golden.py tests/test_tools.py tests/test_p1_tools.py`.
- **Performance:** `PERF_MB=1024 python -m pytest -m perf -s tests/test_perf.py` generates a large
  logcat outside the repo, indexes it and times every tool. `PERF_KEEP=<dir>` keeps the log and
  the database.
