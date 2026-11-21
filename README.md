# automotive-log-mcp

An MCP server for automotive logs and traces. It indexes, searches and reads logs, and every
output line points to a log line or a crash file.

- **Reads** logcat, kernel logs, Android bugreports (zip or text), archives, syslog, and your own
  formats.
- **Finds** tombstones, ANRs, Java crashes and dropbox entries, and groups copies of the same
  crash.
- **Cites** every output line as `log:<source>#<line>` or `art:<id>`.
- **Keeps output small:** at most 50 lines or 8 KB per call; repeated lines are shown once with a
  count.

## Install

Python 3.11+ with SQLite FTS5.

```bash
pip install -e ".[dev]"
```

## Run

```bash
automotive-log-mcp                # MCP over stdio
```

Client setup (Claude Code, VS Code Copilot, Cursor, …): [examples/mcp-config/](examples/mcp-config/).

## Tools

| Tool | Returns |
| --- | --- |
| `add_source` | indexes a file, folder or zip into a case |
| `list_sources` | the case's sources: format, line count, time range |
| `overview` | levels, top tags and messages, gaps, error bursts |
| `list_artifacts` | crashes and ANRs, grouped by crash |
| `read_artifact` | a crash's summary, then a section or lines |
| `search` | lines that match words or a regex, in time order |
| `context` | lines around a line or a crash |
| `get_records` | full lines, nothing cut |
| `count` | lines per tag, level, pid, source or message, optionally per time slot |

A search hit:

```
log:2#18233  10-11 08:12:03.412  E 1234/1250 HvacCtrl  setProperty failed, status=NOT_AVAILABLE  ×37 until 10-11 08:14:51.003 (log:2#19001)
```

## Command line

Every tool is also a CLI command, no agent needed. It prints the same text an agent gets.

```bash
automotive-log-mcp add_source br ~/bugs/bugreport.zip          # index a log into case "br"
automotive-log-mcp list_sources br
automotive-log-mcp overview br
automotive-log-mcp list_artifacts br                           # crashes and ANRs
automotive-log-mcp read_artifact br art:1                        # summary, then a section
automotive-log-mcp search br "setProperty failed" --level E --from -5m
automotive-log-mcp context br log:3#120 --before 20
automotive-log-mcp count br --by tag --bucket 1m
```

- `automotive-log-mcp --help` lists the commands; `automotive-log-mcp <tool> --help` lists a
  command's options.
- `--json` prints JSON instead of text.
- Exit code: 0 ok, 1 tool error, 2 wrong usage.

More: [Command line](docs/reference.md#command-line).

## Files

Everything lives in `~/.automotive-log-mcp/` (change it with `AUTOMOTIVE_LOG_MCP_HOME`): one
SQLite file per case, `config.yaml`, `policy.yaml`, the redaction key and the call log. Your log
files are never copied or changed.

## Docs

| Doc | Content |
| --- | --- |
| [docs/walkthrough.md](docs/walkthrough.md) | one session that uses every tool, with real output |
| [docs/reference.md](docs/reference.md) | tools, filters, config, storage, token use |
| [docs/connectors.md](docs/connectors.md) | add your own log format |

## Develop

```bash
ruff check . && ruff format --check . && python -m mypy && python -m pytest
```
