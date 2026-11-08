# aaos-logs: implementation plan

Nov 8, 2026 · @Xuan · draft for review · based on [log-mcp-design.md](log-mcp-design.md)

## Goal and scope

Build the aaos-logs MCP server so that Copilot CLI can analyse a defect from the logs attached to a ticket, with every finding cited to a log line.

- **In (M0–M3):** logcat, bugreport, kernel and config-driven text logs; the P0 and P1 tools; local stdio server in Copilot CLI; redaction; call log; an evaluation on closed defects.
- **In (M4–M5):** DLT, cross-source alignment, `timeline`, `diff`, `check_citations`, HTTP transport.
- **Out:** a frontend, Jira/Gerrit integration, any server-side model call, multi-user server operation.

Efforts are for one developer, in working days, including tests.

## Decisions fixed by this plan

These are the choices the plan builds on. Each one is open for grilling; changing one changes the milestones that list it.

| # | Decision | Alternative | Affects |
| --- | --- | --- | --- |
| D1 | Python 3.11+, single package `aaos_logs` | Go or C++ for speed | all |
| D2 | MCP via the official Python `mcp` SDK (FastMCP), stdio first | Own JSON-RPC | M0, M5 |
| D3 | Store: stdlib `sqlite3` with FTS5, one DB file per case, WAL | DuckDB, Tantivy | M1 |
| D4 | Templates by regex masking (numbers, hex, UUIDs, paths) | Drain3 clustering | M1 |
| D5 | Redaction at index time; raw value never stored in the DB | Redact at output time | M1 |
| D6 | Citation id `log:<source>#<seq>`, seq 1-based per source | Global row id | M2 |
| D7 | Cases in `~/.aaos-logs/cases/<case>.db`; original files are not copied | Copy into case folder | M1 |
| D8 | Detector rules and regex connectors as YAML in the KB repo `aaos-kb/logs/` | In the server package | M3 |
| D9 | DLT parsed by ESR Labs `dlt-core` (Rust, Apache-2.0, verbose + FIBEX non-verbose) in a small sidecar binary `dlt2jsonl`; the Python connector reads its JSON lines | DLT Viewer export; PyO3 binding; own parser | M4 |
| D10 | Single process, single user; background indexing in a thread | Worker processes | M1 |

## Repository layout

```
aaos-logs/
  pyproject.toml              # deps: mcp, pyyaml; dev: pytest, ruff, mypy
  src/aaos_logs/
    server.py                 # MCP tool registration, arg validation, output caps
    cli.py                    # aaos-logs serve | index | query  (level 0: no AI needed)
    config.py                 # paths, caps, policy file loading
    policy.py                 # AI usage level, blocked sources, byte budget
    calllog.py                # JSONL call log
    model.py                  # RawRecord, Record, Source, Segment dataclasses
    store/
      schema.sql
      db.py                   # open/migrate case DB, batch writer
      query.py                # filter object -> SQL, FTS, regex, cursors
    core/
      ingest.py               # sniff -> container expand -> connector -> pipeline -> store
      levels.py               # level normalization
      templates.py            # masking + template ids
      redact.py               # redaction rules
      segments.py             # reboot / clock-jump detection
      clock.py                # per-segment mapping ts = a*ts_src + b  (M4)
      align.py                # anchors, fitting                         (M4)
      format.py               # compact output lines, truncation, citations
    connectors/
      base.py                 # Connector, Container protocols, registry
      logcat.py
      bugreport.py            # container
      kernel.py
      regex_yaml.py
      archive.py              # zip / tar / gz containers
      artifacts/              # M1: tombstone, anr, java_crash, dropbox, dumpsys, kernel_panic
                              # M4: dlt_file (DLT file transfer), coredump metadata
      dlt.py                  # M4
    detect/
      engine.py               # load YAML rules, run over store
  rules/                      # default rules, overridable from aaos-kb/logs/
  tests/
    fixtures/                 # small synthetic logs, one per format variant
    golden/                   # expected tool outputs
  eval/
    cases.yaml                # closed defects: files, expected root cause
    run_eval.py
```

## Milestones

