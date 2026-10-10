"""Tests for headjack.snippet."""

from __future__ import annotations

from pathlib import Path

import pytest

from headjack.chunker import chunk_markdown
from headjack.snippet import snippet

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "corpus"

LONG_RUNBOOK_TEXT = (
    "This section explains general background context that has nothing to do with "
    "the actual symptom, padding the chunk out to a realistic length so the window "
    "selection logic has somewhere real to search. "
    "Run nodetool disablehints before the alter, or the replacement node will never "
    "catch up with the rest of the cluster once its token range is reassigned. "
    "More unrelated padding text follows here, discussing compaction strategy and "
    "other topics that are not what the query is actually asking about at all, on "
    "and on for a while to push the match near the end of the chunk rather than "
    "conveniently sitting at the start where a naive implementation would get it "
    "right by accident instead of by actually finding the best window."
)


def test_never_exceeds_max_chars() -> None:
    result = snippet(LONG_RUNBOOK_TEXT, "disablehints", max_chars=300)
    assert len(result) <= 300


def test_centres_on_query_terms_near_the_end_of_a_long_chunk() -> None:
    result = snippet(LONG_RUNBOOK_TEXT, "disablehints alter", max_chars=120)
    assert "disablehints" in result.lower()


def test_no_query_terms_returns_first_max_chars() -> None:
    # every token here is nonsense that genuinely does not appear in the fixture text
    result = snippet(LONG_RUNBOOK_TEXT, "zzyzx qqwkkz wibbleflorp", max_chars=50)
    assert result.startswith(LONG_RUNBOOK_TEXT[:40])
    assert len(result) <= 50


def test_empty_text_returns_empty_string() -> None:
    assert snippet("", "query", max_chars=300) == ""


def test_empty_query_returns_first_max_chars() -> None:
    result = snippet(LONG_RUNBOOK_TEXT, "", max_chars=50)
    assert len(result) <= 50


def test_whitespace_is_collapsed() -> None:
    text = "word1   word2\n\n\nword3\tword4"
    result = snippet(text, "word3", max_chars=300)
    assert "   " not in result
    assert "\n" not in result


def test_prefix_ellipsis_when_window_does_not_start_at_edge() -> None:
    result = snippet(LONG_RUNBOOK_TEXT, "disablehints", max_chars=60)
    assert result.startswith("…")


def test_no_ellipsis_when_match_is_at_the_very_start() -> None:
    text = "disablehints must run first. " + "padding " * 100
    result = snippet(text, "disablehints", max_chars=60)
    assert not result.startswith("…")


def test_never_splits_a_word_in_half() -> None:
    result = snippet(LONG_RUNBOOK_TEXT, "disablehints", max_chars=80)
    stripped = result.strip("…").strip()
    for edge_word in (stripped.split()[0], stripped.split()[-1]):
        assert edge_word in LONG_RUNBOOK_TEXT or edge_word.rstrip(".,") in LONG_RUNBOOK_TEXT


def test_does_not_leave_a_dangling_code_fence() -> None:
    text = "before the fence " + "x" * 50 + " ```python\ncode here\n``` after the fence"
    result = snippet(text, "nonexistent query term", max_chars=70)
    assert result.count("```") % 2 == 0


def test_window_maximizes_distinct_term_hits_over_a_single_repeated_hit() -> None:
    # a cluster of two distinct query terms should win over a lone instance of one
    text = (
        "alpha appears alone way out here with nothing else nearby at all to help it. "
        + "padding " * 20
        + "beta and gamma appear right next to each other over here instead."
    )
    result = snippet(text, "beta gamma", max_chars=60)
    assert "beta" in result and "gamma" in result


# ---- property test over the real fixture corpus ---------------------------------------


def _all_fixture_chunks() -> list[str]:
    chunks = []
    for path in FIXTURE_ROOT.rglob("*.md"):
        chunks.extend(c.text for c in chunk_markdown(path.read_text(), min_chars=1))
    return chunks


QUERIES_TO_TRY = ["", "nonexistent zzyzx term", "node", "cassandra", "a b c d e f g"]


def test_snippet_never_exceeds_300_chars_across_the_fixture_corpus() -> None:
    chunk_texts = _all_fixture_chunks()
    assert chunk_texts, "fixture corpus produced no chunks — test fixture may have moved"
    for text in chunk_texts:
        for query in QUERIES_TO_TRY:
            result = snippet(text, query, max_chars=300)
            assert len(result) <= 300


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
