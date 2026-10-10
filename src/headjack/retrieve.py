"""Sparse (`P1-08`) and dense (`P1-09`) retrieval over the index.

Both return ranked chunk ids and nothing else — fusion (`P1-10`) and the artifact
collapse happen one layer up, in `search.py` (`P1-12`). Keeping that boundary clean is
what lets `P1-16`'s ablation run each retriever in isolation.
"""

from __future__ import annotations

import re

import numpy as np

from headjack.db.types import IndexConnection, is_postgres
from headjack.embed import Embedder

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def fts_escape(query: str) -> str:
    """Turn arbitrary user text into a safe FTS5 MATCH expression.

    Incident queries are hostile to FTS5 syntax: a pasted log line brings quotes,
    colons, asterisks, hyphens and parentheses — all FTS5 operators. Rather than
    escaping each one, this extracts the bare alphanumeric tokens and ORs them
    together: safe by construction, since nothing is left that FTS5 could parse as
    an operator.

    OR, not implicit AND, deliberately: an incident query is often a narrative
    sentence, and AND across ten words usually matches nothing. RRF (`P1-10`) and
    the reranker (`P1-11`) are what narrow this back down to precision. This also
    happens to line up with the schema's tokenizer (`porter unicode61`), which
    already splits hyphenated identifiers like `gcp-eu4-02` into separate indexed
    tokens — so OR-ing the query's own split tokens matches what's actually stored.
    """
    tokens = _TOKEN_RE.findall(query)
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)


def _tsquery_from_query(query: str) -> str:
    """OR-joined tsquery tokens (mirrors fts_escape's OR semantics)."""
    tokens = _TOKEN_RE.findall(query)
    if not tokens:
        return ""
    safe = []
    for t in tokens:
        t = re.sub(r"([&|!():*'\"\\])", r"\\\1", t)
        safe.append(t)
    return " | ".join(safe)


def sparse_search(conn: IndexConnection, query: str, *, scope: str, limit: int = 40) -> list[int]:
    """Return chunk ids, best first, by BM25 over `chunk_fts`.

    `bm25()` returns a more-negative score for a better match, so `ORDER BY
    bm25(...)` ascending (the default, with no ASC/DESC) is correct — do not add
    DESC, that would silently return the worst matches first.
    """
    if is_postgres(conn):
        tsq = _tsquery_from_query(query)
        if not tsq:
            return []
        rows = conn.execute(
            """
            SELECT c.id FROM chunk c
            JOIN artifact a ON a.id = c.artifact_id
            WHERE c.search_vector @@ to_tsquery('english', ?)
              AND a.scope IN (?, 'org')
            ORDER BY ts_rank_cd(c.search_vector, to_tsquery('english', ?), 32) DESC
            LIMIT ?
            """,
            (tsq, scope, tsq, limit),
        ).fetchall()
        return [row["id"] for row in rows]

    escaped = fts_escape(query)
    if not escaped:
        return []

    rows = conn.execute(
        """
        SELECT c.id FROM chunk_fts f
        JOIN chunk c ON c.id = f.rowid
        JOIN artifact a ON a.id = c.artifact_id
        WHERE chunk_fts MATCH ? AND a.scope IN (?, 'org')
        ORDER BY bm25(chunk_fts, 1.0, 1.2, 3.0)
        LIMIT ?
        """,
        (escaped, scope, limit),
    ).fetchall()
    return [row["id"] for row in rows]


def dense_search(
    conn: IndexConnection, embedder: Embedder, query: str, *, scope: str, limit: int = 40
) -> list[int]:
    """Return chunk ids, nearest first, by vector KNN over `chunk_vec`.

    Raises `EmbeddingIdentityError` (via `embedder.ensure_identity`) if the index
    was built with a different embedding model — searching with a mismatched model
    would otherwise return plausible-looking nonsense with no signal that anything
    is wrong.

    The scope filter is a post-KNN join: the `k = limit` nearest neighbours are
    found across *all* scopes first, then filtered down. That means this can
    return fewer than `limit` rows even when the index holds far more than `limit`
    chunks overall — callers must not assume this returns the same count as
    `sparse_search`.
    """
    embedder.ensure_identity(conn)
    query_vector = embedder.encode_one(query)
    if query_vector.dtype != np.float32:
        raise TypeError(f"query vector must be float32, got {query_vector.dtype}")
    qvec = np.ascontiguousarray(query_vector, dtype=np.float32)

    if is_postgres(conn):
        rows = conn.execute(
            """
            SELECT v.chunk_id AS chunk_id
            FROM chunk_vec v
            JOIN chunk c ON c.id = v.chunk_id
            JOIN artifact a ON a.id = c.artifact_id
            WHERE a.scope IN (?, 'org')
            ORDER BY v.embedding <=> ?
            LIMIT ?
            """,
            (scope, qvec, limit),
        ).fetchall()
        return [row["chunk_id"] for row in rows]

    qbytes = qvec.tobytes()
    rows = conn.execute(
        """
        SELECT v.chunk_id AS chunk_id
        FROM chunk_vec v
        JOIN chunk c ON c.id = v.chunk_id
        JOIN artifact a ON a.id = c.artifact_id
        WHERE v.embedding MATCH ? AND k = ? AND a.scope IN (?, 'org')
        ORDER BY distance
        """,
        (qbytes, limit, scope),
    ).fetchall()
    return [row["chunk_id"] for row in rows]
