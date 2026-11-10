# Agent run: <case id> / <client> / run <n>

- Date and start time (UTC): 
- Client and model: (e.g. VS Code 1.10x, GitHub Copilot agent mode, <model>; or Claude Code <version>, <model>)
- Server version / commit: (`py -m automotive_log_mcp --version`, `git rev-parse --short HEAD`)
- Tools enabled: automotive-log only / baseline (shell only)
- Expected answer written on (case file commit or date): 

## Prompt

defect_triage(case='<id>', symptom='<as in the case file>')

Interventions (questions answered, nudges): none

## Answer

(paste the final answer, or link `<case>-<client>-<n>.answer.md`)

## Judgement

| item | result |
| --- | --- |
| Component (expected: …) | right / partly / wrong / none |
| Root-cause direction | right / partly / wrong |
| Expected citations cited | x of y: … |
| check_citations (`automotive-log-mcp check`) | PASS / FAIL, n errors, m warnings |
| Agent ran check_citations before answering | yes (n times) / no |
| Calls / bytes / errors (`callstats.py`) | |
| Wall time | |

## Notes

- Where it spent calls before it was oriented:
- Tool outputs that misled it or were too large:
- What the logs do not show (did it say so?):
