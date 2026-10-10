"""Extract a bounded, query-centred snippet from a chunk's text.

This is the context cost of Tier 1 ([spec §4](../../docs/spec/README.md)) — what
actually lands in the agent's window per result — so the 300-character cap is a
budget, not a suggestion.
"""

from __future__ import annotations

import re

_WHITESPACE_RE = re.compile(r"\s+")
_WORD_TOKEN_RE = re.compile(r"\w+")
_SPAN_RE = re.compile(r"\S+")
_STRIP_PUNCT = ".,;:!?\"'()[]{}"


def snippet(text: str, query: str, *, max_chars: int = 300) -> str:
    """A window around the densest concentration of distinct query terms.

    - Collapses whitespace runs first, so span offsets are stable.
    - Finds the window (of up to `max_chars`, minus room for ellipses) covering the
      most *distinct* query terms; ties prefer the earliest window in the text.
    - Expands to word boundaries — never cuts a word in half.
    - Prefixes/suffixes with "…" when the window doesn't start/end at the text's edge.
    - No query terms present (or none found at all) → the first `max_chars`.
    - Hard guarantee: the result never exceeds `max_chars`, ellipses included.
    - Never leaves a lone, unterminated ``` fence in the returned text — a snippet
      with a dangling fence marker breaks markdown rendering downstream.
    """
    normalized = _WHITESPACE_RE.sub(" ", text).strip()
    if not normalized:
        return ""

    query_terms = {t.lower() for t in _WORD_TOKEN_RE.findall(query)}
    spans = list(_SPAN_RE.finditer(normalized))
    if not query_terms or not spans:
        return _close_fences(_hard_truncate(normalized, max_chars))

    start_i, end_i, best_hits = _best_window(normalized, spans, query_terms, max_chars)
    if best_hits <= 0:
        return _close_fences(_hard_truncate(normalized, max_chars))

    window_start = spans[start_i].start()
    window_end = spans[end_i].end()
    prefix = "…" if window_start > 0 else ""
    suffix = "…" if window_end < len(normalized) else ""

    budget = max_chars - len(prefix) - len(suffix)
    window = normalized[window_start:window_end][: max(budget, 0)]
    result = f"{prefix}{window}{suffix}"
    return _close_fences(_hard_truncate(result, max_chars))


def _best_window(
    text: str, spans: list[re.Match[str]], query_terms: set[str], max_chars: int
) -> tuple[int, int, int]:
    """Two-pointer scan over word spans: widen `j` while the window fits in
    `max_chars` (reserving a little room for ellipses), track distinct term hits,
    shrink `i` to advance. O(n) over the chunk's words, not its characters.
    """
    reserved = 2  # worst case: both a leading and trailing ellipsis
    budget = max(max_chars - reserved, 1)

    best = (0, -1, 0)  # start_i, end_i, hit_count — end_i=-1 means "no window yet"
    i = 0
    hits: dict[str, int] = {}

    for j, span in enumerate(spans):
        word = span.group().strip(_STRIP_PUNCT).lower()
        if word in query_terms:
            hits[word] = hits.get(word, 0) + 1

        while spans[j].end() - spans[i].start() > budget and i < j:
            left_word = spans[i].group().strip(_STRIP_PUNCT).lower()
            if left_word in hits:
                hits[left_word] -= 1
                if hits[left_word] == 0:
                    del hits[left_word]
            i += 1

        count = len(hits)
        if count > best[2]:
            best = (i, j, count)

    return best


def _hard_truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def _close_fences(text: str) -> str:
    """An odd number of ``` occurrences means the snippet cut a fence in half —
    neutralise them rather than ship a dangling fence marker downstream."""
    if text.count("```") % 2 == 0:
        return text
    return text.replace("```", "'''")