| M | Name | Outcome | Effort |
| --- | --- | --- | --- |
| M0 | Skeleton | Copilot CLI lists the tools; `list_sources` answers on an empty case | 1 d |
| M1 | Ingest core | A bugreport zip is indexed into a case DB, including tombstones, ANRs, dropbox and dumpsys; CLI can query it | 6–7 d |
| M2 | P0 tools | Agent finds and reads errors and crash artifacts in one bugreport with citations | 4 d |
| M3 | P1 tools + pilot | `overview`, `detect`, `count`; skill + AGENTS.md; eval on 5 defects | 4–5 d |
| — | **Checkpoint** | Go / adjust / stop, based on eval and call log | — |
| M4 | Multi-source | DLT incl. file transfer, clock model, `align`, `timeline` | 6–8 d |
| M5 | Finish | `diff`, `check_citations`, HTTP transport, eval on 10 defects | 3–4 d |

Total to checkpoint: about 15–17 days. Total to M5: about 25–29 days.

## M0 Skeleton (1 day)

Tasks

1. `pyproject.toml`, ruff, mypy, pytest, CI job running all three.
2. `server.py` with FastMCP over stdio; tools registered as stubs with full parameter schemas from the design.
3. `config.py`: data dir, default caps (50 lines, 8 KB, 20 records), policy file path.
4. `calllog.py`: one JSONL line per call: time, case, tool, args, result lines, bytes, duration, error.
5. Copilot CLI setup snippet for `~/.copilot/mcp-config.json` in the README.

Done when: Copilot CLI shows the tools, `list_sources` returns an empty list, and the call log has the line.

## M1 Ingest core (6–7 days)

Tasks

1. **Model and schema** (`model.py`, `schema.sql`)
   - Tables: `sources`, `segments`, `records`, `templates`, `records_fts` (external-content FTS5 on `msg`, `tag`), `artifacts` (type, origin, ts, pid, process, summary JSON, signature, body), `artifact_sections` + `artifact_fts`, `reports` (later), `meta` (schema version).
   - Indexes: `(source_id, seq)` primary, `(ts)`, `(source_id, ts_src)`, `(tag)`, `(pid)`, `(template_id)`, `(level)`.
   - Schema version and migration on open.
2. **Batch writer** (`store/db.py`): 50k rows per transaction, WAL, `synchronous=NORMAL`, FTS filled in the same transaction.
3. **Pipeline** (`core/ingest.py`): sniff → container expand → connector → levels → templates → redact → segments → writer. Progress counter per source in `sources.index_state`.
4. **Connector registry** (`connectors/base.py`): protocols from the design; registry picks the highest `sniff()` score above 0.5, else error "unknown format" with the top 3 guesses.
5. **Connectors**
   - `logcat`: threadtime, time, brief, long; `-v year|epoch|monotonic|uid|UTC`; buffer markers; multi-line continuation.
   - `kernel`: `[  12.345678]`, `<6>[...]`, `dmesg -T`.
   - `archive`: zip, tar, gz members as streams.
   - `bugreport`: section splitting (`------ SYSTEM LOG`, `EVENT LOG`, `KERNEL LOG`, …), fingerprint, timezone, uptime; delegates sections to `logcat`/`kernel`.
   - `regex_yaml`: schema as in the design, validated on load.
6. **Artifact parsers** (`artifacts/`, about 2 days)
   - `tombstone` (text; `.pb` only if text is missing), `anr` (traces with thread states and lock holders), `java_crash` (from crash buffer and dropbox), `dropbox` (by tag, delegating bodies), `dumpsys` (split by service and heading), `kernel_panic` (pstore/last_kmsg).
   - Crash signature normalization; synthetic record per artifact in its origin source.
   - Process list and properties from dumpstate sections feed pid → process name and source metadata.
7. **Templates** (`core/templates.py`): masking regexes, `templates(id, text, count, first_seq, last_seq)`; template hash as key.
8. **Redaction** (`core/redact.py`): rules from policy YAML; stable hash suffix `<VIN#a3f1>`; per-source count of redactions in metadata.
9. **Segments** (`core/segments.py`): new segment on monotonic reset, on a backwards wall jump > 60 s, or on a boot marker.
10. **Background indexing**: `add_source` returns at once; one worker thread per case; queries on a source in progress read what is committed.
11. **CLI**: `aaos-logs index <case> <path>` and `aaos-logs query <case> --search ...` for testing without Copilot (also usable at AI level 0).
12. **Reuse by hash**: same file hash + connector version in another case → copy rows instead of reparsing (optional, cut if time is short).

