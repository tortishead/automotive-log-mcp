# Evaluation

Does the server help an agent find the cause of real defects? Two checks, on the same cases:

1. **Evidence recall** (no agent): do `overview`, `list_artifacts` and each case's saved queries
   show the known root-cause lines on their first page? Script: `evidence_recall.py`. Runs in the
   test suite on the synthetic cases.
2. **Agent runs** (by hand): an agent in a real client triages each case. You note whether it
   found the right component, whether `check_citations` passes, and how many calls and bytes it
   used.

| File | Content |
| --- | --- |
| `cases.yaml` | 9 synthetic cases with expected answers |
| `cases.local.yaml` | your real defects (git-ignored, you create it) |
| `evidence_recall.py` | evidence recall over one or more case files |
| `callstats.py` | calls, bytes and tools per session, from the call log |
| `agent_runs/TEMPLATE.md` | notes for one agent run; copies go in `agent_runs/local/` (git-ignored) |
| `results/` | your reports (git-ignored) |

Status: only the synthetic cases have been run. They test the script, not the server: their
expected lines come from how the logs were written. No agent run has been done.

## Add a real case

Use a **closed** defect whose cause is known from its fix commit. Write the expected answer
**before** you run anything on it, and judge against the fix, not against what the tools show.

1. Put the defect's logs in a stable place outside this repo. Cases reference files by path; they
   do not copy them.
2. Add the case to `eval/cases.local.yaml`:

   ```yaml
   version: 1
   cases:
     - id: T-4711                       # ticket id: letters, digits, '.', '_', '-'
       title: HVAC app crash on temperature change
       symptom: The climate app closes when the temperature is set   # as the ticket says it
       files:                           # indexed in this order
         - C:/defects/T-4711/bugreport-2026-10-11.zip
       expected:
         component: com.example.hvac (HvacController)
         root_cause: >-
           setProperty returns NOT_AVAILABLE before VHAL is ready; the app dereferences the result.
         citations:                     # filled in step 4
           - {cite: log:2#18233, text: "setProperty failed, status=NOT_AVAILABLE"}
       queries:                         # optional, also written before any run
         - {tool: search, args: {query: "setProperty failed", filter: {level: E}}}
       fix: abc1234 (repo hvac-app)
   ```

3. Index the files with `add_source` into a case named after the id, **in the listed order**
   (source ids depend on the order).
4. Find the root-cause lines **from the fix commit** with `search`, `read_artifact` or
   `get_records`. Write each citation and a short text the line must contain. The text check
   catches citations that drift. Never pick citations after seeing what an agent found.

`queries` take the tools `search`, `count`, `context`, `list_artifacts`, `read_artifact`,
`get_records` and `overview`, with their MCP arguments.

## Run evidence recall

```bash
python eval/evidence_recall.py eval/cases.yaml eval/cases.local.yaml --home ~/.automotive-log-mcp \
  --report eval/results/recall.md --json eval/results/recall.json
```

On Windows use `py eval\evidence_recall.py …` with `--home %USERPROFILE%\.automotive-log-mcp`.

- Without `--home`, cases go into a temporary home that is deleted after. With `--home`, existing
  cases are reused, so agent runs later see the same source ids.
- The report shows per case: recall, which expected lines were printed (exactly or via their
  artifact), which were missed, which tool printed them, and the bytes per call.
- The script exits with 1 if an expected citation does not exist or its line lacks the text.
  `--min-recall 1` also fails on any miss.

Synthetic result: 17 of 18 root-cause lines found (13 exact, 4 via their artifact). The miss is
`syn-boot-selinux` `log:1#4`.

## Agent runs

### Set up once

1. Install the server into the Python the client starts: `python -m pip install -e .`
   (Windows: `py -m pip install -e .`).
2. Index each case (step 3 above, or run `evidence_recall.py --home …` once).
3. Set up the client. Keep the call log on (it is by default).
   - **VS Code Copilot (agent mode):** `.vscode/mcp.json` in the workspace you open:

     ```json
     {"servers": {"automotive-log": {"type": "stdio", "command": "py", "args": ["-m", "automotive_log_mcp"]}}}
     ```

     Start the server from the MCP view and pick **Agent** mode in Chat. Under *Configure Tools*,
     turn on the `automotive-log` tools and turn **off** tools that read files directly
     (terminal, file search).
   - **Claude Code:** use [examples/mcp-config/claude-code.mcp.json](../examples/mcp-config/claude-code.mcp.json)
     and block the file and shell tools, in a folder without the logs:
     `claude --strict-mcp-config --mcp-config .mcp.json --disallowedTools "Bash,Read,Grep,Glob,Edit,Write,WebFetch,WebSearch"`.

### One run

1. Start a **new** chat and note the time (UTC).
2. Run the triage prompt with the case id and the symptom exactly as in the case file: VS Code
   `/mcp.automotive-log.defect_triage`, Claude Code `/mcp__automotive-log__defect_triage`.
3. Do not help. If the agent asks, answer only with what the ticket says, and note it.
4. Save the answer as `<case>-<client>-<n>.answer.md`. If the agent did not run
   `check_citations`, ask it once and note the result.
5. Get calls and bytes:

   ```bash
   python eval/callstats.py --case T-4711 --since 2026-11-10T09:00 --markdown
   ```

   The round report in `~/.automotive-log-mcp/rounds/` explains the tokens of the run.
6. Judge the answer against the expected component and cause, and fill in a copy of
   [agent_runs/TEMPLATE.md](agent_runs/TEMPLATE.md).

### Baseline (optional)

Same case and prompt, but the agent gets the unpacked logs and only shell and file tools, no
server (Claude Code: `claude --strict-mcp-config` in a folder with the logs). Count its calls and
bytes read from the transcript. Does the server beat it?

### Decide

| case | client | component | root cause | expected cites found | check_citations | calls | bytes | baseline calls / bytes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| | | | | | | | | |

- **Go** if at least 3 of 5 runs find the right component, `check_citations` passes on every
  answer, and the server beats the baseline on calls or bytes (if run).
- **Adjust** if agents spend many calls getting oriented, or recall is low.
- **Stop** if the logs alone rarely explain the defects.

## Synthetic cases

| Case | Files | Tests |
| --- | --- | --- |
| `syn-hvac-crash` | fixture bugreport | Java crash, VHAL error before it |
| `syn-radio-anr` | fixture bugreport | ANR lock holder in a binder call |
| `syn-vhal-abort` | `vhal-abort.log` | VHAL abort after a bus timeout |
| `syn-lmk-media` | `lmk-media.log` | low-memory kill of another app |
| `syn-boot-selinux` | `boot-selinux.log` | SELinux denial at boot |
| `syn-watchdog-audio` | `watchdog-audio.log` | watchdog kill; audio HAL error found by search |
| `syn-evs-crash` | `evs-crash/` | a tombstone file next to a logcat |
| `syn-wifi-fw-ota` | `wifi-fw-ota.dmesg` | two boots; the second fails to load Wi-Fi firmware |
| `syn-bt-restart` | `bt-restart.log` | a HAL restart loop; cause found by search |
