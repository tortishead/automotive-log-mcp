# automotive-log-mcp: implementation plan

Nov 8, 2026 · @Xuan · draft · based on [design.md](design.md)

## Goal and scope

Build an MCP server that lets any agent analyse a defect from automotive logs, with every finding cited to a log line or artifact.

- **First release (M0–M4):** logcat, bugreport (including tombstones, ANRs, Java crashes and dropbox), kernel logs and config-driven text logs; P0 and P1 tools including `check_citations`; local stdio transport; redaction and policy file; call log; evaluation on closed defects.
- **Second release (M5–M6):** DLT (via `dlt-core`), cross-source alignment, `timeline`, `diff`, remaining artifact parsers (`dumpsys`, kernel panic, tombstone `.pb`), HTTP transport. Until then, DLT users can use dlt-mcp-server inside DLT Viewer.
- **Out:** any UI, ticket-system integration, server-side model calls, multi-user operation, symbolization of native frames.

Efforts are for one developer, in working days, including tests.

## Decisions

Reviewed on Nov 8, 2026 (see the review outcome at the end). Changing a decision changes the milestones that list it.

| # | Decision | Alternative | Affects |
| --- | --- | --- | --- |
| D1 | Python 3.11+ for server and core; Rust only for the DLT sidecar | All Rust (faster ingest, single binary) | all |
| D2 | Official Python `mcp` SDK, stdio first, HTTP in M6 | Own JSON-RPC | M0, M6 |
| D3 | Store: stdlib `sqlite3` with FTS5, one DB file per case, WAL | DuckDB, Tantivy | M1 |
| D4 | Templates by regex masking (numbers, hex, UUIDs, paths) | Drain3 clustering | M1 |
| D5 | Redaction always on, at index time and before template extraction, with keyed-hash suffix; raw values never in records, templates or call log; no raw mode | Redact at output time; opt-in redaction | M1 |
| D6 | Citations `log:<source>#<seq>` (seq 1-based per source) and `art:<id>[/section][:L<line>]` | Global row id | M3 |
| D7 | Cases in `~/.automotive-log-mcp/cases/<case>.db`; originals referenced by path and hash, not copied; `relink` for moved files | Copy into a case folder | M1 |
| D8 | Default detector rules and regex connectors ship in the package; extra rule directories from config override rules by name | Rules only in the package | M4 |
| D9 | DLT parsed by `dlt-core` in a sidecar binary `dlt2jsonl`, run through the generic `command` connector | PyO3 binding; DLT Viewer export; own parser | M5 |
| D10 | Single process; background indexing in one worker thread per case | Worker processes | M1 |
| D11 | Tool names and parameters, and the connector interface, follow semver; breaking changes only in a major version | No API stability promise | all |
| D12 | First release without DLT; DLT and alignment in M5 | DLT before the first evaluation | M4, M5 |
| D13 | Third-party connectors: YAML, `command`, or Python entry points; extend by default, replace when configured; code connectors allowed by policy | Connectors only in this repo | M1, M5, M6 |

## Repository layout

```
automotive-log-mcp/
  pyproject.toml              # deps: mcp, pyyaml; dev: pytest, ruff, mypy, hypothesis
  src/automotive_log_mcp/
    server.py                 # MCP tools, instructions, prompts, arg validation, caps
    cli.py                    # serve | index | query | rules  (works without an agent)
    config.py                 # paths, caps, rule dirs, policy file
    policy.py                 # allowed tools, blocked sources/ECUs, byte budget
    calllog.py                # JSONL call log
    model.py                  # RawRecord, RawArtifact, Record, Artifact, Source, Segment
    store/
      schema.sql
      db.py                   # open/migrate case DB, batch writer
      query.py                # filter object -> SQL, FTS, regex, cursors
    core/
      ingest.py               # sniff -> container -> connector -> pipeline -> store
      levels.py, templates.py, redact.py, segments.py
      clock.py, align.py      # M5
      format.py               # text + structured output, truncation, citations
    connectors/
      base.py                 # protocols, registry
      archive.py              # zip / tar / gz
      bugreport.py            # container
      logcat.py, kernel.py, regex_yaml.py
      command.py              # M5
      artifacts/              # M2: tombstone, anr, java_crash, dropbox; M5: dumpsys, kernel_panic, tombstone .pb, dlt_file
      dlt.py                  # M5, drives dlt2jsonl
    detect/
      engine.py
    rules/                    # default detector rules and regex connectors (YAML)
  dlt2jsonl/                  # M5, Rust crate using dlt-core
  tests/
    fixtures/                 # small synthetic logs, one per format variant
    golden/                   # expected tool outputs
  eval/
    cases.yaml                # closed defects: files, expected component, root-cause citations
    evidence_recall.py        # tool-level eval, no agent needed
    agent_runs/               # agent-level eval notes and call logs
  examples/
    mcp-config/               # client config snippets (Copilot, Claude, Cursor, …)
```

