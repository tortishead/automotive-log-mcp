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
| `cases.yaml` | the 10 synthetic cases (from the test fixtures and `synthetic/`), with expected answers |
| `cases.local.yaml` | your real closed defects (git-ignored; create it from the example below) |
| `evidence_recall.py` | evidence recall over one or more case files; Markdown report, optional JSON |
| `callstats.py` | calls, bytes and tools per session from the server's call log (for where the tokens went and why a run was expensive, use `automotive-log-mcp calls`, see the main README "Token use") |
| `agent_runs/` | the agent-run checklist and template; your run notes go in `agent_runs/local/` (git-ignored) |
| `results/` | reports you write with `--report` (git-ignored) |

**Status:** the harness and both procedures are built and checked on the 10 synthetic cases
only. The M4 and M6 done criteria ("both evaluations run on 5 / 10 closed defects and written up")
need your real defects; no agent run has been done on any case yet. The synthetic cases test the
harness, not the server's value: their expected citations come from how the logs were written.

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
component"); the release plan asks for ten.

For a defect whose evidence is spread over files on different clocks (a kernel log in seconds
since boot, a second device), also write `expected.order`: the citations in the order they
happened. The harness checks that the case clock (what `timeline` shows) puts them in that order
and that every one is placed; a wrong order fails the run like an unresolved citation.

Recorded `queries` take the tools `search`, `count`, `context`, `list_artifacts`,
`read_artifact`, `get_records`, `detect`, `overview`, `timeline` and `diff`, with the arguments the
MCP tools take, e.g.:

```yaml
queries:
  - {tool: timeline, args: {filter: {from: "log:1#6-1s", to: "log:1#6"}}}
  - {tool: diff, args: {a: {sources: ["1"], seg: 1}, b: {sources: ["1"], seg: 2}}}
```

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

## The synthetic cases

| Case | Files | What it exercises |
| --- | --- | --- |
| `syn-hvac-crash` | fixture bugreport | Java crash artifact, VHAL error before it |
| `syn-radio-anr` | fixture bugreport | ANR lock holder in a binder call |
| `syn-vhal-abort` | `vhal-abort.log` | VHAL abort after a bus timeout |
| `syn-lmk-media` | `lmk-media.log` | low memory kill of another app |
| `syn-boot-selinux` | `boot-selinux.log` | SELinux denial at boot |
| `syn-can-busoff` | `can-busoff/hu-logcat.txt`, `can-busoff/dmesg.txt` | two clocks: the dmesg is placed by 12 kernel lines it shares with the logcat (`paired`); the cause (CAN bus-off) shows only on the timeline, 248 ms before the VHAL error; `expected.order` |
| `syn-watchdog-audio` | `watchdog-audio.log` | watchdog kill, audio HAL error found by a search |
| `syn-evs-crash` | `evs-crash/evs.log`, `evs-crash/tombstone_03` | a standalone tombstone file next to a logcat |
| `syn-wifi-fw-ota` | `wifi-fw-ota.dmesg` | two boots of one kernel log; only `diff` (boot 1 against boot 2) shows the missing firmware |
| `syn-bt-restart` | `bt-restart.log` | a HAL restart loop, the cause found by a search |

First run of all ten (Nov 10, 2026; expected answers written before it): **19 of 20** root-cause
citations surfaced (15 exact, 4 through their artifact), every expected citation resolves to a
line with its text, and the order check of `syn-can-busoff` holds. The miss is `syn-boot-selinux`
`log:1#4` (since M4: no rule covers the daemon's own "Permission denied" line). Whether an agent
finds the same evidence, and in how many calls, is not measured: no agent run was done.
