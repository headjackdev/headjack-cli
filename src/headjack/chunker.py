"""Heading-aware markdown chunking, plus symptom-literal extraction.

Split on markdown headings, never on a fixed token window. A runbook section is a
semantic unit: "§B — Hints are piling up" carries its ordering constraint (alter
before removenode) inside it. A chunk that severs a procedure from its precondition
is worse than no chunk, because it retrieves just as confidently as a correct one.

`extract_symptoms` pulls exact literals — error codes, identifiers, shas — into their
own field so BM25 can weight them separately (see `search.py`). Whether this is worth
the complexity is an open question the Phase 1 ablation (`P1-16`) answers; `extract`
is a plain bool specifically so that experiment can turn it off without a second code
path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# #{2,4}: h1 is not a split point — a document's own title isn't a section boundary.
_HEADING_RE = re.compile(r"^(#{2,4})\s+(.*)")
_FENCE_RE = re.compile(r"^(```|~~~)")

# Compiled once at import time, not per call — extract_symptoms runs on every chunk
# of every indexed artifact.
#
# Known over-matcher: the 4th pattern (keyspace.table) also fires on filenames
# ("filename.md"), Python attribute access ("self.foo"), and abbreviations ("e.g.").
# Decision: accept these as noise rather than add filtering. The 60-item cap and
# case-insensitive dedup bound the damage, and distinguishing "real.keyspace" from
# "self.foo" without false negatives needs more than a regex can give cheaply. If
# `P1-16`'s ablation shows this pattern hurts more than it helps, remove it — don't
# patch around it.
_SYMPTOM_PATTERNS = [
    re.compile(r"`([^`]{3,80})`"),  # inline code
    re.compile(r'"([A-Z][^"]{5,80})"'),  # quoted log lines
    re.compile(r"\b([A-Z][a-zA-Z]*(?:Exception|Error|Failed|Exceeded))\b"),
    re.compile(r"\b([a-z][a-z0-9_]*\.[a-z][a-z0-9_]*)\b"),  # keyspace.table (+ noise, see above)
    re.compile(r"\b([0-9a-f]{7,40})\b"),  # git shas
    re.compile(r"\b([a-z]+-[a-z0-9]+-\d+)\b"),  # gcp-eu4-02, itops-847384
]
_SYMPTOM_CAP = 60


@dataclass(frozen=True)
class Chunk:
    heading: str | None
    text: str
    symptoms: str = ""


def chunk_markdown(
    body: str, min_chars: int = 200, max_chars: int = 2400, extract: bool = True
) -> list[Chunk]:
    """Split `body` on `##`/`###`/`####` headings. Merge runts into the chunk before
    them, hard-split giants on blank-line paragraph boundaries. A heading-looking line
    inside a fenced code block (``` or ~~~) is not treated as a heading — runbooks are
    full of fenced YAML containing `# comments`.

    `extract=False` skips symptom-literal extraction entirely — every chunk's
    `symptoms` is `""`. For the Phase 1 ablation; leave it `True` otherwise.
    """
    if not body.strip():
        return []

    sections = _split_on_headings(body)
    merged = _merge_and_split(sections, min_chars, max_chars)
    return [
        Chunk(heading=h, text=t, symptoms=extract_symptoms(t) if extract else "") for h, t in merged
    ]


def extract_symptoms(text: str) -> str:
    """Pull exact literals into their own string so BM25 can weight them separately.

    Matches across all six patterns are pooled, deduped case-insensitively keeping
    first-seen casing and order, and capped at 60 — a chunk with hundreds of inline
    codes would otherwise dominate the `symptoms` FTS column with noise.
    """
    hits: list[str] = []
    for pattern in _SYMPTOM_PATTERNS:
        hits.extend(pattern.findall(text))

    seen: set[str] = set()
    out: list[str] = []
    for hit in hits:
        key = hit.lower()
        if key not in seen:
            seen.add(key)
            out.append(hit)

    return " ".join(out[:_SYMPTOM_CAP])


def _split_on_headings(body: str) -> list[tuple[str | None, str]]:
    parts: list[tuple[str | None, str]] = []
    cur: list[str] = []
    head: str | None = None
    in_fence = False
    fence_marker: str | None = None

    for line in body.splitlines():
        fence = _FENCE_RE.match(line.lstrip())
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence, fence_marker = True, marker
            elif marker == fence_marker:
                in_fence, fence_marker = False, None
            cur.append(line)
            continue

        heading = None if in_fence else _HEADING_RE.match(line)
        if heading:
            if cur:
                parts.append((head, "\n".join(cur)))
            head, cur = heading.group(2).strip(), [line]
        else:
            cur.append(line)

    if cur:
        parts.append((head, "\n".join(cur)))
    return parts


def _merge_and_split(
    sections: list[tuple[str | None, str]], min_chars: int, max_chars: int
) -> list[tuple[str | None, str]]:
    out: list[tuple[str | None, str]] = []
    for heading, text in sections:
        if len(text) < min_chars and out:
            prev_heading, prev_text = out[-1]
            out[-1] = (prev_heading, prev_text + "\n" + text)
        elif len(text) > max_chars:
            out.extend((heading, part) for part in _split_giant(text, max_chars, min_chars))
        else:
            out.append((heading, text))
    return out


def _split_giant(text: str, max_chars: int, min_chars: int) -> list[str]:
    """Hard-split on blank-line paragraph boundaries. A single paragraph longer than
    `max_chars` is emitted whole — unsplittable, not silently dropped or truncated.

    The section's own heading line is always the first "paragraph" here (it's part
    of `text`, see `_split_on_headings`), and it is always short. Without the
    `len(buf) < min_chars` escape hatch below, it would flush as its own
    near-empty chunk the moment the paragraph after it is large — never merging
    with real content at all.
    """
    parts: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        if not buf:
            buf = para
        elif len(buf) < min_chars or len(buf) + 2 + len(para) <= max_chars:
            buf += "\n\n" + para
        else:
            parts.append(buf)
            buf = para
    if buf.strip():
        parts.append(buf)
    return parts
