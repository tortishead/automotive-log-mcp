# MCP client configuration

The snippets in the first table start the server as `automotive-log-mcp serve` over stdio. If the command is not on
the client's `PATH` (e.g. installed in a virtualenv), use the full path to the executable, or
`python -m automotive_log_mcp serve` with the virtualenv's `python`. To use another home
directory, add `"env": {"AUTOMOTIVE_LOG_MCP_HOME": "/path/to/home"}` to the server entry.

| Client | File | Where it goes |
| --- | --- | --- |
| Claude Code | [claude-code.mcp.json](claude-code.mcp.json) | `.mcp.json` in the project root, or `claude mcp add automotive-log -- automotive-log-mcp serve` |
| Claude Desktop | [claude-desktop.json](claude-desktop.json) | merge into `claude_desktop_config.json` (Settings → Developer → Edit Config) |
| VS Code (GitHub Copilot agent mode) | [vscode-copilot.mcp.json](vscode-copilot.mcp.json) | `.vscode/mcp.json` in the workspace |
| Cursor | [cursor.mcp.json](cursor.mcp.json) | `.cursor/mcp.json` in the project, or `~/.cursor/mcp.json` |

On Windows, the same snippets work; `automotive-log-mcp` resolves to `automotive-log-mcp.exe`
in the virtualenv's `Scripts` folder.

## Over HTTP

With `automotive-log-mcp serve --http` running (see the README, [Run over HTTP](../../README.md#run-over-http)),
clients connect to `http://127.0.0.1:8765/mcp` and send the token as `Authorization: Bearer …`:

| Client | File | Verified |
| --- | --- | --- |
| Claude Code | [claude-code.http.mcp.json](claude-code.http.mcp.json) (`${AUTOMOTIVE_LOG_MCP_TOKEN}` is read from the client's environment) | yes, Claude Code 2.1.322 (`claude -p --mcp-config`) |
| VS Code (GitHub Copilot agent mode) | [vscode-copilot.http.mcp.json](vscode-copilot.http.mcp.json) (asks for the token once, stored by VS Code) | no: written from the VS Code MCP config format, not tried |

Server-side examples: [../http/config.yaml](../http/config.yaml) and
[../http/policy.yaml](../http/policy.yaml).
