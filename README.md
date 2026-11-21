# automotive-log-mcp

An MCP server for automotive logs and traces. It indexes, searches and reads logs, and every
output line points to a log line or a crash file.

- **Reads** logcat, kernel logs, Android bugreports (zip or text), archives, syslog, and your own
  formats.
- **Finds** tombstones, ANRs, Java crashes and dropbox entries, and groups copies of the same
  crash.
- **Cites** every output line as `log:<source>#<line>` or `art:<id>`. `check_citations` checks an
  answer before it is given.
- **Keeps output small:** at most 50 lines or 8 KB per call; repeated lines are shown once with a
  count.
- **Hides personal data** (VINs, MACs, IPs, GPS, …) when it indexes a log. No tool returns the raw
  values.

Status: version 0.1.0, not published. Tested on synthetic logs only, not yet on real logs.

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

### From the command line

Every tool is also a command. It prints the same text an agent gets.

```bash
automotive-log-mcp add_source br ~/bugs/bugreport.zip
automotive-log-mcp list_artifacts br
automotive-log-mcp search br "setProperty failed" --level E --from -5m
automotive-log-mcp check_citations br answer.md   # exit 1 on FAIL
```

`automotive-log-mcp <tool> --help` lists the options. `--json` prints JSON.

### Over HTTP

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))" > ~/.automotive-log-mcp/token.txt
cp examples/http/config.yaml examples/http/policy.yaml ~/.automotive-log-mcp/   # then set allowed_roots
automotive-log-mcp --http         # http://127.0.0.1:8765/mcp
```

Every request needs `Authorization: Bearer <token>`. `add_source` reads only below `allowed_roots`.
There is no TLS: outside your own machine, use a TLS proxy or an SSH tunnel.

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
| `check_citations` | whether every citation in an answer is valid |

A search hit:

```
log:2#18233  10-11 08:12:03.412  E 1234/1250 HvacCtrl  setProperty failed, status=NOT_AVAILABLE  ×37 until 10-11 08:14:51.003 (log:2#19001)
```

## Files

Everything lives in `~/.automotive-log-mcp/` (change it with `AUTOMOTIVE_LOG_MCP_HOME`): one
SQLite file per case, `config.yaml`, `policy.yaml`, the redaction key and the call log. Your log
files are never copied or changed.

## Docs

| Doc | Content |
| --- | --- |
| [docs/reference.md](docs/reference.md) | tools, filters, config, storage, token use |
| [docs/connectors.md](docs/connectors.md) | add your own log format |

## Develop

```bash
ruff check . && ruff format --check . && python -m mypy && python -m pytest
```
