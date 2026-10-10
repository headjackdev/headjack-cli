"""Tests for headjack.retrieve.sparse_search and fts_escape."""

from __future__ import annotations

import itertools
import sqlite3
from pathlib import Path

import pytest

from headjack.retrieve import fts_escape, sparse_search
from headjack.store import connect

CASSANDRA_LITERALS = {
    "BackoffLimitExceeded": "job failed with BackoffLimitExceeded repeatedly overnight",
    "K8SeedProvider3x": "the seed provider K8SeedProvider3x needs updating before restart",
    "Started Cassandra": 'log line "Started Cassandra" prints every 120 seconds on boot',
    "stock_event": "the affected table is stock_event in the primary keyspace",
    "gcp-eu4-02": "deployed to gcp-eu4-02 overnight without a canary",
    "ca7585a": "the commit that broke it was ca7585a, reverted the next morning",
}

HOSTILE_QUERIES = [
    "K8SeedProvider3x",
    '"Started Cassandra"',
    "stock_event",
    "gcp-eu4-02",
    "ca7585a",
    "NEAR/5",
    "foo*",
    "a OR b",
    "(unbalanced",
    "col:value",
    "",
    " ".join(f"word{i}" for i in range(500)),
]


_name_seq = itertools.count()


def _seed(
    conn: sqlite3.Connection, text: str, *, scope: str = "personal", symptoms: str = ""
) -> int:
    # A counter, not a truncated slice of `text` — several tests seed many chunks
    # whose text differs only after the first few characters, which previously
    # collided on the artifact-identity unique constraint.
    artifact_id = conn.execute(
        "INSERT INTO artifact (kind, name, content_hash, scope, body, updated_at) "
        "VALUES ('skill', ?, 'h', ?, 'b', '2026-01-01T00:00:00Z')",
        (f"artifact-{next(_name_seq)}", scope),
    ).lastrowid
    chunk_id = conn.execute(
        "INSERT INTO chunk (artifact_id, heading, text, symptoms) VALUES (?, 'H', ?, ?)",
        (artifact_id, text, symptoms),
    ).lastrowid
    conn.execute(
        "INSERT INTO chunk_vec (chunk_id, embedding) VALUES (?, ?)",
        (chunk_id, (b"\x00\x00\x80?") * 384),
    )
    conn.commit()
    assert chunk_id is not None
    return chunk_id


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    return connect(tmp_path / "sparse.db")


# ---- the Cassandra outage literals, each findable ------------------------------------


@pytest.mark.parametrize("literal,text", CASSANDRA_LITERALS.items())
def test_cassandra_literal_retrieves_its_chunk_at_rank_one(
    db: sqlite3.Connection, literal: str, text: str
) -> None:
    target = _seed(db, text)
    _seed(db, "unrelated padding chunk about something else entirely")
    _seed(db, "another unrelated chunk discussing a different system")

    results = sparse_search(db, literal, scope="personal")
    assert results, f"no results for {literal!r}"
    assert results[0] == target


# ---- hostile input never raises -------------------------------------------------------


@pytest.mark.parametrize("query", HOSTILE_QUERIES)
def test_hostile_query_never_raises(db: sqlite3.Connection, query: str) -> None:
    _seed(db, "some baseline content so the index isn't empty")
    results = sparse_search(db, query, scope="personal")
    assert isinstance(results, list)


def test_empty_query_returns_empty_without_touching_db(db: sqlite3.Connection) -> None:
    assert sparse_search(db, "", scope="personal") == []
    assert fts_escape("") == ""
    assert fts_escape("   ") == ""


def test_fts_escape_ors_tokens() -> None:
    assert fts_escape("foo bar") == '"foo" OR "bar"'


def test_fts_escape_splits_hyphenated_identifiers() -> None:
    # matches how the schema's porter unicode61 tokenizer splits the indexed text
    assert fts_escape("gcp-eu4-02") == '"gcp" OR "eu4" OR "02"'


# ---- symptoms column weighting ---------------------------------------------------------


def test_symptoms_match_outranks_buried_match(db: sqlite3.Connection) -> None:
    buried = _seed(
        db,
        "a very long chunk of prose that happens to mention disablehints only once, "
        "surrounded by a great deal of unrelated padding text to dilute its weight "
        "in the body column so the symptoms boost is what should decide the order",
    )
    symptom_hit = _seed(db, "short chunk", symptoms="disablehints")

    results = sparse_search(db, "disablehints", scope="personal")
    assert results[0] == symptom_hit
    assert buried in results


# ---- scope isolation --------------------------------------------------------------------


def test_personal_scope_invisible_to_other_scope(db: sqlite3.Connection) -> None:
    _seed(db, "secret personal content about gcp-eu4-02", scope="personal")
    results = sparse_search(db, "gcp-eu4-02", scope="team")
    assert results == []


def test_org_scope_visible_to_all(db: sqlite3.Connection) -> None:
    target = _seed(db, "shared org content about gcp-eu4-02", scope="org")
    results = sparse_search(db, "gcp-eu4-02", scope="team")
    assert target in results
    results2 = sparse_search(db, "gcp-eu4-02", scope="personal")
    assert target in results2


# ---- determinism and limit ---------------------------------------------------------------


def test_deterministic(db: sqlite3.Connection) -> None:
    _seed(db, "repeatable content about gcp-eu4-02 outage")
    first = sparse_search(db, "gcp-eu4-02", scope="personal")
    second = sparse_search(db, "gcp-eu4-02", scope="personal")
    assert first == second


def test_limit_is_respected(db: sqlite3.Connection) -> None:
    for i in range(10):
        _seed(db, f"chunk number {i} discussing gcp-eu4-02 repeatedly")
    results = sparse_search(db, "gcp-eu4-02", scope="personal", limit=3)
    assert len(results) == 3
