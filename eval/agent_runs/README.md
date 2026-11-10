# Agent runs

The agent-level evaluation, run by hand in an interactive MCP client on the cases of
`eval/cases.yaml` and `eval/cases.local.yaml`. A non-interactive runner is built only if the
checkpoint says go (plan, M4 item 6).

Write each case's expected answer into the case file **before** the first run on it, and do not
change it after seeing an agent's answer. Record runs in `eval/agent_runs/local/` (git-ignored):
one copy of [TEMPLATE.md](TEMPLATE.md) per run, named `<case>-<client>-<n>.md`.

## Setup (once)

1. Install the server into the Python the client will start (Windows: `py -m pip install -e .`
   in the repository; Linux: `python -m pip install -e .`).
2. Index each case as described in [../README.md](../README.md#add-a-real-case) (or run
   `evidence_recall.py --home <your home>` once, which indexes every case under its id).
3. Configure the client. The call log must be on (it is by default: `calllog.jsonl` in the
   server home).

   **VS Code, GitHub Copilot agent mode (Windows):** `.vscode/mcp.json` in the workspace you open
   for the runs (not necessarily this repository):

   ```json
   {
     "servers": {
       "automotive-log": {
         "type": "stdio",
         "command": "py",
         "args": ["-m", "automotive_log_mcp", "serve"]
       }
     }
   }
   ```

   Start the server from the MCP view (or the `Start` code lens in `mcp.json`), open the Chat view
   and pick **Agent** mode. Check under *Configure Tools* that the `automotive-log` tools are on
   and other tools that could read the log files directly (terminal, file search) are **off** for
   the server runs; they are what the baseline uses.

   **Claude Code:** `.mcp.json` from [../../examples/mcp-config/claude-code.mcp.json](../../examples/mcp-config/claude-code.mcp.json)
   (on Windows with `"command": "py", "args": ["-m", "automotive_log_mcp", "serve"]`), or
   `claude mcp add automotive-log -- py -m automotive_log_mcp serve`. For a run with only the
   server's tools, start it in a folder without the log files and deny the file and shell tools:
   `claude --strict-mcp-config --mcp-config .mcp.json --disallowedTools "Bash,Read,Grep,Glob,Edit,Write,WebFetch,WebSearch"`.

## One run

1. Start a **new** chat (a new server process = a new call-log session; this keeps runs apart).
   Note the time (UTC) in the template.
2. Use the triage prompt: in VS Code type `/` and pick `mcp.automotive-log.defect_triage`
   (Claude Code: `/mcp__automotive-log__defect_triage`) with the case id and the symptom exactly
   as in the case file. If the client has no prompt support, paste the text that
   `defect_triage` returns (the same steps) with the case and symptom.
3. Do not help the agent. If it asks a question, answer only with what the defect report says;
   note every intervention in the template.
4. When it answers, save the answer as Markdown (`<case>-<client>-<n>.answer.md`) and check it
   yourself, whether or not the agent called check_citations:

   ```bat
   py -m automotive_log_mcp check T-4711 eval\agent_runs\local\T-4711-copilot-1.answer.md
   ```

   ```bash
   python -m automotive_log_mcp check T-4711 eval/agent_runs/local/T-4711-claude-1.answer.md
   ```

   Exit status 0 = PASS (every citation well-formed and resolving; warnings for uncited paragraphs
   and quotes are listed but allowed).
5. Take calls and bytes from the call log:

   ```bat
   py eval\callstats.py --case T-4711 --since 2026-11-10T09:00 --markdown
   ```

   ```bash
   python eval/callstats.py --case T-4711 --since 2026-11-10T09:00 --markdown
   ```

6. Judge the answer against the case's expected component and root cause (and the fix commit),
   fill in the checklist, and move on to the next case.

## Checklist (per run)

- [ ] Expected answer was in the case file before this run (git log or file date)
- [ ] New chat / new server session; only the server's tools enabled (or: baseline, only shell)
- [ ] Prompt: `defect_triage` with the case id and the symptom as written in the case file
- [ ] Interventions: none, or each one noted
- [ ] **Component**: right / partly (right subsystem, wrong module) / wrong / no answer
- [ ] **Root-cause direction**: right / partly / wrong
- [ ] **Root-cause citations**: which of the expected citations the answer cites
- [ ] **check_citations**: PASS / FAIL (`automotive-log-mcp check`), errors and warnings counted
- [ ] Agent called check_citations itself before answering: yes / no
- [ ] Calls, bytes, errors, check_citations calls (from `callstats.py`)
- [ ] Wall time from prompt to answer
- [ ] What the agent spent calls on before it was oriented (for "adjust": overview, rules, instructions)

## Optional baseline: shell tools only

The same case and prompt text, but the agent gets the unpacked log files and only shell / file
tools (no MCP server): in VS Code, disable the `automotive-log` server and allow the terminal; in
Claude Code, `claude --strict-mcp-config` (no MCP servers) in a folder with the unpacked logs.
Count its tool calls and the bytes it read (from the transcript; there is no call log). Compare
component, root cause and calls/bytes with the server runs. The checkpoint asks whether the
server beats the baseline on calls or bytes.

## Summary table (for the write-up)

| case | client | component | root cause | expected cites found | check_citations | calls | bytes | baseline calls / bytes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| | | | | | | | | |

Go to M5 if at least 3 of 5 runs point to the right component, check_citations passes on every
answer, and the server beats the baseline on calls or bytes (if run). Adjust if agents spend
many calls getting oriented or evidence recall is low; stop or rethink if the logs alone rarely
explain the defects (plan, "Checkpoint").
