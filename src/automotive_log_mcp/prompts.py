"""The server instructions, sent with every model request."""

INSTRUCTIONS = """\
Tools for reading automotive logs and traces (Android bugreport, logcat, kernel logs, other \
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

Broader questions: overview(case) for where errors cluster; context(case, at='art:<id>' or \
'log:<source>#<seq>') for the lines before an event (scope='pid' follows one process across \
buffers); search/count with filter level 'W' or 'E' and from/to; get_records for messages cut with '…'.

Cite findings exactly as printed: log:<source>#<seq> or art:<id>[/section][:L<n>]; a statement \
without a citation is a guess. Outputs are capped (about 50 lines / 8 KB): narrow with level, \
from/to, tags or pid instead of paging. Printed timestamps work as from/to, as do '-5m', '+30s' \
and 'log:3#120-30s'. Redacted values are stable tokens (<MAC:3fa2c1d0>); ≈ marks estimated counts.
"""
