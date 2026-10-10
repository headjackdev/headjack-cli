"""Tests for headjack.chunker."""

from __future__ import annotations

from headjack.chunker import Chunk, chunk_markdown

CASSANDRA_RUNBOOK = """\
Intro paragraph before any heading, explaining what this runbook covers in general.
It needs to be long enough on its own that it will not be merged into whatever
section happens to follow it, so this sentence pads it out to that length.

## §A — Node won't rejoin

Short section.

## §B — Hints are piling up

Run `nodetool disablehints` before the alter, or the replacement node will never
catch up. Then run `nodetool removenode <id>` to retire the old one. This has to
happen in that order, every time, because alter-before-removenode is what stops
a flood of hinted handoff traffic from the rest of the cluster once the dead node's
token range is reassigned, and getting the order backwards has caused outages.

## §C — SSTable explosion

```
# this looks like a heading but it's inside a fence
## not a real heading
```

Real content after the fence, discussing compaction strategy in enough detail that
this section clears the minimum chunk size on its own without needing a merge.
"""


def test_empty_body_returns_no_chunks() -> None:
    assert chunk_markdown("") == []
    assert chunk_markdown("   \n\n  ") == []


def test_content_before_first_heading_is_its_own_chunk() -> None:
    body = "Lead-in paragraph.\n\n## First heading\n\nBody text here that is long enough."
    chunks = chunk_markdown(body, min_chars=5)
    assert chunks[0].heading is None
    assert "Lead-in paragraph" in chunks[0].text


def test_runt_merges_into_previous_chunk_keeping_its_heading() -> None:
    body = "## Keep me\n\n" + ("x" * 300) + "\n\n## Tiny\n\nshort"
    chunks = chunk_markdown(body, min_chars=200, max_chars=2400)
    assert len(chunks) == 1
    assert chunks[0].heading == "Keep me"
    assert "short" in chunks[0].text


def test_runt_as_first_chunk_is_kept_as_its_own_chunk() -> None:
    body = "## Only section\n\nshort"
    chunks = chunk_markdown(body, min_chars=200)
    assert len(chunks) == 1
    assert chunks[0].heading == "Only section"


def test_heading_like_line_inside_fence_is_not_a_split_point() -> None:
    chunks = chunk_markdown(CASSANDRA_RUNBOOK, min_chars=50, max_chars=2400)
    headings = [c.heading for c in chunks]
    assert "not a real heading" not in headings
    assert "§C — SSTable explosion" in headings
    c_chunk = next(c for c in chunks if c.heading == "§C — SSTable explosion")
    assert "## not a real heading" in c_chunk.text  # preserved verbatim, just not split on


def test_h1_is_not_a_split_point() -> None:
    body = "# Title\n\nIntro.\n\n## Real section\n\n" + ("y" * 250)
    chunks = chunk_markdown(body, min_chars=10)
    assert chunks[0].heading is None
    assert "# Title" in chunks[0].text


def test_giant_section_splits_on_blank_lines_keeping_heading() -> None:
    paragraphs = [
        f"Paragraph number {i} with some padding text to take up space." for i in range(40)
    ]
    body = "## Giant section\n\n" + "\n\n".join(paragraphs)
    chunks = chunk_markdown(body, min_chars=50, max_chars=500)
    assert len(chunks) > 1
    assert all(c.heading == "Giant section" for c in chunks)
    assert all(len(c.text) <= 500 or "\n\n" not in c.text for c in chunks)


def test_single_unsplittable_paragraph_exceeding_max_is_emitted_whole() -> None:
    # Realistic min_chars (200, the contract default): the heading line itself is
    # ~22 chars, well under that, so it must merge forward into the giant paragraph
    # rather than standing alone as its own near-empty chunk.
    huge_single_paragraph = "word " * 1000  # one paragraph, no blank lines, > max_chars
    body = "## One huge paragraph\n\n" + huge_single_paragraph
    chunks = chunk_markdown(body, min_chars=200, max_chars=500)
    assert len(chunks) == 1
    assert len(chunks[0].text) > 500


def test_no_chunk_below_min_except_unmergeable_edges() -> None:
    chunks = chunk_markdown(CASSANDRA_RUNBOOK, min_chars=50, max_chars=2400)
    for c in chunks:
        assert len(c.text) >= 50 or c is chunks[0] or c is chunks[-1]


def test_symptoms_field_empty_without_extraction() -> None:
    # Superseded by P1-04's real extract_symptoms (see test_symptoms.py for the
    # extraction behaviour itself) — this now just confirms the extract=False
    # bypass chunk_markdown exposes for the Phase 1 ablation.
    chunks = chunk_markdown(CASSANDRA_RUNBOOK, extract=False)
    assert all(c.symptoms == "" for c in chunks)


def test_deterministic() -> None:
    assert chunk_markdown(CASSANDRA_RUNBOOK) == chunk_markdown(CASSANDRA_RUNBOOK)


def test_round_trip_concatenation_preserves_content_modulo_merge_newlines() -> None:
    body = (
        "## A\n\nfirst section text that is long enough to stand alone here.\n\n"
        "## B\n\nsecond section text that is also long enough to stand alone."
    )
    chunks = chunk_markdown(body, min_chars=10)
    rejoined = "\n".join(c.text for c in chunks)
    for fragment in ("first section text", "second section text", "## A", "## B"):
        assert fragment in rejoined


def test_chunk_is_frozen() -> None:
    c = Chunk(heading="h", text="t")
    try:
        c.text = "changed"  # type: ignore[misc]
        raised = False
    except Exception:
        raised = True
    assert raised