## Milestones

| M | Name | Outcome | Effort |
| --- | --- | --- | --- |
| M0 | Skeleton | Any MCP client lists the tools; `list_sources` answers on an empty case | 1–2 d |
| M1 | Ingest core | A bugreport zip's log sections are indexed into a case DB; CLI can query them | 5–6 d |
| M2 | Artifacts | Tombstones, ANRs, Java crashes and dropbox entries from a bugreport are parsed and on the timeline | 2 d |
| M3 | P0 tools | An agent finds and reads errors and crash artifacts in one bugreport, with citations | 4 d |
| M4 | P1 tools + evaluation | `overview`, `detect`, `count`, `check_citations`; agent instructions and prompt; eval on 5 defects | 5–6 d |
| — | **Checkpoint** | Go / adjust / stop, based on eval and call logs | — |
| M5 | DLT + multi-source | DLT incl. file transfer, clock model, `align`, `timeline`, remaining artifact parsers | 8–10 d |
| M6 | Finish | `diff`, HTTP transport, eval on 10 defects, release | 2–3 d |

To checkpoint: about 17–20 days. To M6: about 27–33 days.

## M0 Skeleton (1–2 days)

1. `pyproject.toml`, ruff, mypy, pytest; CI on Linux and Windows.
2. `server.py`: official MCP SDK over stdio; all P0 tools as stubs with full parameter schemas and read-only annotations.
3. Server `instructions` text (analysis order, citation rule) and an empty `defect_triage` prompt.
4. `config.py`, `policy.py` (allowed tools, caps), `calllog.py`.
5. FTS5 availability check at startup with a clear error.
6. Client config snippets in `examples/mcp-config/` for at least two clients.

Done when: two different MCP clients list the tools, `list_sources` returns an empty list, and the call log has the lines.

Status (Nov 8, 2026): done. Checked with Claude Code (`claude -p` with `examples/mcp-config/claude-code.mcp.json`), the Python SDK client and a raw JSON-RPC client over stdio (the last two in `tests/test_stdio.py`). Other tools return "not implemented yet (planned for M1/M3)".

## M1 Ingest core (5–6 days)

1. **Schema**: `sources`, `segments`, `records`, `templates`, `records_fts` (external content on `msg`, `tag`), `artifacts`, `artifact_sections`, `artifact_fts`, `meta` (schema version); indexes on `(source_id, seq)`, `(ts)`, `(source_id, ts_src)`, `tag`, `pid`, `template_id`, `level`; migration on open.
2. **Batch writer**: 50k rows per transaction, WAL, `synchronous=NORMAL`, FTS in the same transaction.
3. **Pipeline**: sniff → container → connector → levels → redact → templates → segments → writer; redaction before templates so no raw value reaches the `templates` table; progress per source.
4. **Registry**: built-in, YAML and entry-point connectors (D13). Highest `sniff()` score above 0.5 wins, else "unknown format" with the top 3 guesses.
5. **Connectors**: `archive`; `logcat` (all formats and `-v` modifiers, buffer markers, multi-line); `kernel`; `bugreport` (sections, fingerprint, timezone, uptime, process list); `regex_yaml` (schema-validated).
6. **Templates, redaction** (HMAC suffix, secret created on first run; same rules applied to call-log arguments), **segments** (monotonic reset, backwards wall jump > 60 s, boot markers; in logs without a year, month 12 → 01 increments the year instead of starting a segment).
7. **Background indexing** in one worker thread per case; queries read committed rows.
8. **CLI**: `index <case> <path>`, `query <case> --search …`, `relink <case> <source> <path>` (checks the hash). `list_sources` flags sources whose original is missing; reindexing them fails with a clear error.
9. Unparseable lines become records with `level=null` and the raw text, never a failed import.
10. **Reuse by hash** (optional, cut if time is short): same file hash, connector version and redaction-rules hash in another case → copy rows instead of reparsing.

