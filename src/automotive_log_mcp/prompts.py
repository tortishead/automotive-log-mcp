"""Texts the server sends: its instructions (with every model request) and the triage prompt."""

INSTRUCTIONS = """\
Tools for analysing a defect from automotive logs (Android bugreport, logcat, kernel logs, other \
formats from connectors). The \
server parses, indexes and searches; reasoning is yours. Each call re-sends the whole \
conversation, so make few, narrow calls and stop once the evidence answers the question.

Simple questions (what crashed, and why): 3-4 calls.
1. add_source(case, path) if the case is new; its reply lists the sources (no list_sources needed).
2. list_artifacts(case): tombstones, ANRs, Java crashes, dropbox entries, grouped by signature in \
time order; 'same crash as' marks copies, which need no second read.
3. read_artifact(case, id='art:<id>') for the crash that fits: its summary has the signal or \
exception, cause and top of the stack; read a section only if the summary is not enough.
4. Answer, citing each finding; check_citations(case, text) before answering when asked to cite.

Broader questions: detect(case) for known failure patterns (crash, ANR, watchdog, low memory \
kill, SELinux, binder, VHAL, service restart, boot not completed); overview(case) for where \
errors cluster; context(case, at='art:<id>' or 'log:<source>#<seq>') for the lines before an \
event (scope='pid' follows one process across buffers); search/count with filter level 'W' or \
'E' and from/to; get_records for messages cut with '…'.

Several files or devices (bugreport plus kernel log, ECU logs): timeline(case, filter) merges all \
sources on one case clock and context(case, at=..., scope='all') shows every source around an \
event; '~' marks times aligned from another clock, align(case) shows how well (and sets anchors). \
Say when an order across sources rests on '~' times within their error. A good and a bad run, \
or two boots: diff(case, a={'sources': [...]}, b=...) lists what only one side has.

Cite findings exactly as printed: log:<source>#<seq> or art:<id>[/section][:L<n>]; a statement \
without a citation is a guess. Outputs are capped (about 50 lines / 8 KB): narrow with level, \
from/to, tags or pid instead of paging. Printed timestamps work as from/to, as do '-5m', '+30s' \
and 'log:3#120-30s'. Redacted values are stable tokens (<MAC:3fa2c1d0>); ≈ marks estimated counts.
"""

TRIAGE_PROMPT = """\
Triage the defect in case '{case}' with the automotive-log-mcp tools.

{symptom}Work in this order and stop as soon as the evidence is clear:
1. list_sources(case='{case}'). Note the build fingerprint and each source's time range. If the case \
has no sources, ask for the log files and add them with add_source.
2. overview(case='{case}') and detect(case='{case}'): where the errors are, and which known failure \
patterns (crash, ANR, watchdog, low memory kill, VHAL error, service restart, …) occur and when.
3. list_artifacts(case='{case}'). For each group whose time and process fit the symptom, \
read_artifact the first artifact, then the section it suggests: the backtrace of a tombstone, the \
exception of a Java crash, the main thread (and lock holder or binder peer) of an ANR.
4. context(case='{case}', at='art:<id>', before=30, after=10) for what happened just before the \
crash; scope='pid' to follow the crashing process across buffers.
5. search(case='{case}', query=..., filter={{'level': 'W', 'from': ..., 'to': ...}}) for errors \
and warnings in the minutes before the defect: start from the component or process in the \
symptom, then widen. Keep collapse on; repeated messages show ×count. count(..., bucket='1m') shows \
when a message starts or stops.
6. get_records for any message cut with '…' that matters.
7. Before you answer: check_citations(case='{case}', text=<your answer in Markdown>). Fix every \
error it reports (a citation that is malformed or does not resolve) and either cite or mark as a \
guess every paragraph it warns about; run it again until it passes. Only then give the answer.

Answer with:
- What failed (component, process) and when (timestamp as printed).
- The chain of evidence, oldest first, each step with its citation (log:<source>#<seq> or \
art:<id>/<section>:L<n>) exactly as the tools printed it.
- The constant text from 'code search' for the lines a developer should look up in the code.
- What the logs do not show, and what you are guessing (without a citation it is a guess).
"""
