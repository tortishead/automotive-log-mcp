# MCP client configuration

All snippets start the server as `automotive-log-mcp serve` over stdio. If the command is not on
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