Tests: one synthetic fixture per format variant with golden records; property tests for templates (idempotent masking, number-only differences share a template); redaction tests including no false positives on fingerprints and package names, and a check that after indexing no rule matches anything in `templates` or the call log; a threadtime fixture crossing New Year; performance on a generated 1 GB logcat (lines/s, DB size; target ≥ 100k lines/s and DB ≤ 1.5× input).

Done when: a real bugreport indexes without errors, record counts match the input within multi-line merges, and the performance number is measured and accepted.

Status (Nov 8, 2026): built, verified on synthetic data only; performance targets not met.

- Verified with synthetic fixtures (one per format variant, golden records in `tests/golden/`): logcat (all `-v` formats and modifiers, buffer markers, multi-line merges, New Year crossing), kernel (`dmesg` variants, `/dev/kmsg`), a bugreport zip (sections, per-buffer sources, fingerprint, time zone, uptime, process list), archives, the shipped `syslog` YAML connector; property tests for templates and redaction; a check that no redaction rule matches anything in records, templates, source metadata or the call log after indexing; background indexing with committed batches visible to readers; failed imports; relink with hash check; policy `blocked_sources` and `allow_code_connectors`. Tests pass on Python 3.11 and 3.13 (Linux; Windows only through CI).
- **Not verified:** a real bugreport and record counts on real data (no real samples were available; "Inputs needed before M1" still open). The redaction rules were not reviewed against a real sample.
- **Performance** (generated 1 GiB threadtime logcat, `PERF_MB=1024 pytest -m perf`, one core): 9.77M lines (1,024 MiB) in 231.6 s including hashing = **42,200 lines/s** (target ≥ 100k); DB 1,960 MiB = **1.91× input** (target ≤ 1.5×); line counts matched the input within multi-line merges. Not accepted yet: both targets are missed. Measured breakdown on a 50 MB sample: Python parse + redact + template ≈ 15 µs/line (≈ 66k lines/s without any DB writes), SQLite ≈ 8 µs/row (base insert 1.4, six secondary indexes ≈ 3, FTS5 ≈ 3). A writer thread overlapping parse and write did not help (binding rows holds the GIL). Size: records ≈ 1.0×, FTS ≈ 0.4×, indexes ≈ 0.6× on this data. Options for the checkpoint: drop or defer some indexes (`template_id`, `pid`), FTS5 `detail=column` (no phrase queries), parse in worker processes (changes D10), or the DuckDB fallback from the risk table.
- Deviations from the list above: records use `id = source_id << 40 | seq` as rowid (no separate `(source_id, seq)` index); tags are interned in a `tags` table and FTS reads tag text through the view `records_v`; `ts_src` and `ts_raw` are stored only when they cannot be derived from `ts` (per-source `ts_format`); the `(source_id, ts_src)` and `level` indexes are partial (`ts_src IS NOT NULL`, `level IN ('W','E','F')`); a `processes` table holds the bugreport process list; `ts` is the device wall clock as written (time zone kept in source metadata, UTC mapping is the M5 clock model); kernel records in a bugreport keep `ts` null (only a minute-resolution boot-time estimate exists). Extra CLI commands `sources` and `reindex`. Item 10 (reuse by hash) was skipped.

## M2 Artifacts (2 days)

1. Parsers: `tombstone` (text), `anr` (thread states, lock holders, binder peer), `java_crash` (crash buffer and dropbox), `dropbox` (by tag, delegating bodies), `dumpstate_section` for metadata. `dumpsys`, `kernel_panic` and tombstone `.pb` move to M5, or earlier if an evaluation case needs one.
2. Crash signatures; synthetic record per artifact in its origin source.
3. Process list and properties feed pid → process and source metadata.

Tests: at least two tombstone variants (signal, abort), one ANR with a lock holder, one dropbox crash; golden summaries and signatures.

Done when: every tombstone, ANR and dropbox entry in a real bugreport appears as an artifact with a correct summary and a time on the timeline.

Status (Nov 9, 2026): built, verified on synthetic data only; the done criterion is not checked.