Tests

- Artifact fixtures: at least two tombstone variants (signal and abort), one ANR with a lock holder, one dropbox crash, one `dumpsys car_service`; golden summaries and signatures.
- One fixture per format variant (20–200 lines each, synthetic, no real data), golden record output.
- Property tests for templates: masking is idempotent; two lines differing only in numbers get the same template.
- Redaction tests for each rule, including no false positives on build fingerprints and package names.
- Performance test on a generated 1 GB logcat: report lines/s and DB size; target ≥ 100k lines/s and DB ≤ 1.5× input.

Done when: a real bugreport zip indexes without errors, every tombstone/ANR/dropbox entry in it appears as an artifact with a correct summary, record counts match `wc -l` within multi-line merges, and the performance target holds (or the measured number is written down and accepted).

## M2 P0 tools (4 days)

Tasks

1. **Filter object** (`store/query.py`): parse and validate all fields; relative times (`+30s`, `log:2#18233-5s`); glob on tags/process; errors for invalid ranges.
2. **Output formatting** (`core/format.py`): compact line as in the design; truncation at 200 characters with `…`; byte and line caps; footer with "N more, narrow by …" hints; timestamps printed in a form `from`/`to` accept.
3. **Tools**
   - `add_source`, `list_sources`
   - `search`: text (FTS5 phrase/prefix) and regex (SQL prefilter by other filters, then Python `re` over candidates, capped scan with a "scanned N of M" note); `collapse`; `count_only`; cursor = last `(source_id, seq)`.
   - `context`: by citation or time; `scope=source|pid` (`all` arrives in M4).
   - `list_artifacts`: filter by type, time, process; groups repeats by signature.
   - `read_artifact`: summary first, then section list or line range; `dumpsys` never returned whole.
   - `get_records`: up to 20, full message and `attrs`.
4. **Empty-result reasons** and regex-looking query warning.
5. **Search hits include the template literal** for aaos-search queries.

Tests

- Golden outputs for each tool on fixtures.
- Cap tests: every tool stays under 8 KB on a 1 GB log with a broad query.
- Latency on the 1 GB log: `search` text < 1 s, `context` < 200 ms, regex with filters < 5 s.

Done when: in Copilot CLI, on a real bugreport, the agent finds a known crash, reads its context and the full stack trace, and cites lines that open to the right place.

## M3 P1 tools and pilot (4–5 days)

Tasks

1. `overview`: level counts, top 20 tags/processes/templates, segments, gaps > 30 s, error bursts (E/F per 10 s above 5× median), histogram with up to 60 buckets, detector hit counts; KB note pointers by tag.
2. `count`: group by field, optional time buckets.
3. `detect`: YAML rule format (`match`, `level`, `tag`, `followed_by within`, `group_by pid`, `severity`, `note`); 8–10 default rules (Java crash, native crash, ANR, watchdog, LMK, SELinux denial, binder failure, VHAL error, service restart, boot not completed).
4. **Policy** (`policy.py`): AI level 0/1/2, blocked sources, byte budget per session; level 0 disables the MCP server and leaves the CLI.
5. **KB side**: `skills/log-triage/SKILL.md` with the analysis order (list_sources → resolve_version → overview/detect → search → context → aaos-search for the log statement → answer with hypothesis/confirmed labels); AGENTS.md rules for log citations and alignment honesty.
6. **Evaluation harness** (`eval/`): `cases.yaml` with 5 closed defects (files, expected component, root-cause line ids); script runs Copilot CLI non-interactively if possible, otherwise a manual checklist; reads the call log for tokens and calls.

Done when: the eval on 5 closed defects is run and written up (correct direction per case, citations valid, calls and bytes per case).

## Checkpoint (after M3)

Decide with the eval and call log:

- **Go** to M4 if at least 3 of 5 cases point to the right component and citations are valid.
- **Adjust** if the agent spends many calls on orientation (improve `overview`, rules, skill) or answers are wrong for missing knowledge (KB notes first).
- **Stop or rethink** if logs alone are not enough for most cases (then the bottleneck is elsewhere, e.g. code search or ticket data).

## M4 Multi-source (6–8 days)

Tasks

