# automotive-log-mcp

An MCP server that lets any agent analyse a defect from automotive logs (Android bugreport,
logcat, kernel logs), with every finding cited to a log line or artifact.

Status: **M0 skeleton**. All P0 tools are listed with their full parameter schemas;
`list_sources` answers, the other tools report the milestone that brings them. See
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
[examples/mcp-config/](examples/mcp-config/).

## Files

All state lives under `~/.automotive-log-mcp/` (override with `AUTOMOTIVE_LOG_MCP_HOME`):

| Path | What |
| --- | --- |
| `config.yaml` | optional: `cases_dir`, `policy_file`, `call_log` (null disables), `caps`, `rule_dirs` |
| `policy.yaml` | optional: `allowed_tools`, `blocked_sources`, `blocked_ecus`, `byte_budget`, `allow_code_connectors` |
| `cases/<case>.db` | one SQLite DB per case (from M1) |
| `calllog.jsonl` | one line per tool call: tool, arguments, status, duration, output bytes; never log content |

Example `policy.yaml`:

```yaml
allowed_tools: [list_sources, search, context, list_artifacts, read_artifact, get_records]
byte_budget: 500000
```

## Develop

```bash
ruff check . && ruff format --check . && mypy && pytest
```
