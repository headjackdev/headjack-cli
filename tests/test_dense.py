"""Tests for headjack.retrieve.dense_search.

Fast tests use a fake embedder with hand-assigned vectors, so KNN scenarios are
exact and reproducible without a model. The two tests that need genuine semantic
behaviour (paraphrase retrieval, "sensible neighbours") use the real `Embedder` and
are marked `slow`.
"""

from __future__ import annotations

import itertools
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from headjack.embed import Embedder, EmbeddingIdentityError
from headjack.retrieve import dense_search
from headjack.store import connect

_name_seq = itertools.count()


class _FakeEmbedder:
    DIM = 384

    def __init__(self, vectors: dict[str, np.ndarray] | None = None) -> None:
        self.vectors = vectors or {}

    def ensure_identity(self, conn: sqlite3.Connection) -> None:
        conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('embedding_model', 'fake')")
        conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('embedding_dim', '384')")
        conn.commit()

    def encode_one(self, text: str) -> np.ndarray:
        if text in self.vectors:
            return self.vectors[text]
        return np.zeros(self.DIM, dtype=np.float32)


def _unit(dim: int, dtype: type = np.float32) -> np.ndarray:
    v = np.zeros(384, dtype=dtype)
    v[dim] = 1.0
    return v


def _seed(conn: sqlite3.Connection, vector: np.ndarray, *, scope: str = "personal") -> int:
    artifact_id = conn.execute(
        "INSERT INTO artifact (kind, name, content_hash, scope, body, updated_at) "
        "VALUES ('skill', ?, 'h', ?, 'b', '2026-01-01T00:00:00Z')",
        (f"artifact-{next(_name_seq)}", scope),
    ).lastrowid
    chunk_id = conn.execute(
        "INSERT INTO chunk (artifact_id, heading, text, symptoms) VALUES (?, 'H', 't', '')",
        (artifact_id,),
    ).lastrowid
    conn.execute(
        "INSERT INTO chunk_vec (chunk_id, embedding) VALUES (?, ?)",
        (chunk_id, vector.astype(np.float32).tobytes()),
    )
    conn.commit()
    assert chunk_id is not None
    return chunk_id


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    return connect(tmp_path / "dense.db")


def test_nearest_neighbour_is_the_closest_vector(db: sqlite3.Connection) -> None:
    target = _seed(db, _unit(0))
    _seed(db, _unit(1))
    _seed(db, _unit(2))

    fake = _FakeEmbedder({"query": _unit(0)})
    results = dense_search(db, fake, "query", scope="personal")
    assert results[0] == target


def test_non_float32_query_vector_raises(db: sqlite3.Connection) -> None:
    _seed(db, _unit(0))
    fake = _FakeEmbedder({"query": _unit(0, dtype=np.float64)})
    with pytest.raises(TypeError, match="float32"):
        dense_search(db, fake, "query", scope="personal")


def test_embedding_model_mismatch_raises(db: sqlite3.Connection) -> None:
    db.execute("INSERT INTO meta (key, value) VALUES ('embedding_model', 'some-other-model')")
    db.execute("INSERT INTO meta (key, value) VALUES ('embedding_dim', '768')")
    db.commit()

    # The real Embedder, not the fake: its ensure_identity is what actually
    # detects the mismatch, and dense_search raises before ever reaching
    # encode_one — so this stays fast and never attempts to load a model.
    with pytest.raises(EmbeddingIdentityError):
        dense_search(db, Embedder(), "query", scope="personal")


def test_empty_index_returns_empty_list(db: sqlite3.Connection) -> None:
    fake = _FakeEmbedder({"query": _unit(0)})
    assert dense_search(db, fake, "query", scope="personal") == []


def test_scope_filtering(db: sqlite3.Connection) -> None:
    _seed(db, _unit(0), scope="personal")
    team_target = _seed(db, _unit(0), scope="team")
    org_target = _seed(db, _unit(0), scope="org")

    fake = _FakeEmbedder({"query": _unit(0)})
    results = dense_search(db, fake, "query", scope="team")

    assert team_target in results
    assert org_target in results
    # the personal-scope chunk belongs to a different caller and must not appear
    personal_ids = {
        row["id"]
        for row in db.execute(
            "SELECT c.id AS id FROM chunk c JOIN artifact a ON a.id = c.artifact_id "
            "WHERE a.scope = 'personal'"
        )
    }
    assert not (personal_ids & set(results))


def test_deterministic(db: sqlite3.Connection) -> None:
    _seed(db, _unit(0))
    _seed(db, _unit(1))
    fake = _FakeEmbedder({"query": _unit(0)})
    first = dense_search(db, fake, "query", scope="personal")
    second = dense_search(db, fake, "query", scope="personal")
    assert first == second


def test_limit_can_return_fewer_rows_than_requested_after_scope_filter(
    db: sqlite3.Connection,
) -> None:
    # k nearest neighbours are found across ALL scopes before the scope filter is
    # applied, so asking for personal-scope results out of a mostly-team-scope
    # index can legitimately return fewer than `limit`.
    _seed(db, _unit(0), scope="team")
    _seed(db, _unit(1), scope="team")
    target = _seed(db, _unit(2), scope="personal")

    fake = _FakeEmbedder({"query": _unit(2)})
    results = dense_search(db, fake, "query", scope="personal", limit=1)
    assert results == [target]


# ---- real semantic behaviour, once -----------------------------------------------------


@pytest.mark.slow
def test_paraphrase_with_no_shared_tokens_retrieves_target_in_top_five(
    db: sqlite3.Connection,
) -> None:
    embedder = Embedder()
    target_text = "the import job became extremely slow starting around midnight"
    distractor_texts = [
        "deploy the frontend using the standard release pipeline",
        "rotate the TLS certificate before it expires next month",
        "update the on-call rotation schedule for next quarter",
    ]

    target = _seed(db, embedder.encode_one(target_text))
    for text in distractor_texts:
        _seed(db, embedder.encode_one(text))

    results = dense_search(
        db, embedder, "overnight the data load got really slow", scope="personal"
    )
    assert target in results[:5]


@pytest.mark.slow
def test_no_exact_literal_match_still_returns_sensible_neighbours(
    db: sqlite3.Connection,
) -> None:
    embedder = Embedder()
    cassandra_text = "nodetool repair is required after a node rejoins the cluster"
    unrelated_text = "update the marketing newsletter template for the spring campaign"

    cassandra_chunk = _seed(db, embedder.encode_one(cassandra_text))
    _seed(db, embedder.encode_one(unrelated_text))

    results = dense_search(
        db, embedder, "a database node came back but data looks inconsistent", scope="personal"
    )
    assert results[0] == cassandra_chunk