- Verified with synthetic fixtures (golden artifacts with summaries and signatures in `tests/golden/`, assertions in `tests/test_artifacts.py`): tombstones with a signal (SIGSEGV, fault address, cause) and an abort (abort message; `libc`/`libbase` frames skipped in the signature) and an unsymbolized one; an ANR whose main thread waits on a lock held by a sleeping thread, a deadlock, a native process block, and (in the bugreport) a lock holder blocked in a binder call whose peer is found in `BINDER TRANSACTIONS`; Java crashes from the crash buffer (`FATAL EXCEPTION`, also `IN SYSTEM PROCESS`) and from dropbox with nested causes; dropbox files (`.txt`, `.txt.gz`) with UTC → device time via `persist.sys.timezone`, a `dumpsys dropbox -p` section with several entries, and a generic entry (`system_server_wtf`). In the bugreport zip every artifact has a time and a record on the timeline whose `attrs.art` points back; citations `art:<id>[/section][:L<n>]` resolve to the original lines; reindexing replaces artifacts; the no-leak redaction test covers all artifact tables; a v1 case DB migrates to v2. Tests pass on Python 3.11 and 3.13 (Linux).
- **Not verified:** the done criterion, "every tombstone, ANR and dropbox entry in a real bugreport appears as an artifact with a correct summary and a time on the timeline": no real bugreport was available. Open points only a real sample can settle: the exact layout of `dumpsys dropbox -p` output and of dropbox headers on the target Android versions; which bugreports carry ANRs as `FS/data/anr/` files versus the `VM TRACES AT LAST ANR` section (both are parsed; if both are present the same ANR becomes two artifacts with the same signature); tombstone variants (MTE, GWP-ASan, `Cause:` lines, `x86` vs `arm64` registers); whether real lock holders and binder peers are found as often as in the fixtures (`BINDER TRANSACTIONS` is a snapshot at capture time, so a peer is only found while the call is still pending); redaction false positives in tombstone memory dumps.
- **Performance:** no measurable change (generated 100 MiB logcat with ~1,800 `FATAL EXCEPTION` records, same machine, two runs each: M1 32.8k/33.8k, M2 33.1k/33.0k lines/s).
- Deviations from the list above:
  - Schema version 2 (a migration step; `schema.sql` stays version 1) recreates the artifact tables, which v1 defined but never wrote: `artifacts` gains `tid`, `headline` and `lines`, `artifact_sections` gains `first_line` and `lines`. `L<line>` in a citation counts over the whole artifact (for a file: the line of the original); a section, when given, must contain the line. Ranges `:L<n>-L<m>` are accepted too.
  - A crash-buffer `FATAL EXCEPTION` gets no synthetic record: the record itself is the artifact's line (`artifacts.record_id`, `attrs.art`), so a crash is not counted twice in searches. All other artifacts get a synthetic record `[art:<id>] <headline>` in their origin source (the artifact file or bugreport section becomes a source of its connector, `tombstone`, `anr` or `dropbox`).
  - The connector interface (D11) is extended compatibly: `ParseItem` includes `RawArtifact`, `RawRecord.artifact` attaches one to a record, `ContainerContext.facts` carries data for completing artifacts. Artifact rows are written at the end of each imported file, after completion (process names, binder peer).
  - A bugreport zip's main entry (`main_entry.txt`) is read first, and what a nested container learns (fingerprint, time zone, properties) applies to later members of the enclosing archive. This changes source ids of bugreport zips relative to M1 (the bugreport text's sources come first).
  - `dumpstate_section` is a module, not a connector: section headers (`path: mtime` becomes source metadata `section_file`/`section_time`), properties, process list, uptime and binder transactions, used by the `bugreport` container.
  - Pid → process: the process list (`ps`) and the processes artifacts name (tombstones, every process an ANR dumps) fill missing process names of artifacts and the ANR binder peer; they are not written back to the `processes` table.
  - Redaction rules version 2: dropbox names `tag@<ms>.<ext>` are no longer taken for e-mail addresses (sources indexed with v1 rules show as stale).
  - CLI command `artifacts <case> [--type] [--show <citation>]`; `list_artifacts`/`read_artifact` over MCP stay M3.
  - New dependency `tzdata` on Windows only (zone data for dropbox times).

## M3 P0 tools (4 days)

