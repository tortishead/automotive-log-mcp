# Client setup

These files start the server as `automotive-log-mcp` over stdio. If the command is not on the
client's `PATH` (for example, in a virtualenv), use its full path, or `python -m automotive_log_mcp`
with the virtualenv's Python. For another home folder, add
`"env": {"AUTOMOTIVE_LOG_MCP_HOME": "/path/to/home"}` to the server entry.

| Client | File | Put it in |
| --- | --- | --- |
| Claude Code | [claude-code.mcp.json](claude-code.mcp.json) | `.mcp.json` in the project, or run `claude mcp add automotive-log -- automotive-log-mcp` |
| Claude Desktop | [claude-desktop.json](claude-desktop.json) | `claude_desktop_config.json` (Settings → Developer → Edit Config) |
| VS Code Copilot (agent mode) | [vscode-copilot.mcp.json](vscode-copilot.mcp.json) | `.vscode/mcp.json` in the workspace |
| Cursor | [cursor.mcp.json](cursor.mcp.json) | `.cursor/mcp.json` in the project, or `~/.cursor/mcp.json` |

On Windows the same files work.
