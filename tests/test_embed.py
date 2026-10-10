"""Tests for headjack.embed.Embedder.

The real model is never loaded in these tests — `_load_model` is monkeypatched to a
fake that returns deterministic unit-norm vectors, with a call counter so cache
behaviour is actually verifiable rather than assumed. The one test that loads the
real model is marked `slow` and excluded from `make check`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pytest

from headjack.embed import Embedder, EmbeddingIdentityError


class _FakeModel:
    """Deterministic stand-in for SentenceTransformer: same text -> same vector,
    unit-norm, without downloading or running anything."""

    def __init__(self) -> None:
        self.call_count = 0
        self.texts_seen: list[str] = []

    def encode(
        self,
        texts: list[str],
        *,
        batch_size: int = 64,
        normalize_embeddings: bool = True,
        convert_to_numpy: bool = True,
        show_progress_bar: bool = False,
    ) -> np.ndarray:
        self.call_count += 1
        self.texts_seen.extend(texts)
        vectors = []
        for text in texts:
            rng = np.random.default_rng(abs(hash(text)) % (2**32))
            v = rng.standard_normal(Embedder.DIM).astype(np.float32)
            v /= np.linalg.norm(v)
            vectors.append(v)
        return np.stack(vectors)


@pytest.fixture
def embedder(tmp_path: Path) -> tuple[Embedder, _FakeModel]:
    e = Embedder(cache_dir=tmp_path)
    fake = _FakeModel()
    e._model = fake  # bypass _load_model entirely — no import attempted
    return e, fake


def test_encode_shape_and_unit_norm(embedder: tuple[Embedder, _FakeModel]) -> None:
    e, _ = embedder
    vectors = e.encode(["hello", "world", "a third string"])
    assert vectors.shape == (3, Embedder.DIM)
    assert vectors.dtype == np.float32
    norms = np.linalg.norm(vectors, axis=1)
    np.testing.assert_allclose(norms, 1.0, atol=1e-5)


def test_encode_one(embedder: tuple[Embedder, _FakeModel]) -> None:
    e, _ = embedder
    vector = e.encode_one("hello")
    assert vector.shape == (Embedder.DIM,)


def test_empty_sequence_returns_empty_array(embedder: tuple[Embedder, _FakeModel]) -> None:
    e, fake = embedder
    vectors = e.encode([])
    assert vectors.shape == (0, Embedder.DIM)
    assert fake.call_count == 0


def test_empty_string_encodes_without_raising(embedder: tuple[Embedder, _FakeModel]) -> None:
    e, _ = embedder
    vector = e.encode_one("")
    assert vector.shape == (Embedder.DIM,)


def test_second_call_on_same_text_hits_cache(embedder: tuple[Embedder, _FakeModel]) -> None:
    e, fake = embedder
    first = e.encode_one("repeat me")
    assert fake.call_count == 1
    second = e.encode_one("repeat me")
    assert fake.call_count == 1  # no second model call
    np.testing.assert_array_equal(first, second)


def test_partial_cache_hit_only_calls_model_for_misses(
    embedder: tuple[Embedder, _FakeModel],
) -> None:
    e, fake = embedder
    e.encode_one("cached already")
    assert fake.call_count == 1

    e.encode(["cached already", "brand new text"])
    assert fake.call_count == 2
    assert fake.texts_seen[-1] == "brand new text"


def test_cache_persists_across_embedder_instances(tmp_path: Path) -> None:
    e1 = Embedder(cache_dir=tmp_path)
    fake1 = _FakeModel()
    e1._model = fake1
    v1 = e1.encode_one("persisted text")

    e2 = Embedder(cache_dir=tmp_path)
    fake2 = _FakeModel()
    e2._model = fake2
    v2 = e2.encode_one("persisted text")

    assert fake2.call_count == 0  # second instance never touched its model
    np.testing.assert_array_equal(v1, v2)


def test_cache_hit_path_works_with_sentence_transformers_unimportable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Warm the cache with a fake model first.
    warm = Embedder(cache_dir=tmp_path)
    warm._model = _FakeModel()
    warm.encode_one("already cached")

    # A fresh Embedder, with sentence_transformers made unimportable, must still
    # serve the cached text without ever attempting the import.
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "sentence_transformers":
            raise ImportError("sentence_transformers is not installed")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)

    cold = Embedder(cache_dir=tmp_path)
    vector = cold.encode_one("already cached")  # must not raise
    assert vector.shape == (Embedder.DIM,)


# ---- ensure_identity ----------------------------------------------------------------


def _meta_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    return conn


def test_ensure_identity_records_on_empty_index() -> None:
    conn = _meta_conn()
    Embedder().ensure_identity(conn)
    model = conn.execute("SELECT value FROM meta WHERE key = 'embedding_model'").fetchone()
    dim = conn.execute("SELECT value FROM meta WHERE key = 'embedding_dim'").fetchone()
    assert model["value"] == Embedder.MODEL
    assert dim["value"] == str(Embedder.DIM)


def test_ensure_identity_is_idempotent_for_matching_model() -> None:
    conn = _meta_conn()
    e = Embedder()
    e.ensure_identity(conn)
    e.ensure_identity(conn)  # must not raise or duplicate rows
    count = conn.execute("SELECT COUNT(*) AS n FROM meta WHERE key = 'embedding_model'").fetchone()[
        "n"
    ]
    assert count == 1


def test_ensure_identity_raises_on_model_mismatch() -> None:
    conn = _meta_conn()
    conn.execute("INSERT INTO meta (key, value) VALUES ('embedding_model', 'some-other-model')")
    conn.execute("INSERT INTO meta (key, value) VALUES ('embedding_dim', '768')")
    conn.commit()

    with pytest.raises(EmbeddingIdentityError, match="some-other-model"):
        Embedder().ensure_identity(conn)


def test_ensure_identity_raises_on_dim_mismatch_alone() -> None:
    conn = _meta_conn()
    conn.execute(f"INSERT INTO meta (key, value) VALUES ('embedding_model', '{Embedder.MODEL}')")
    conn.execute("INSERT INTO meta (key, value) VALUES ('embedding_dim', '999')")
    conn.commit()

    with pytest.raises(EmbeddingIdentityError, match="999"):
        Embedder().ensure_identity(conn)


# ---- the real thing, once ------------------------------------------------------------


@pytest.mark.slow
def test_real_model_loads_and_encodes() -> None:
    e = Embedder()
    vectors = e.encode(["a real sentence for the real model"])
    assert vectors.shape == (1, Embedder.DIM)
    assert abs(float(np.linalg.norm(vectors[0])) - 1.0) < 1e-4
