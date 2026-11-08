# automotive-log-mcp: design (PoC)

Nov 8, 2026 · @Xuan · draft

## What it is

An MCP server that gives any agent (Copilot, Claude, Cursor, custom SDK agent) tools to analyse automotive logs. PoC goal: **defect analysis on one Android bugreport** — find what went wrong, read the evidence around it, and cite every finding to the log line it comes from.

## Key decisions

- **One tool API, per-format connectors behind it.** Connectors only turn bytes into records. Everything after is shared: one record model, one index, one tool set.
- **Deterministic server.** Parses, indexes, searches. Never calls a model; reasoning and summaries are the agent's job.
- **Every output line is cited** (`log:<source>#<seq>` or `art:<id>`), the log equivalent of `path:line`.
- **Capped outputs.** Default 50 lines / ~8 KB, say when capped and how to narrow. Repeated messages collapse into one line with count and first/last time — the main token saver.
- **Redaction at index time.** Regex rules (VIN, IMEI, MAC, GPS, …) replace values with tokens before anything is stored, so no tool can return the raw value.

```
 bugreport zip / logcat / dmesg
        │
  connectors (sniff + parse)
        │ records + artifacts
  core: normalize → redact → template → SQLite + FTS5 (one file per case)
        │
  MCP tools (stdio) → agent
```

## Concepts

- **Case**: sources for one investigation (e.g. a ticket). One SQLite file.
- **Source**: one log stream. A bugreport expands into several (main, system, events, crash, kernel).
- **Record**: one normalized log line.
- **Artifact**: a document that is not a line stream — tombstone, ANR trace, Java crash. Stored whole with parsed key fields; also adds one synthetic record at its time so search and context see it.
- **Template**: message with variable parts replaced (`status=<*>`). Used for collapse.

## Record model

| Field | Notes |
| --- | --- |
| `source_id`, `seq` | citation; `seq` 1-based per source |
| `ts_raw`, `ts` | timestamp as written; parsed to ns (null if none) |
| `level` | `V D I W E F`, null if unknown |
| `tag`, `pid`, `tid` | null if the format has none |
| `msg`, `template_id` | message after redaction; its template |
| `attrs` | JSON for anything format-specific |

## MCP tools (PoC)

| Tool | What it does |
| --- | --- |
| `add_source(case, path, format?)` | Register a file or zip; sniff format; index. Returns source ids. |
| `list_sources(case)` | Format, record count, time range, build fingerprint. |
| `search(case, query, mode=text\|regex, filter?, collapse=true, count_only?, limit, cursor)` | FTS5 or regex over `msg` and `tag`. |
| `context(case, at, before=20, after=20)` | Lines around a record. |
| `list_artifacts(case, type?)` / `read_artifact(case, id, section?)` | Tombstones, ANRs, crashes: summary, then sections. |
| `get_records(case, ids)` | Full records (other tools cut messages at ~200 chars). |

Filter: `sources`, `from`/`to`, `level` (minimum), `tags`, `not_tags`, `pid`. Timestamps printed in outputs can be passed back as `from`/`to` unchanged. Empty results say why; invalid params fail loudly.

Output:

```
log:2#18233  10-11 08:12:03.412  E 1234/1250 HvacCtrl  setProperty failed, status=NOT_AVAILABLE   ×37 (08:12:03–08:14:51)
```

## Connectors (PoC)

- `bugreport` (container): splits sections, reads fingerprint and timezone, hands tombstones / ANR / dropbox files to artifact parsers.
- `logcat`: `threadtime` format, multi-line stack traces.
- `kernel`: `dmesg` format.
- Artifact parsers: `tombstone` (text), `anr`, `java_crash`.

```python
class Connector(Protocol):
    name: str
    def sniff(self, head: bytes, filename: str) -> float: ...   # 0..1
    def parse(self, stream: BinaryIO, ctx: SourceContext) -> Iterator[RawRecord | RawArtifact]: ...  # streaming
```

## Large files

Stream, never load whole files. Batched SQLite inserts with WAL; FTS5 filled in the same pass. Collapse + caps keep tokens flat.

## Stack

Python 3.11+, stdlib `sqlite3` (FTS5), official `mcp` SDK, `pyyaml`. Local stdio only; case files in `~/.automotive-log-mcp/cases/`. Original logs never modified.

## Not in PoC

DLT (planned via `dlt-core` sidecar), multi-source time alignment, `overview` / `detect` / `count` / `timeline` / `diff` / `check_citations`, `dumpsys` and kernel-panic artifacts, policy file and call log, HTTP transport, background indexing, user-added formats (YAML regex connectors, third-party plugins). Add after PoC proves useful on real defects.

## Open questions

- Which logs come with a typical defect (bugreport, plain logcat, DLT, vendor logs)?
- Typical and worst-case size per case.
- Default redaction list.
