# Changelog

All notable changes to automotive-log-mcp. Versions follow semantic versioning for the tool names
and parameters and the connector interface (D11 in the implementation plan): a breaking change
there needs a new major version. Output text, defaults and the case DB layout may change in minor
versions; a case DB of an older schema is migrated on first open.

## [0.1.0] - 2026-11-10

First release (milestones M0 to M6 of [docs/implementation-plan.md](docs/implementation-plan.md)).
Not published to PyPI and not tagged; install from the repository (`pip install -e .`).

### Added

- **MCP server** over stdio (default) and, new in M6, over streamable HTTP from the official MCP
  SDK (`serve --http`): one bearer token (`AUTOMOTIVE_LOG_MCP_TOKEN` or `http.token_file`), the
  SDK's DNS-rebinding protection on loopback and for `http.allowed_hosts`, server instructions and
  a `defect_triage` prompt.
- **Fourteen tools**, each with capped (50 lines / 8 KB by default), cited output and strict
  arguments (unknown arguments are an error with a hint):
  - P0: `add_source`, `list_sources`, `search` (FTS5 words or regex, collapse, cursors),
    `context` (`scope` = `source`, `pid`, `all`), `list_artifacts`, `read_artifact`, `get_records`;
  - P1: `overview`, `count`, `detect`, `check_citations`;
  - multi-source: `timeline`, `align`;
  - M6: `diff`: messages only in B, only in A, and those whose share of the side's records changed
    by `min_ratio` or more, between two sides (sources, a segment such as one boot, or a time
    window); every item cited; sides may be on different clocks.
- **Formats:** logcat (all `-v` formats and modifiers), kernel logs (`dmesg` variants,
  `/dev/kmsg`), Android bugreports (zip or text, sections, fingerprint, time zone, process list,
  binder transactions), archives (zip, tar, gz, bz2, xz), BSD syslog (a shipped YAML connector).
- **Artifacts:** tombstones (text), ANR traces (lock holders, deadlocks, binder peers), Java
  crashes, dropbox entries; crash signatures; a record per artifact on the timeline.
- **Own formats:** YAML regex connectors, `command` connectors (an external program writes JSON
  lines: records, artifacts, source metadata), Python entry points (`automotive_log_mcp.connectors`,
  when the policy allows code connectors). Guide: [docs/connectors.md](docs/connectors.md).
- **Clock model and alignment** (schema 5): every source mapped onto one case clock per segment
  (offset, drift, pieces), from manual anchors, same lines in two sources, UTC times in kernel
  lines, marker rules and the bugreport boot time; confidence and an error bound per source.
- **Detector rules** (10 default rules) and **marker rules** in YAML, from the package and from
  configured rule directories. Reference: [docs/rules.md](docs/rules.md).
- **Redaction** at index time with keyed-hash tokens (VIN, IMEI, MAC, IPv4, GPS, e-mail, phone,
  serial, SSID), also over artifacts and call-log arguments; no raw mode.
- **Policy file:** `allowed_tools`, `blocked_sources`, `byte_budget`, `allow_code_connectors`,
  and, new in M6, `allowed_roots` (directories `add_source` and `index` may read; required over
  HTTP, where an omitted list refuses every path).
- **Call log** with sizes, token estimates, hashes and citation ids (never log content), one file
  and a report per round, and `calls` to analyse a session (optionally with a Claude Code
  transcript).
- **CLI** without an agent: `index`, `sources`, `query`, `artifacts`, `relink`, `reindex`,
  `overview`, `count`, `detect`, `check`, `rules`, `timeline`, `align`, `diff`, `calls`, `serve`.
- **Evaluation harness** ([eval/](eval/)): evidence recall over case files with expected
  root-cause citations (and, new in M6, `timeline`/`diff` queries and an order check on the case
  clock), 10 synthetic cases, an agent-run checklist.

### Changed in M6

- The alignment error is never below the timestamps' resolution (1 ms for logcat); exact
  agreement of paired lines used to print `±0 ns`.
- `uvicorn` and `starlette` are direct dependencies (they were already required by `mcp`).

### Known limitations

- Verified on synthetic fixtures and synthetic evaluation cases only: no real bugreport, no real
  closed defect, and no agent run of the evaluation (the M4 and M6 done criteria are not met).
- Ingest runs at about 26–42k lines/s on generated logs (target 100k lines/s).
- Not read: DLT (use a `command` connector; see [docs/dlt.md](docs/dlt.md)), tombstone `.pb`,
  `dumpsys` sections, kernel panics / `pstore`. `blocked_ecus` in the policy is not enforced.
- HTTP: plain HTTP only (use a TLS proxy or an SSH tunnel off loopback), one token, no OAuth, no
  users; the byte budget and call-log session are per server process.
- VS Code Copilot has not been tried as a client (over stdio or HTTP); its config snippets are
  written from its documented format.
