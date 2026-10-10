"""Reciprocal Rank Fusion and the chunk-to-artifact collapse.

RRF is the standard fuser for combining sparse and dense rankings because it
operates on *ranks*, sidestepping the score-incompatibility that breaks naive
weighted averaging between BM25 scores and cosine distances.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from headjack.db.types import IndexConnection
from headjack.snippet import snippet

RRF_K = 60  # standard; dampens tail contribution. Rarely needs tuning.


@dataclass(frozen=True)
class Hit:
    artifact: dict[str, Any]
    chunk_id: int
    heading: str | None
    snippet: str
    score: float


def rrf(*ranked_lists: Sequence[int], k: int = RRF_K) -> list[tuple[int, float]]:
    """Fuse ranked id lists. Score = sum of 1/(k + rank + 1) across lists, rank
    0-based. Ties break by id ascending, so the result is fully deterministic —
    `P1-16`'s ablation table has to be reproducible, not just "usually the same".
    """
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, item_id in enumerate(ranked):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))


def collapse(
    conn: IndexConnection, scored: Sequence[tuple[int, float]], limit: int, query: str = ""
) -> list[Hit]:
    """Chunks -> artifacts, keeping each artifact's highest-scoring chunk.

    `query` isn't in the original sketch of this signature, but `snippet()` can't
    produce a query-centred window without it, so it's a required-by-default
    keyword here rather than threading raw chunk text back out to the caller.

    Five chunks from the same runbook is one answer, not five — this is what keeps
    result diversity without losing the best-matching section.
    """
    best: dict[int, Hit] = {}

    for chunk_id, score in scored:
        row = conn.execute(
            """
            SELECT
                a.id AS artifact_id, a.kind, a.name, a.repo, a.path, a.version,
                a.content_hash, a.scope, a.owner, a.reviewed_by, a.systems, a.updated_at,
                c.heading AS chunk_heading, c.text AS chunk_text
            FROM chunk c JOIN artifact a ON a.id = c.artifact_id
            WHERE c.id = ?
            """,
            (chunk_id,),
        ).fetchone()
        if row is None:
            continue

        artifact_id = row["artifact_id"]
        if artifact_id in best and score <= best[artifact_id].score:
            continue

        artifact = {
            "id": row["artifact_id"],
            "kind": row["kind"],
            "name": row["name"],
            "repo": row["repo"],
            "path": row["path"],
            "version": row["version"],
            "content_hash": row["content_hash"],
            "scope": row["scope"],
            "owner": row["owner"],
            "reviewed_by": row["reviewed_by"],
            "systems": row["systems"],
            "updated_at": row["updated_at"],
        }
        best[artifact_id] = Hit(
            artifact=artifact,
            chunk_id=chunk_id,
            heading=row["chunk_heading"],
            snippet=snippet(row["chunk_text"], query),
            score=score,
        )

    ordered = sorted(best.values(), key=lambda hit: -hit.score)
    return ordered[:limit]
