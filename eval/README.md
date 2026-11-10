# Evaluation

Two levels, on the same cases (implementation plan, M4 item 6 and the checkpoint):

1. **Evidence recall** (no agent): do `detect`, `list_artifacts` and a case's recorded queries
   show the known root-cause lines on their first page? `evidence_recall.py`, runs in CI on the
   synthetic cases.
2. **Agent runs** (by hand): an agent in a real MCP client (VS Code Copilot agent mode, Claude
   Code, …) triages each case with the `defect_triage` prompt; you record whether it found the
   right component, whether `check_citations` passes on its answer, and its calls and bytes from
   the call log. Checklist and template in [agent_runs/](agent_runs/).

| File | What |
| --- | --- |
| `cases.yaml` | the 5 synthetic cases (from the test fixtures and `synthetic/`), with expected answers |
| `cases.local.yaml` | your real closed defects (git-ignored; create it from the example below) |
| `evidence_recall.py` | evidence recall over one or more case files; Markdown report, optional JSON |
| `callstats.py` | calls, bytes and tools per session from the server's call log (for where the tokens went and why a run was expensive, use `automotive-log-mcp calls`, see the main README "Token use") |
| `agent_runs/` | the agent-run checklist and template; your run notes go in `agent_runs/local/` (git-ignored) |
| `results/` | reports you write with `--report` (git-ignored) |

**Status:** the harness and both procedures are built and checked on the 5 synthetic cases only.
The M4 done criterion ("both evaluations run on 5 closed defects and written up") needs your
real defects. The synthetic cases test the harness, not the server's value: their expected
citations come from how the logs were written.

## Add a real case

A case is a **closed** defect whose root cause is known from its fix commit. Write the expected
answer **before** you run anything on it (neither the recall script nor an agent), and judge it
against the fix, not against what the tools show.

1. Copy the defect's logs somewhere stable (the case DB references them by path and hash; it
   never copies them). Keep them out of this repository.
2. Create `eval/cases.local.yaml` (git-ignored) with one entry per defect:

   ```yaml
   version: 1
   cases:
     - id: T-4711                       # the ticket id; letters, digits, '.', '_', '-'
       title: HVAC app crash on temperature change
       symptom: The climate app closes when the temperature is set (as the ticket says it)
       files:                           # indexed in this order into a fresh case named after id
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

3. Index the files once, into the case named after the id, **in the order listed** (source ids
   in citations depend on the order):

   ```bat
   :: Windows
   py -m automotive_log_mcp index T-4711 C:\defects\T-4711\bugreport-2026-10-11.zip
   py -m automotive_log_mcp sources T-4711
   ```

   ```bash
   # Linux
   python -m automotive_log_mcp index T-4711 ~/defects/T-4711/bugreport-2026-10-11.zip
   python -m automotive_log_mcp sources T-4711
   ```

4. Find the root-cause lines **from the fix commit** (the code it changed, the message it fixed)
   with the CLI, not with the tools under test first: `query`, `artifacts --show`, e.g.
   `py -m automotive_log_mcp query T-4711 --search "setProperty failed" --level E`. Write their
   citations and a short text each line must contain into `expected.citations`. The text check
   catches citations that drift (a reindex with another connector version, a wrong line).
   Writing citations after looking at the CLI output is fine; what must not happen is choosing
   them after seeing what `detect` or an agent found.

Five such cases are the checkpoint's minimum ("at least 3 of 5 agent runs point to the right
component").

## Run evidence recall

```bat
:: Windows (from the repository root; your cases stay indexed in your normal home)
py eval\evidence_recall.py eval\cases.yaml eval\cases.local.yaml --home %USERPROFILE%\.automotive-log-mcp --report eval\results\recall.md --json eval\results\recall.json
```

```bash
# Linux
python eval/evidence_recall.py eval/cases.yaml eval/cases.local.yaml --home ~/.automotive-log-mcp \
  --report eval/results/recall.md --json eval/results/recall.json
```

Without `--home` the cases are indexed into a temporary home that is deleted afterwards (the
synthetic cases in CI run that way). With `--home`, a case that already exists is reused (files
already in it are not indexed again), so the agent runs below see the same source ids.

The report lists per case: recall, which expected citations were printed exactly, which only by
their artifact (`art:<id>`, the next `read_artifact` shows the line), which were missed, which
tool printed them, and the bytes of each call. Expected citations that do not resolve, or whose
line does not contain the given text, are listed and make the script exit with 1.
`--min-recall 1` also fails on any miss. The test suite (`tests/test_eval.py`) runs the synthetic
cases and checks their results against the recorded baseline below.

## Run the agent evaluation

See [agent_runs/README.md](agent_runs/README.md): the checklist, the client setup for VS Code
Copilot agent mode (Windows, `py -m automotive_log_mcp serve`) and Claude Code, and how to take
calls and bytes from the call log with `callstats.py`; `automotive-log-mcp calls --session <id>`
(Windows: `py -m automotive_log_mcp calls ...`) explains the tokens of one run and suggests changes.

## Write up

For the checkpoint, put together (in `eval/results/` or the ticket system, not in this repo if
the logs are confidential):

- the evidence-recall report (`recall.md`),
- one filled-in run template per agent run (`agent_runs/local/<case>-<client>.md`),
- the summary table from `agent_runs/README.md` (right component in how many of 5, check_citations
  passing on how many answers, median calls and bytes, baseline numbers if you ran it),
- the go / adjust / stop decision with the reason (plan, "Checkpoint").
