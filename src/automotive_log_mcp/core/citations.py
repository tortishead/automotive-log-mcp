"""Citations in a Markdown answer: find them, check their form, split the text into paragraphs.

Used by the ``check_citations`` tool (which resolves them against the case) and the evaluation.
A citation is ``log:<source>#<seq>`` or ``art:<id>[/<section>][:L<n>[-L<m>]]`` (D6). Anything
else that starts with ``log:`` or ``art:`` followed by a character that can start an id
(digit, '#', '/') is a malformed citation; prose like "state of the art: …" is not a citation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

LOG_RE = re.compile(r"log:(\d+)#(\d+)")
ART_RE = re.compile(r"art:(\d+)(?:/([a-z0-9_.-]+))?(?::L(\d+)(?:-L?(\d+))?)?")

# A candidate: 'log:' or 'art:' not inside a word or path, then anything up to a delimiter.
_CANDIDATE = re.compile(r"(?<![\w/.:-])(log|art):([^\s`'\"<>()\[\]{},;|*_~\u2013\u2014\u2192]*)")
_TRAILING = ".:!?…"
_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
_ITEM = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}")
_QUOTE = re.compile(r"`([^`\n]{10,300})`|\"([^\"\n]{10,300})\"|“([^”\n]{10,300})”")

# Words that mark a paragraph as an explicit guess, a gap in the evidence or a next step:
# such paragraphs need no citation (the triage prompt asks for them).
HEDGES = (
    "guess",
    "assum",
    "hypothes",
    "speculat",
    "probabl",
    "likely",
    "not shown",
    "not in the log",
    "no evidence",
    "do not show",
    "does not show",
    "don't show",
    "doesn't show",
    "unknown",
    "unclear",
    "not visible",
    "cannot tell",
    "can't tell",
    "without a citation",
    "no citation",
    "next step",
    "recommend",
    "to confirm",
    "would need",
)
MIN_WORDS = 8  # shorter paragraphs (labels, list intros) need no citation


@dataclass(frozen=True)
class Found:
    line: int  # 1-based line of the text
    text: str  # as written (trailing punctuation removed)
    kind: str  # 'log' | 'art'
    valid: bool  # well-formed
    problem: str | None = None


@dataclass
class Paragraph:
    first_line: int
    text: str
    cites: list[Found] = field(default_factory=list)
    kind: str = "text"  # text | item | table

    @property
    def words(self) -> int:
        return len(re.findall(r"[A-Za-z0-9]\S*", self.text))

    def hedged(self) -> bool:
        low = self.text.lower()
        return any(h in low for h in HEDGES)

    def quotes(self) -> list[str]:
        """Quoted log text: `code` or "double quoted", 10+ characters, not a tool call or citation."""
        out = []
        for m in _QUOTE.finditer(self.text):
            q = next(g for g in m.groups() if g)
            if "(" in q or _CANDIDATE.search(q) or q.strip().startswith(("http", "/")):
                continue
            out.append(q)
        return out


def classify(kind: str, body: str) -> tuple[str, bool, str | None]:
    """The citation text without trailing punctuation, whether it is well-formed, and why not."""
    while body and body[-1] in _TRAILING:
        body = body[:-1]
    text = f"{kind}:{body}"
    if kind == "log":
        lm = LOG_RE.fullmatch(text)
        if lm:
            return (text, True, None) if int(lm[2]) > 0 else (text, False, "seq starts at 1")
        if re.fullmatch(r"log:\d+#\d+\s*(?:-|\.\.)\s*\d+", text):
            return text, False, "ranges are not citations; cite each line, e.g. log:3#10 and log:3#12"
        if re.fullmatch(r"log:\d+#\d+[+-]\d.*", text):
            return text, False, "a time offset is a filter value, not a citation; cite the line itself"
        if re.fullmatch(r"log:\d+", text):
            return text, False, "no line: expected log:<source>#<seq>, e.g. log:3#120"
        return text, False, "expected log:<source>#<seq>, e.g. log:3#120"
    m = ART_RE.fullmatch(text)
    if m:
        if m[3] and (int(m[3]) == 0 or (m[4] and int(m[4]) < int(m[3]))):
            return text, False, "invalid line range"
        return text, True, None
    return text, False, "expected art:<id>[/<section>][:L<line>], e.g. art:6/backtrace:L17"


def find(text: str) -> list[Found]:
    """Every citation (well-formed or not) outside fenced code blocks, in order."""
    out: list[Found] = []
    for p in paragraphs(text):
        out.extend(p.cites)
    return out


def _scan_line(line: str, n: int) -> list[Found]:
    out = []
    for m in _CANDIDATE.finditer(line):
        body = m[2]
        if not body or not (body[0].isdigit() or body[0] in "#/" or "#" in body):
            continue  # prose such as "log: …" or "state of the art: …"
        text, valid, problem = classify(m[1], body)
        out.append(Found(n, text, m[1], valid, problem))
    return out


def paragraphs(text: str) -> list[Paragraph]:
    """Paragraphs of Markdown: blank-line separated text, each list item and table row on its own;
    headings and fenced code are left out (citations inside code fences are not counted)."""
    out: list[Paragraph] = []
    cur: Paragraph | None = None
    fence: str | None = None

    def close() -> None:
        nonlocal cur
        if cur is not None and cur.text.strip():
            out.append(cur)
        cur = None

    for n, raw in enumerate(text.splitlines(), 1):
        m = _FENCE.match(raw)
        if fence is not None:
            if m and m[1] == fence:
                fence = None
            continue
        if m:
            close()
            fence = m[1]
            continue
        line = raw.rstrip()
        stripped = line.lstrip("> ").strip() if line.lstrip().startswith(">") else line.strip()
        if not stripped:
            close()
            continue
        if _HEADING.match(line):
            close()
            continue
        cites = _scan_line(line, n)
        if stripped.startswith("|"):
            close()
            if not _TABLE_SEP.match(stripped):
                out.append(Paragraph(n, stripped, cites, "table"))
            continue
        if _ITEM.match(line):
            close()
            cur = Paragraph(n, _ITEM.sub("", line, count=1).strip(), cites, "item")
            continue
        if cur is None:
            cur = Paragraph(n, stripped, cites)
        else:
            cur.text += " " + stripped
            cur.cites.extend(cites)
    close()
    return out


def normalize(text: str) -> str:
    """For comparing quoted text with log text: case-folded, whitespace collapsed, no ellipsis."""
    text = text.replace("⏎", " ").rstrip("…").removesuffix("...")
    return re.sub(r"\s+", " ", text).strip().casefold()