1. **Filter object**: validation, relative times, citation references, globs, `attrs` keys, clear errors.
2. **Output**: compact text plus structured content; 200-character truncation with `…`; line and byte caps; "N more, narrow by …" footer; round-trip timestamps.
3. **Tools**: `add_source`, `list_sources`, `search` (FTS5 text, regex with SQL prefilter and capped scan, `collapse`, `count_only`, cursor), `context` (`scope=source|pid`), `list_artifacts` (grouped by signature), `read_artifact` (summary, section list, ranges), `get_records`.
4. Empty-result reasons; regex-looking query warning; search hits include the template's literal text, ready for a code search.
5. Fill the `defect_triage` prompt and instructions with the P0 workflow.

Tests: golden outputs per tool; every tool under 8 KB on a 1 GB log with a broad query; latency on 1 GB: text search < 1 s, `context` < 200 ms, filtered regex < 5 s.

Done when: in a real MCP client, on a real bugreport, an agent finds a known crash, reads its context and full stack trace, and cites lines that resolve correctly.

Status (Nov 9, 2026): built; verified on synthetic data and with a real MCP client; the done criterion is checked only on a synthetic bugreport.

- Verified: golden outputs (text and structured content) per tool on the bugreport zip in `tests/golden/tools__*.json`; filter resolution (every printed timestamp, short and full, selects its record again; `to` inclusive to its precision; relative times; citations with offsets; monotonic kernel times; ambiguous-year, mixed-clock and unknown-source errors; tag globs with suggestions; `not_tags`, `pid`, `attrs`); the regex prefilter returns the same hits as a full scan for a set of patterns; collapse, cursors (pages disjoint, in time order, complete; a cursor from another query is refused); caps hold with tiny configured caps for every tool; empty-result reasons; policy-blocked sources invisible to every tool; Python 3.11 and 3.13 (Linux; Windows only through CI). Over stdio, `tests/test_stdio.py` runs the P0 workflow with the SDK client and resolves every citation the tools printed. **Claude Code** (`claude -p` with the server over stdio) on the synthetic bugreport found the HVAC Java crash, read the full stack trace (from the dropbox copy, which `list_artifacts` marked as the same crash), read the process's lines before it with `context(scope='pid')` in 5 calls and 4.5 KB, and every citation in its answer resolved to the quoted line.
- **Not verified:** the done criterion on a *real* bugreport with a *known* crash (none available); whether agents use the tools well on real data (call counts, bytes) is for the M4 evaluation.
- **Performance** (generated 1 GiB threadtime logcat, 9.66M records, one core, `PERF_MB=1024 pytest -m perf -s`, best of 3 after one warm-up call): text search 80–550 ms (target < 1 s: met), including the broad queries `failed` (643k matches) and `com` (1.3M); `context` 1–2 ms (target < 200 ms: met); filtered regex 150 ms (15-min window) and 2.8 s (level E + tag, 643k candidates; target < 5 s: met); an unfiltered regex stops after 1M candidates in 2.4 s and says so; `list_artifacts` (19,260 Java crashes in 999 groups) 260 ms; `read_artifact` 23 ms; `get_records` (100 ids) 2 ms. Every tool's text stayed under 8 KB (largest 7.8 KB). Structured content is about twice the text (up to 18 KB of JSON). Without the sampling below, broad text searches took 1.3–1.8 s (≈ 1.3 µs per matching record for the lookup). Ingest in the same run: 305 s = 32k lines/s (M1 targets still not met).
- Deviations from the list above:
  - Tool logic lives in a new module `tools.py` (shared by the server and the CLI `query`), output helpers in `core/format.py`; `server.py` registers the tools. The `Filter` model moved to `tools.py` (same schema).
  - Compatible schema changes (D11): output schemas for every read tool; `context.at` also takes `art:<id>` (the artifact's record); `read_artifact.id` takes ranges `:L<n>-L<m>`; `start_line`/`end_line` are artifact line numbers (`L<n>`, as printed), not section-relative; `list_artifacts.type` is an enum; citations in `at`/`ids` must have a numeric source id (all source ids are numbers).
  - Output: text lines print `MM-DD hh:mm:ss.mmm` (or `123.456789` since boot), not the timestamp as written, so every printed time round-trips as `from`/`to` (the year comes from the case's range); structured content has the full `YYYY-MM-DD …`; `get_records` and `read_artifact` show the time as written. Results are in time order; records with only a monotonic clock come after wall-clock ones. A wall-clock `from`/`to` leaves monotonic-only sources out, with a warning.
  - Collapse groups by source, template, tag and level, ordered by first occurrence. **Broad text queries (> 300k FTS matches) are grouped from a rowid sample** (every k-th match): counts are marked `≈`, the result is `exact: false` and a warning says so; without a filter the total is the exact FTS count. Pages of broad matches walk the time index (> 20k matches), others sort the matches.
  - Regex: literal parts outside groups and classes (none with a top-level `|` or inline flags) become `instr` conditions and an FTS superset query; then at most `caps.regex_scan_rows` (new config key, default 1,000,000) candidates are scanned in Python.
  - `read_artifact` shows up to `2 × max_lines` lines; a single requested line is not cut at 200 characters.
  - The code-search text is the longest constant part of the template's first line; synthetic artifact records get none.
  - M2 open item (same crash as crash-buffer and dropbox artifact): both stay artifacts. `list_artifacts` groups by type and signature up to its first frame (the crash-buffer copy has fewer frames), shows the most detailed signature, and marks an artifact with the same pid within 10 s of an earlier one in its group as `same crash as art:<id>`; `read_artifact` lists the other copies with their line counts. The same applies to an ANR present as file and as bugreport section.
  - The CLI `query` prints the `search` tool's text (new flags `--from`, `--to`, `--not-tag`, `--collapse`, `--cursor`; without `--search`/`--regex` it lists every record that passes the filter). The M1 `query.Query`/`search`/`format_hit` are gone.

## M4 P1 tools and evaluation (5–6 days)

1. `overview`: level counts, top tags/processes/templates, segments, gaps > 30 s, error bursts (E/F per 10 s above 5× median), up to 60 histogram buckets, detector hit counts.
2. `count`: group by field, optional time buckets.
3. `check_citations`: validate every `log:` and `art:` citation in Markdown; warn on uncited paragraphs; errors on malformed ids. Used by the triage prompt before an answer is final, and by the evaluation.
4. `detect`: rule format (`match`, `level`, `tag`, `followed_by within`, `group_by pid`, `severity`, `note`); 8–10 default rules (Java crash, native crash, ANR, watchdog, LMK, SELinux denial, binder failure, VHAL error, service restart, boot not completed); rule directories from config.
5. **Docs:** how to use dlt-mcp-server in DLT Viewer for DLT until M5.
6. **Evaluation**, two levels. Expected answers (component, root-cause citations) are written into `cases.yaml` before any run and judged against the fix commit.
   - **Evidence recall (no agent):** for each closed defect in `eval/cases.yaml`, check that `detect`, `list_artifacts` or a recorded query surfaces the known root-cause citations in the first page. Runs in CI on redacted or synthetic cases.
   - **Agent runs:** the same cases, run by hand in at least one interactive MCP client with a checklist; record correctness of the root-cause direction, citation validity, tool calls and bytes from the call log. Optional baseline: the same agent with only shell tools on the unpacked files, to show what the server adds. A non-interactive runner is built only if the checkpoint says go.

Done when: both evaluations are run on 5 closed defects and written up.

## Checkpoint (after M4)

- **Go** to M5 if at least 3 of 5 agent runs point to the right component, `check_citations` passes on every answer, and the server beats the shell-only baseline on calls or bytes (if the baseline was run). With five cases this shows a direction, not proof.
- **Adjust** if agents spend many calls getting oriented (improve `overview`, rules, instructions) or evidence recall is low (parsers or rules miss things).
- **Stop or rethink** if logs alone rarely explain the defects; the bottleneck is then elsewhere (code, ticket data, domain knowledge).

## M5 DLT and multi-source (7–9 days)

1. **Clock model**: per segment `a`, `b`, `ts_conf`; batch recompute of `ts`.
2. **Automatic alignment**: anchors 1–3 from the design with residual error; `align` tool (`auto`, `anchor`, `offset`); results in `list_sources`.
3. **`timeline`** and `context(scope=all)`: k-way merge on `ts`, `~` for non-exact times.
4. **`dlt2jsonl`** (Rust, about 200–300 lines): reads DLT with `dlt-core` (`DltMessageReader`, optional `--fibex`), writes one JSON object per message (offset, storage time, ECU, ticks, session, app/ctx, type, level, verbose flag, arguments, payload text); static binaries for Linux, Windows and macOS from CI; Apache-2.0 notice shipped with the binary.
5. **`command` connector** (D13).
6. **DLT connector**: `dlt2jsonl` through `command`, maps lines to records; control messages to `attrs`; FIBEX files via `options`; unmatched non-verbose messages keep message id and payload hex; split/rotated files as one source.
7. **DLT file transfer** (`FLST/FLDA/FLFI`): reassembled from decoded arguments; known types parsed as artifacts, coredumps as metadata only.
8. **Remaining artifact parsers** (about 1 day): `dumpsys` (per service and heading), `kernel_panic` (pstore/last_kmsg, also as previous-boot kernel records), tombstone `.pb` when no text is present; fixture `dumpsys car_service`.
9. Benchmark on a real multi-GB DLT trace: time per stage (dlt-core, JSON decode, pipeline, SQLite), to decide whether more of the pipeline should move to Rust.

Tests: synthetic log pairs with known offset and drift (error < 10 ms for paired timestamps, < 500 ms for marker anchors); DLT fixtures per argument type; a non-verbose sample with FIBEX once available.

Done when: a case with a bugreport and a DLT trace shows both on one timeline with confidence, and an agent orders a cross-source event pair correctly in one eval case.

## M6 Finish (2–3 days)

1. `diff`: template sets per source or segment, only-in-A, only-in-B, count ratios above a threshold.
2. HTTP transport (streamable HTTP, token auth, server-side path policy for `add_source`).
3. Eval on 10 defects; release 0.1.0 with README, `docs/connectors.md`, `docs/rules.md`, changelog.

## Cross-cutting

- **Privacy:** no network calls in stdio mode; case DBs on the user's disk; call log without log content; only synthetic or fully redacted fixtures in the repo.
- **Versioning:** connector version and redaction-rules hash stored per source; changing either marks sources stale ("reindex recommended"); tool API per D11.
- **Licences:** repo MIT; `dlt-core` Apache-2.0 (compatible; keep its notice with `dlt2jsonl`); no code from MPL projects.
- **Portability:** paths, file locking and subprocess handling tested on Windows, since many automotive engineers work there.

## Risks

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Real logcat/bugreport variants break parsers | M1/M2 slip | Collect 10 real samples before M1; unparseable lines kept as raw records |
| FTS5 too slow or large on multi-GB logs | Tools time out | Measured in M1; DuckDB fallback behind `store/` |
| Redaction misses personal data | Logs cannot be used with agents | Default rules reviewed against a real sample; policy can block sources; CLI-only mode |
| Agents ignore tools or over-call | High cost, weak answers | Server instructions and prompt; tool descriptions tuned from call logs |
| Python ingest too slow for DLT-heavy cases | Long waits | Stage benchmark in M5; move pipeline stages to Rust behind the same interface |
| No FIBEX for non-verbose DLT | DLT payloads unreadable | Ask early; DLT Viewer export as fallback |
| Logs alone don't explain defects | Weak results | Checkpoint after M4; agents combine this server with code search and other tools |

## Inputs needed before M1

- 5–10 closed defects with logs and known root cause.
- 10 real log samples covering the formats that defects actually come with.
- A DLT sample, and whether it is verbose or non-verbose with FIBEX.
- Fields the default redaction rules must cover.

## Review outcome (Nov 8, 2026)

1. Python and SQLite FTS5 stay; ingest performance is measured in M1 (and per stage for DLT in M5), DuckDB or more Rust only if targets are missed (D1, D3).
2. One DB per case; originals not copied; path and hash stored, `relink` for moved files (D7).
3. No raw mode: redaction always on, at index time, before templates, and over call-log arguments (D5).
4. Default rules ship in the package; configured rule directories override by name (D8).
5. Checkpoint: 3 of 5 plus `check_citations` passing; expected answers written before the runs and judged against the fix commit.
6. No DLT in the first release; dlt-mcp-server covers DLT-only work until M5 (D12).
7. First artifact parsers: tombstone, ANR, Java crash, dropbox; `dumpsys`, kernel panic and tombstone `.pb` move to M5.
8. Logcat without a year: New Year rollover increments the year, no new segment.
