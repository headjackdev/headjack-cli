"""Tests for headjack.rerank.

Fast tests use a fake cross-encoder (substring match -> high score), so reranking
behaviour is exact and reproducible without downloading a model. The one test that
loads the real model is marked `slow`.
"""

from __future__ import annotations

import time

import pytest

from headjack.rerank import NullReranker, Reranker


class _FakeCrossEncoder:
    """Deterministic: scores a pair by whether the query string literally
    appears in the document, so "planted exact match" tests are exact."""

    def __init__(self) -> None:
        self.call_count = 0

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        self.call_count += 1
        return [10.0 if query in doc else 0.0 for query, doc in pairs]


@pytest.fixture
def reranker() -> tuple[Reranker, _FakeCrossEncoder]:
    r = Reranker()
    fake = _FakeCrossEncoder()
    r._model = fake
    return r, fake


def test_empty_input_returns_empty_and_never_loads_model() -> None:
    r = Reranker()
    assert r.rerank("query", []) == []
    assert r._model is None  # _load_model never called


def test_predict_empty_pairs_returns_empty_without_loading_model() -> None:
    r = Reranker()
    assert r.predict([]) == []
    assert r._model is None


def test_planted_exact_match_at_rank_fifteen_reaches_rank_one(
    reranker: tuple[Reranker, _FakeCrossEncoder],
) -> None:
    r, _ = reranker
    candidates = [(i, f"irrelevant text number {i}") for i in range(20)]
    # plant the exact match at index 14 (fused "rank 15")
    candidates[14] = (14, "the exact phrase QUERY_TOKEN appears right here")

    result = r.rerank("QUERY_TOKEN", candidates, top_n=20)
    assert result[0][0] == 14


def test_rerank_returns_at_most_top_n(reranker: tuple[Reranker, _FakeCrossEncoder]) -> None:
    r, _ = reranker
    candidates = [(i, "text") for i in range(30)]
    result = r.rerank("query", candidates, top_n=20)
    assert len(result) == 20


def test_rerank_drops_candidates_beyond_top_n_rather_than_appending_unscored(
    reranker: tuple[Reranker, _FakeCrossEncoder],
) -> None:
    r, _ = reranker
    candidates = [(i, "text") for i in range(25)]
    result = r.rerank("query", candidates, top_n=20)
    returned_ids = {chunk_id for chunk_id, _ in result}
    assert returned_ids == set(range(20))  # the tail (20-24) is gone, not appended


def test_scores_are_finite_floats(reranker: tuple[Reranker, _FakeCrossEncoder]) -> None:
    r, _ = reranker
    result = r.rerank("query", [(1, "text")])
    assert isinstance(result[0][1], float)
    assert result[0][1] in (10.0, 0.0)


def test_document_text_is_truncated_before_reaching_the_model() -> None:
    r = Reranker()
    captured: list[tuple[str, str]] = []

    class _CapturingModel:
        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            captured.extend(pairs)
            return [0.0 for _ in pairs]

    r._model = _CapturingModel()
    huge_doc = "x" * 5000
    r.predict([("q", huge_doc)])
    assert len(captured[0][1]) <= 2000


def test_predict_logs_wall_time(
    reranker: tuple[Reranker, _FakeCrossEncoder], caplog: pytest.LogCaptureFixture
) -> None:
    r, _ = reranker
    with caplog.at_level("DEBUG", logger="headjack.rerank"):
        r.predict([("q", "d")])
    assert any("pairs in" in record.message for record in caplog.records)


# ---- NullReranker ------------------------------------------------------------------


def test_null_reranker_preserves_fused_order() -> None:
    candidates = [(i, "text") for i in range(5)]
    result = NullReranker().rerank("query", candidates)
    assert [chunk_id for chunk_id, _ in result] == [0, 1, 2, 3, 4]


def test_null_reranker_scores_are_descending() -> None:
    candidates = [(i, "text") for i in range(5)]
    result = NullReranker().rerank("query", candidates)
    scores = [score for _, score in result]
    assert scores == sorted(scores, reverse=True)


def test_null_reranker_truncates_to_top_n() -> None:
    candidates = [(i, "text") for i in range(10)]
    result = NullReranker().rerank("query", candidates, top_n=3)
    assert len(result) == 3
    assert [chunk_id for chunk_id, _ in result] == [0, 1, 2]


def test_null_reranker_empty_input() -> None:
    assert NullReranker().rerank("query", []) == []


# ---- the real model, once -----------------------------------------------------------


@pytest.mark.slow
def test_real_cross_encoder_reorders_a_genuine_candidate_set() -> None:
    r = Reranker()
    candidates = [
        (1, "deploy the frontend using the standard release pipeline"),
        (2, "run nodetool repair after a Cassandra node rejoins the cluster"),
        (3, "update the marketing newsletter template"),
    ]
    start = time.monotonic()
    result = r.rerank("a cassandra node came back but data looks inconsistent", candidates)
    elapsed_ms = (time.monotonic() - start) * 1000
    print(f"real cross-encoder rerank of {len(candidates)} pairs: {elapsed_ms:.1f} ms")

    assert result[0][0] == 2
