"""The `headjack index` command: crawl -> chunk -> extract -> embed -> persist.

Builds a *local* SQLite file — the Phase 1 ablation runner's (`P1-16`) index, or an
offline working copy for research and ops. This never reaches a remote Headjack;
`headjack publish` is how a corpus actually gets into one. The directory it points
at is whatever corpus is being indexed or evaluated, never a user's own installed
skills.

Incremental by content hash: an unchanged artifact costs nothing, not even an
embedding call. That is what makes the Phase 1 ablation runner and ordinary
iterative corpus work tolerable — re-running over a corpus that hasn't moved should
be close to instant.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path

from headjack import cliconfig
from headjack.chunker import chunk_markdown
from headjack.corpus import crawl
from headjack.embed import Embedder
from headjack.store import connect


@dataclass
class IndexStats:
    added: int = 0
    updated: int = 0
    skipped: int = 0
    removed: int = 0
    chunks: int = 0
    elapsed_s: float = 0.0


def index_corpus(
    root: Path,
    db_path: Path,
    *,
    scope: str = "personal",
    extract_symptoms: bool = True,
    force: bool = False,
    embedder: Embedder | None = None,
) -> IndexStats:
    """Crawl `root` and bring `db_path` up to date with it.

    Deletion scope: an artifact is removed if it has this run's `scope` and its
    `path` was not seen in this crawl. That is correct for the one-corpus-per-database
    workflow this targets; indexing two distinct corpora into the same database under
    the same scope, where a file removed from one happens to share a path with a file
    in the other, is not something this guards against.
    """
    conn = connect(db_path)
    embedder = embedder or Embedder()
    embedder.ensure_identity(conn)

    stats = IndexStats()
    start = time.monotonic()

    before_paths = {
        row["path"]
        for row in conn.execute("SELECT path FROM artifact WHERE scope = ?", (scope,)).fetchall()
    }
    seen_paths: set[str] = set()

    for artifact in crawl(root, scope=scope):
        seen_paths.add(artifact.path)
        existing = conn.execute(
            "SELECT id, content_hash FROM artifact WHERE path = ? AND scope = ?",
            (artifact.path, scope),
        ).fetchone()

        if existing is not None and existing["content_hash"] == artifact.content_hash and not force:
            stats.skipped += 1
            continue

        with conn:  # one transaction per artifact: a crash here leaves the index
            # consistent up to the last fully-written artifact, never a half-written one
            if existing is not None:
                conn.execute("DELETE FROM artifact WHERE id = ?", (existing["id"],))

            cur = conn.execute(
                "INSERT INTO artifact "
                "(kind, name, repo, path, git_sha, version, content_hash, scope, "
                "owner, reviewed_by, systems, body, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    artifact.kind,
                    artifact.name,
                    artifact.repo,
                    artifact.path,
                    artifact.git_sha,
                    artifact.version,
                    artifact.content_hash,
                    artifact.scope,
                    artifact.owner,
                    artifact.reviewed_by,
                    json.dumps(artifact.systems),
                    artifact.body,
                    artifact.updated_at,
                ),
            )
            artifact_id = cur.lastrowid

            chunks = chunk_markdown(artifact.body, extract=extract_symptoms)
            if chunks:
                vectors = embedder.encode([chunk.text for chunk in chunks])
                for chunk, vector in zip(chunks, vectors, strict=True):
                    chunk_cur = conn.execute(
                        "INSERT INTO chunk (artifact_id, heading, text, symptoms) "
                        "VALUES (?, ?, ?, ?)",
                        (artifact_id, chunk.heading, chunk.text, chunk.symptoms),
                    )
                    conn.execute(
                        "INSERT INTO chunk_vec (chunk_id, embedding) VALUES (?, ?)",
                        (chunk_cur.lastrowid, vector.tobytes()),
                    )
                stats.chunks += len(chunks)

        if existing is not None:
            stats.updated += 1
        else:
            stats.added += 1

    for stale_path in before_paths - seen_paths:
        conn.execute("DELETE FROM artifact WHERE path = ? AND scope = ?", (stale_path, scope))
        stats.removed += 1
    if stats.removed:
        conn.commit()

    conn.close()
    stats.elapsed_s = time.monotonic() - start
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="headjack index", description="Build or update the index from a corpus"
    )
    parser.add_argument("root", type=Path, help="corpus root directory to crawl")
    parser.add_argument(
        "--scope",
        default="personal",
        choices=["personal", "team", "org"],
        help="visibility scope recorded on every indexed artifact (default: personal)",
    )
    parser.add_argument(
        "--no-symptoms",
        action="store_true",
        help="skip symptom-literal extraction (for the Phase 1 ablation)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="reindex every artifact regardless of content hash",
    )
    args = parser.parse_args(argv)

    db = cliconfig.DEFAULT_DB_PATH
    db.parent.mkdir(parents=True, exist_ok=True)

    stats = index_corpus(
        args.root,
        db,
        scope=args.scope,
        extract_symptoms=not args.no_symptoms,
        force=args.force,
    )

    db_size = db.stat().st_size if db.exists() else 0
    print(
        f"added {stats.added}, updated {stats.updated}, skipped {stats.skipped}, "
        f"removed {stats.removed}, {stats.chunks} chunks written, "
        f"{stats.elapsed_s:.2f}s, db size {db_size} bytes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
