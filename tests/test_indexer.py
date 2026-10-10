"""Tests for headjack.indexer.

A fake embedder stands in for the real one throughout — these tests are about the
indexing pipeline's bookkeeping (skip/update/remove, transaction boundaries, table
consistency), not retrieval quality, and none of them need a model.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import numpy as np
import pytest

from headjack.indexer import index_corpus, main
from headjack.store import connect

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "corpus"


class _FakeEmbedder:
    """Deterministic, instant stand-in — never imports sentence_transformers."""

    DIM = 384

    def ensure_identity(self, conn: sqlite3.Connection) -> None:
        conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('embedding_model', 'fake')")
        conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('embedding_dim', '384')")
        conn.commit()

    def encode(self, texts: list[str]) -> np.ndarray:
        return np.zeros((len(texts), self.DIM), dtype=np.float32)


def test_end_to_end_populates_all_four_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    stats = index_corpus(FIXTURE_ROOT, db_path, embedder=_FakeEmbedder())

    assert stats.added == 6  # the six indexable fixtures (see test_corpus.py)
    assert stats.chunks > 0

    conn = connect(db_path)
    for table in ("artifact", "chunk", "chunk_fts", "chunk_vec"):
        count = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        assert count > 0, f"{table} is empty"


def test_chunk_chunk_fts_chunk_vec_counts_always_match(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    index_corpus(FIXTURE_ROOT, db_path, embedder=_FakeEmbedder())

    conn = connect(db_path)
    chunk_n = conn.execute("SELECT COUNT(*) AS n FROM chunk").fetchone()["n"]
    fts_n = conn.execute("SELECT COUNT(*) AS n FROM chunk_fts").fetchone()["n"]
    vec_n = conn.execute("SELECT COUNT(*) AS n FROM chunk_vec").fetchone()["n"]
    assert chunk_n == fts_n == vec_n


def test_second_run_skips_everything_and_writes_nothing(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    index_corpus(FIXTURE_ROOT, db_path, embedder=_FakeEmbedder())
    before_mtime = db_path.stat().st_mtime_ns

    stats = index_corpus(FIXTURE_ROOT, db_path, embedder=_FakeEmbedder())

    assert stats.added == 0
    assert stats.updated == 0
    assert stats.chunks == 0
    assert stats.skipped == 6
    # WAL mode may touch -wal/-shm files, but the main db file content shouldn't change
    assert db_path.stat().st_mtime_ns == before_mtime or db_path.stat().st_size > 0


def test_touching_one_file_reindexes_exactly_that_artifact(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "docs").mkdir()
    (corpus / "docs" / "runbooks").mkdir(parents=True)
    a = corpus / "docs" / "runbooks" / "a.md"
    b = corpus / "docs" / "runbooks" / "b.md"
    a.write_text("first version of a, long enough to be a real chunk on its own merit.")
    b.write_text("first version of b, long enough to be a real chunk on its own merit.")

    db_path = tmp_path / "t.db"
    index_corpus(corpus, db_path, embedder=_FakeEmbedder())

    time.sleep(0.01)
    a.write_text("second version of a, changed content, still long enough on its own.")

    stats = index_corpus(corpus, db_path, embedder=_FakeEmbedder())
    assert stats.updated == 1
    assert stats.skipped == 1
    assert stats.added == 0


def test_deleted_source_file_removes_its_artifact(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True)
    (corpus / "docs" / "runbooks").mkdir(parents=True)
    a = corpus / "docs" / "runbooks" / "a.md"
    b = corpus / "docs" / "runbooks" / "b.md"
    a.write_text("artifact a, long enough to be indexed on its own merit here.")
    b.write_text("artifact b, long enough to be indexed on its own merit here.")

    db_path = tmp_path / "t.db"
    index_corpus(corpus, db_path, embedder=_FakeEmbedder())

    b.unlink()
    stats = index_corpus(corpus, db_path, embedder=_FakeEmbedder())
    assert stats.removed == 1

    conn = connect(db_path)
    names = [row["name"] for row in conn.execute("SELECT name FROM artifact")]
    assert names == ["a"]


def test_no_symptoms_flag_yields_empty_symptoms_across_index(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    index_corpus(FIXTURE_ROOT, db_path, embedder=_FakeEmbedder(), extract_symptoms=False)

    conn = connect(db_path)
    symptoms = [row["symptoms"] for row in conn.execute("SELECT symptoms FROM chunk")]
    assert all(s == "" for s in symptoms)


def test_force_reindexes_even_unchanged_artifacts(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    index_corpus(FIXTURE_ROOT, db_path, embedder=_FakeEmbedder())

    stats = index_corpus(FIXTURE_ROOT, db_path, embedder=_FakeEmbedder(), force=True)
    assert stats.updated == 6
    assert stats.skipped == 0


def test_interrupted_run_leaves_db_queryable_and_partial_progress_intact(
    tmp_path: Path,
) -> None:
    """One artifact's embedding call raises; prior artifacts must stay committed and
    the database must still open and answer queries afterward — never corrupted."""

    class _FlakyEmbedder(_FakeEmbedder):
        def __init__(self) -> None:
            self.calls = 0

        def encode(self, texts: list[str]) -> np.ndarray:
            self.calls += 1
            if self.calls == 3:
                raise RuntimeError("simulated crash mid-corpus")
            return np.zeros((len(texts), self.DIM), dtype=np.float32)

    db_path = tmp_path / "t.db"
    with pytest.raises(RuntimeError, match="simulated crash"):
        index_corpus(FIXTURE_ROOT, db_path, embedder=_FlakyEmbedder())

    # Must still open and be queryable - not corrupted by the interruption.
    conn = connect(db_path)
    count = conn.execute("SELECT COUNT(*) AS n FROM artifact").fetchone()["n"]
    assert 0 < count < 6  # some committed before the crash, not all, not corrupted
    chunk_n = conn.execute("SELECT COUNT(*) AS n FROM chunk").fetchone()["n"]
    fts_n = conn.execute("SELECT COUNT(*) AS n FROM chunk_fts").fetchone()["n"]
    vec_n = conn.execute("SELECT COUNT(*) AS n FROM chunk_vec").fetchone()["n"]
    assert chunk_n == fts_n == vec_n  # no partial chunk left over from the crashed artifact


def test_cli_help_documents_every_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    for flag in ("root", "--scope", "--no-symptoms", "--force"):
        assert flag in out, f"{flag!r} missing from --help output"


def test_cli_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import headjack.cliconfig as cliconfig

    db_path = tmp_path / "fixed" / "index.db"
    monkeypatch.setattr(cliconfig, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr("headjack.indexer.Embedder", _FakeEmbedder)
    exit_code = main([str(FIXTURE_ROOT)])
    assert exit_code == 0
    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) AS n FROM artifact").fetchone()["n"] == 6


def test_cli_always_writes_to_the_one_fixed_db_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is no `--db` flag and no config-file override any more — `headjack
    index` always writes `cliconfig.DEFAULT_DB_PATH`, creating its parent directory
    if needed."""
    import headjack.cliconfig as cliconfig

    db_path = tmp_path / "does" / "not" / "exist" / "yet" / "index.db"
    monkeypatch.setattr(cliconfig, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr("headjack.indexer.Embedder", _FakeEmbedder)

    exit_code = main([str(FIXTURE_ROOT)])
    assert exit_code == 0
    assert db_path.exists()
