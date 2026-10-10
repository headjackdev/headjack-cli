"""Tests for headjack.fuse (rrf + collapse)."""

from __future__ import annotations

import itertools
import sqlite3
from pathlib import Path

import pytest

from headjack.fuse import RRF_K, collapse, rrf
from headjack.store import connect

_name_seq = itertools.count()


def test_rrf_matches_hand_computed_worked_example() -> None:
    sparse = [10, 20, 30]
    dense = [20, 40, 10]
    result = dict(rrf(sparse, dense))

    k = RRF_K
    expected_10 = 1 / (k + 1) + 1 / (k + 3)  # rank 0 in sparse, rank 2 in dense
    expected_20 = 1 / (k + 2) + 1 / (k + 1)  # rank 1 in sparse, rank 0 in dense
    expected_30 = 1 / (k + 3)  # rank 2 in sparse only
    expected_40 = 1 / (k + 2)  # rank 1 in dense only

    assert result[10] == pytest.approx(expected_10)
    assert result[20] == pytest.approx(expected_20)
    assert result[30] == pytest.approx(expected_30)
    assert result[40] == pytest.approx(expected_40)


def test_id_in_both_lists_outranks_id_at_same_position_in_one() -> None:
    fused = dict(rrf([1, 2], [2, 3]))
    # id 2 appears at rank 1 in both lists; id 1 only at rank 0 in one.
    # id 2's combined score must beat id 3's (rank 1 in one list only).
    assert fused[2] > fused[3]


def test_ties_break_by_id_ascending() -> None:
    # id 100 at rank 0 in one list, id 50 at rank 0 in a different list: equal
    # scores, so the tie-break (ascending id) decides the order.
    fused = rrf([100], [50], k=10)
    assert fused[0][0] == 50
    assert fused[0][1] == pytest.approx(fused[1][1])


def test_empty_lists_produce_empty_result() -> None:
    assert rrf([], []) == []


def test_deterministic() -> None:
    sparse = [3, 1, 2]
    dense = [2, 3, 1]
    assert rrf(sparse, dense) == rrf(sparse, dense)


# ---- collapse --------------------------------------------------------------------------


def _seed_artifact_with_chunks(
    conn: sqlite3.Connection, name: str, chunks: list[str]
) -> tuple[int, list[int]]:
    artifact_id = conn.execute(
        "INSERT INTO artifact (kind, name, content_hash, scope, body, updated_at) "
        "VALUES ('runbook', ?, ?, 'personal', 'b', '2026-01-01T00:00:00Z')",
        (name, f"hash-{next(_name_seq)}"),
    ).lastrowid
    chunk_ids = []
    for i, text in enumerate(chunks):
        chunk_id = conn.execute(
            "INSERT INTO chunk (artifact_id, heading, text, symptoms) VALUES (?, ?, ?, '')",
            (artifact_id, f"section {i}", text),
        ).lastrowid
        conn.execute(
            "INSERT INTO chunk_vec (chunk_id, embedding) VALUES (?, ?)",
            (chunk_id, (b"\x00\x00\x80?") * 384),
        )
        assert chunk_id is not None
        chunk_ids.append(chunk_id)
    conn.commit()
    return artifact_id, chunk_ids


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    return connect(tmp_path / "fuse.db")


def test_collapse_keeps_highest_scoring_chunk_per_artifact(db: sqlite3.Connection) -> None:
    _, chunk_ids = _seed_artifact_with_chunks(
        db, "runbook-a", ["low score chunk", "high score chunk"]
    )
    low_id, high_id = chunk_ids

    scored = [(low_id, 0.1), (high_id, 0.9)]
    hits = collapse(db, scored, limit=10)

    assert len(hits) == 1
    assert hits[0].chunk_id == high_id
    assert hits[0].score == 0.9


def test_collapse_never_keeps_first_seen_over_higher_scored(db: sqlite3.Connection) -> None:
    _, chunk_ids = _seed_artifact_with_chunks(
        db, "runbook-b", ["first chunk", "second chunk, better"]
    )
    first_id, second_id = chunk_ids

    # first-seen in `scored` is the lower-scoring one — collapse must not just
    # take whichever it encounters first.
    scored = [(first_id, 0.2), (second_id, 0.8)]
    hits = collapse(db, scored, limit=10)
    assert hits[0].chunk_id == second_id


def test_collapse_preserves_fused_ordering_between_artifacts(db: sqlite3.Connection) -> None:
    _, chunks_a = _seed_artifact_with_chunks(db, "artifact-a", ["a text"])
    _, chunks_b = _seed_artifact_with_chunks(db, "artifact-b", ["b text"])

    scored = [(chunks_b[0], 0.9), (chunks_a[0], 0.5)]
    hits = collapse(db, scored, limit=10)
    assert [h.artifact["name"] for h in hits] == ["artifact-b", "artifact-a"]


def test_collapse_limit_applies_to_artifacts_not_chunks(db: sqlite3.Connection) -> None:
    _, chunks_a = _seed_artifact_with_chunks(db, "artifact-a", ["x", "y", "z"])
    _, chunks_b = _seed_artifact_with_chunks(db, "artifact-b", ["p"])

    scored = [(chunks_a[0], 0.9), (chunks_a[1], 0.8), (chunks_a[2], 0.7), (chunks_b[0], 0.6)]
    hits = collapse(db, scored, limit=1)
    assert len(hits) == 1
    assert hits[0].artifact["name"] == "artifact-a"


def test_collapse_snippet_under_300_chars(db: sqlite3.Connection) -> None:
    long_text = "word " * 500
    _, chunk_ids = _seed_artifact_with_chunks(db, "long-artifact", [long_text])
    hits = collapse(db, [(chunk_ids[0], 1.0)], limit=10, query="word")
    assert len(hits[0].snippet) <= 300
