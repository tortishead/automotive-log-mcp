# automotive-log-mcp

An MCP server that lets any agent analyse a defect from automotive logs, with every finding cited
to a log line or artifact.

- **Reads:** logcat, kernel logs, Android bugreports (zip or text), archives, BSD syslog, and your
  own formats through YAML or an external decoder. DLT is not read yet ([docs/dlt.md](docs/dlt.md)).
- **Finds:** tombstones, ANRs, Java crashes and dropbox entries as artifacts with a summary and a
  crash signature; 10 detector rules for known failure patterns.
- **Cites:** every output line carries `log:<source>#<seq>` or `art:<id>`; `check_citations`
  verifies an answer before it is given.
- **Stays small:** outputs are capped (50 lines / 8 KB) and repeated messages collapse to one line.
- **Redacts** VINs, MACs, IPs, GPS and more at index time; no tool can return the raw value.

Status: **0.1.0**, not published. Verified on synthetic logs only, not yet on real defects.

## Install

Python 3.11+ with SQLite FTS5.

```bash
pip install -e ".[dev]"
```

## Run

```bash
automotive-log-mcp serve          # MCP over stdio
```

Client configuration: [examples/mcp-config/](examples/mcp-config/).

### Run over HTTP

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))" > ~/.automotive-log-mcp/token.txt
cp examples/http/config.yaml examples/http/policy.yaml ~/.automotive-log-mcp/   # then edit allowed_roots
automotive-log-mcp serve --http   # http://127.0.0.1:8765/mcp
```

Every request needs `Authorization: Bearer <token>`. `add_source` reads only below `allowed_roots`.
The server speaks plain HTTP: off loopback, put it behind a TLS proxy or an SSH tunnel.

## Tools

| Tool | What it returns |
| --- | --- |
| `add_source` | indexes a file, directory or zip into a case |
| `list_sources` | sources with format, record count, time range, fingerprint |
| `search` | records matching words or a regex, in time order |
| `context` | lines around a record or an artifact |
| `list_artifacts` | crashes and ANRs, grouped by signature |
| `read_artifact` | an artifact's summary, then a section or line range |
| `get_records` | full records, messages not cut |
| `overview` | levels, top tags and messages, gaps, error bursts |
| `count` | records by tag, level, pid, source or template, optionally per time bucket |
| `detect` | hits of the detector rules, by severity |
| `diff` | messages only in one of two runs, boots or time windows |
| `check_citations` | whether every citation in an answer resolves |

A hit looks like this:

```
log:2#18233  10-11 08:12:03.412  E 1234/1250 HvacCtrl  setProperty failed, status=NOT_AVAILABLE  ×37 until 10-11 08:14:51.003 (log:2#19001)
```

## Command line

The same tools work without an agent:

```bash
automotive-log-mcp index T-1 ~/bugreports/bugreport.zip
automotive-log-mcp sources T-1
automotive-log-mcp query T-1 --search "setProperty failed" --level W
automotive-log-mcp artifacts T-1 --show art:6
automotive-log-mcp detect T-1
automotive-log-mcp check T-1 answer.md
automotive-log-mcp calls              # where an agent session spent its tokens
```

`automotive-log-mcp <command> --help` lists the options.

## Files

State lives in `~/.automotive-log-mcp/` (override with `AUTOMOTIVE_LOG_MCP_HOME`): one SQLite DB
per case, `config.yaml`, `policy.yaml`, the redaction key and the call log. Original logs are
referenced by path and hash, never copied or changed.

## More

| Document | What |
| --- | --- |
| [docs/reference.md](docs/reference.md) | every tool, filter form, config key, storage, token use |
| [docs/connectors.md](docs/connectors.md) | add your own log format |
| [docs/rules.md](docs/rules.md) | write detector rules |
| [docs/dlt.md](docs/dlt.md) | working with DLT traces |
| [docs/implementation-plan.md](docs/implementation-plan.md) | milestones, status, next steps |
| [eval/](eval/README.md) | evaluation harness and cases |
| [CHANGELOG.md](CHANGELOG.md) | changes per release |

## Develop

```bash
ruff check . && ruff format --check . && python -m mypy && python -m pytest
```