1. **Clock model** (`core/clock.py`): per segment `a`, `b`, `ts_conf`; recompute `ts` in batches on change.
2. **Automatic alignment** (`core/align.py`): anchors 1–3 from the design (same device clocks; paired timestamps with least-squares fit; shared boot markers); report residual error.
3. **`align` tool** with `auto`, `anchor`, `offset`; results in `list_sources`.
4. **`timeline`** and `context(scope=all)`: k-way merge on `ts`, `~` for non-exact times.
5. **DLT connector** (about 3 days):
   - `dlt2jsonl` (Rust, about 200–300 lines): reads a DLT file with `dlt-core` (`DltMessageReader`, optional `--fibex` files), writes one JSON object per message to stdout: byte offset, storage time, ECU id, ECU ticks, session id, app/ctx ids, message type and level, verbose/non-verbose flag, decoded arguments, payload as text. Built as a static binary per platform in CI.
   - Python `dlt` connector: starts `dlt2jsonl` as a subprocess and maps each JSON line to a record; control messages go to `attrs`, not to `msg`.
   - FIBEX files are passed per case (`add_source(..., options={"fibex": [...]})`); messages without a FIBEX match keep their raw message id and payload hex.
   - DLT file transfer (`FLST/FLDA/FLFIN`) reassembled in Python from the decoded arguments (dlt-core has no file-transfer support).
6. **DLT file transfer**: reassemble `FLST/FLDA/FLFIN` into files, parse known types as artifacts, coredumps as metadata only.
7. **Interim**: document how pilot users connect dlt-mcp-server in DLT Viewer to Copilot CLI until our DLT connector lands.

Tests

- Synthetic pairs of logs with a known offset and drift; alignment error < 10 ms for paired timestamps, < 500 ms for marker anchors.
- DLT fixtures for each argument type; non-verbose sample once available.

Done when: a case with a bugreport and a DLT trace shows both sources on one timeline with confidence, and the agent correctly orders a cross-source event pair in one eval case.

## M5 Finish (3–4 days)

Tasks

1. `diff`: template sets per source/segment, only-in-A, only-in-B, count ratios above a threshold; capped.
2. `check_citations`: parse Markdown, validate every `log:` id exists; warn on uncited paragraphs; errors on malformed ids.
3. HTTP transport (streamable HTTP) behind a token, same tools; config switch.
4. Eval on 10 defects; update the write-up.

## Cross-cutting

- **Security and compliance:** no network calls from the server in stdio mode; case DBs on the engineer's disk; redaction rules reviewed by compliance before the pilot; call log contains arguments but no log content.
- **Fixtures:** only synthetic or fully redacted logs in the repo.
- **Versioning:** connector versions stored per source; bumping one marks sources stale and `list_sources` says "reindex recommended".
- **Docs:** README (install, Copilot setup, CLI), `docs/connectors.md` (how to add one), `docs/rules.md` (detector YAML).

## Risks

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Real logcat/bugreport variants break the parsers | M1 slips | Collect 10 real samples in week 1; unknown lines become records with `level=null` instead of errors |
| FTS5 too slow or too big on multi-GB logs | Tools time out | Measured in M1; fallback to DuckDB behind `store/` |
| Redaction misses personal data | Compliance block | Compliance reviews rules and a redacted sample before the pilot; level 0 CLI as fallback |
| Copilot ignores tools or over-calls | High cost, weak answers | Skill with fixed order; tool descriptions tuned from the call log |
| DLT non-verbose without FIBEX access | No DLT decoding | Use DLT Viewer export or dlt-mcp-server; ask early |
| Logs alone don't explain defects | POC looks weak | Checkpoint after M3; combine with aaos-search and KB notes |

## Needed from others before or during M1

- 5–10 closed defects with logs and known root cause (eval set).
- 10 real log samples covering the formats tickets actually carry.
- Compliance: redaction list and approval of AI level 1 for logs.
- DLT: verbose or non-verbose, a sample trace, FIBEX availability.

## Questions to grill

1. Is Python fast enough, or do we expect multi-GB logs from day one (D1, D3)?
2. One DB per case and no copy of originals: fine for how logs are stored today (D7)?
3. Redact at index time means engineers never see raw values through the tools. Acceptable, or do they need a raw mode at level 0 (D5)?
4. Should detector rules and regex connectors live in the KB repo or in this repo (D8)?
5. Is the checkpoint threshold (3 of 5) right, and who judges the answers?
6. Do we need DLT in the first pilot, which would move M4 before M3?
