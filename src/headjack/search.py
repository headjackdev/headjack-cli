"""The end-to-end hybrid search pipeline: sparse + dense -> RRF -> rerank -> collapse.

This is `search()` from the spec, assembled from pieces each already tested in
isolation (`P1-08` through `P1-11`). If this file contains retrieval logic of its
own, the layering is wrong and `P1-16`'s per-stage ablation stops describing what
actually runs.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Protocol

import httpx

from headjack import cliconfig
from headjack.db.types import IndexConnection
from headjack.embed import Embedder
from headjack.fuse import Hit, collapse, rrf
from headjack.rerank import NullReranker, Reranker
from headjack.retrieve import dense_search, sparse_search
from headjack.store import connect

_LOCAL_ONLY_FLAGS = ("explain", "no_sparse", "no_rerank")

_RERANK_POOL = 20  # candidates considered for reranking — see spec §4


class _RerankerLike(Protocol):
    def rerank(
        self, query: str, candidates: list[tuple[int, str]], *, top_n: int = 20
    ) -> list[tuple[int, float]]: ...


def search(
    conn: IndexConnection,
    embedder: Embedder,
    reranker: _RerankerLike,
    query: str,
    *,
    scope: str = "personal",
    k: int = 5,
    use_sparse: bool = True,
    use_dense: bool = True,
) -> list[Hit]:
    """sparse 40 -> dense 40 -> RRF -> top 20 -> rerank -> collapse -> top k.

    `use_sparse` and `use_dense` are what `P1-16`'s ablation toggles. Both `False`
    is a programming error, not an empty result — it means the caller asked for a
    search that does nothing.
    """
    if not use_sparse and not use_dense:
        raise ValueError("search() needs at least one of use_sparse or use_dense")
    if not query.strip():
        return []

    ranked_lists: list[list[int]] = []
    if use_sparse:
        ranked_lists.append(sparse_search(conn, query, scope=scope))
    if use_dense:
        ranked_lists.append(dense_search(conn, embedder, query, scope=scope))

    fused = rrf(*ranked_lists)
    if not fused:
        return []

    top_ids = [chunk_id for chunk_id, _score in fused[:_RERANK_POOL]]
    candidates = _load_chunk_texts(conn, top_ids)
    reranked = reranker.rerank(query, candidates, top_n=_RERANK_POOL)

    return collapse(conn, reranked, limit=k, query=query)


def _load_chunk_texts(conn: IndexConnection, chunk_ids: list[int]) -> list[tuple[int, str]]:
    """Fetch raw chunk text for reranking, preserving `chunk_ids`' order — the
    fused rank order, which `NullReranker` depends on to approximate "no rerank"."""
    if not chunk_ids:
        return []
    placeholders = ",".join("?" for _ in chunk_ids)
    rows = conn.execute(
        f"SELECT id, text FROM chunk WHERE id IN ({placeholders})", chunk_ids
    ).fetchall()
    text_by_id = {row["id"]: row["text"] for row in rows}
    return [(cid, text_by_id[cid]) for cid in chunk_ids if cid in text_by_id]


def _hit_to_json(hit: Hit) -> dict[str, object]:
    """The `/v1/search` response shape from spec §4 — matching it here means
    `P2-02` can be a thin wrapper rather than a reimplementation."""
    return {
        "name": hit.artifact["name"],
        "kind": hit.artifact["kind"],
        "version": hit.artifact["version"],
        "installable": hit.artifact["kind"] == "skill",
        "source": f"{hit.artifact.get('repo') or ''}/{hit.artifact.get('path') or ''}",
        "matched_in": hit.heading,
        "snippet": hit.snippet,
        "score": round(hit.score, 3),
        "reviewed": bool(hit.artifact["reviewed_by"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="headjack search", description="Query the index")
    parser.add_argument("query", help="the search query")
    parser.add_argument("-k", type=int, default=5, help="number of results (default: 5)")
    parser.add_argument(
        "--scope", default="personal", choices=["personal", "team", "org"], help="search scope"
    )
    parser.add_argument("--json", action="store_true", help="emit the /v1/search JSON shape")
    parser.add_argument(
        "--explain",
        action="store_true",
        help="print per-stage candidate counts and timings (local index only)",
    )
    parser.add_argument(
        "--no-sparse",
        action="store_true",
        help="disable sparse (BM25) retrieval (local index only)",
    )
    parser.add_argument(
        "--no-dense",
        action="store_true",
        help="disable dense (vector) retrieval; for --remote this sends no query_vector",
    )
    parser.add_argument(
        "--no-rerank",
        action="store_true",
        help="skip the cross-encoder, use NullReranker (local index only)",
    )
    args = parser.parse_args(argv)

    # The one fixed local index (see cliconfig.DEFAULT_DB_PATH). If `headjack index`
    # has ever been run, search it; otherwise fall through to the remote catalog —
    # there is no flag for either side of this, only the one local path and the one
    # configured remote.
    if cliconfig.DEFAULT_DB_PATH.exists():
        return _search_local(cliconfig.DEFAULT_DB_PATH, args)

    used_local_only_flags = [
        f"--{flag.replace('_', '-')}" for flag in _LOCAL_ONLY_FLAGS if getattr(args, flag)
    ]
    if used_local_only_flags:
        parser.error(
            f"{', '.join(used_local_only_flags)} only apply to the local index "
            f"(run `headjack index` first)"
        )

    config = cliconfig.load()
    remote = cliconfig.resolve_remote(None)
    token = config.get("token")
    if not token:
        parser.error("no token configured — run `headjack init --token <TOKEN>` first")
    return _search_remote(remote, token, args)


def _search_local(db: Path, args: argparse.Namespace) -> int:
    use_sparse = not args.no_sparse
    use_dense = not args.no_dense
    conn = connect(db)
    embedder = Embedder()
    reranker: _RerankerLike = NullReranker() if args.no_rerank else Reranker()

    if args.explain:
        t0 = time.monotonic()
        sparse_ids = sparse_search(conn, args.query, scope=args.scope) if use_sparse else []
        t1 = time.monotonic()
        dense_ids = dense_search(conn, embedder, args.query, scope=args.scope) if use_dense else []
        t2 = time.monotonic()
        fused = rrf(sparse_ids, dense_ids)
        t3 = time.monotonic()
        top_ids = [cid for cid, _score in fused[:_RERANK_POOL]]
        candidates = _load_chunk_texts(conn, top_ids)
        reranked = reranker.rerank(args.query, candidates, top_n=_RERANK_POOL)
        t4 = time.monotonic()
        hits = collapse(conn, reranked, limit=args.k, query=args.query)
        t5 = time.monotonic()
        print(
            f"sparse: {len(sparse_ids)} candidates, {(t1 - t0) * 1000:.1f} ms\n"
            f"dense:  {len(dense_ids)} candidates, {(t2 - t1) * 1000:.1f} ms\n"
            f"fuse:   {len(fused)} fused, {(t3 - t2) * 1000:.1f} ms\n"
            f"rerank: {len(reranked)} scored, {(t4 - t3) * 1000:.1f} ms\n"
            f"collapse: {len(hits)} hits, {(t5 - t4) * 1000:.1f} ms\n"
        )
    else:
        hits = search(
            conn,
            embedder,
            reranker,
            args.query,
            scope=args.scope,
            k=args.k,
            use_sparse=use_sparse,
            use_dense=use_dense,
        )

    if args.json:
        print(json.dumps({"results": [_hit_to_json(h) for h in hits]}, indent=2))
    elif not hits:
        print(f"no results for {args.query!r}", file=sys.stderr)
    else:
        for i, hit in enumerate(hits, 1):
            print(f"{i}. {hit.artifact['name']} ({hit.artifact['kind']}) — {hit.heading}")
            print(f"   {hit.snippet}")
            print(f"   score={hit.score:.3f}  source={hit.artifact.get('path')}")
            print()

    return 0


def _search_remote(
    remote: str, token: str, args: argparse.Namespace, *, client: httpx.Client | None = None
) -> int:
    """Hit a live server's `/v1/search` instead of a local file. The server runs
    its own sparse+dense+fuse+rerank pipeline — the only knob the client has is
    whether to send a `query_vector` at all (`--no-dense` sends none, sparse-only
    on the server side, same semantic `--no-dense` has locally).

    `client` is injectable so tests can pass a `TestClient` bound to a real,
    in-process server instead of hitting the network — production callers (the CLI
    below) always get a real `httpx.Client`."""
    query_vector = None if args.no_dense else Embedder().encode_one(args.query).tolist()
    body = {"query": args.query, "query_vector": query_vector, "k": args.k, "scope": args.scope}
    headers = {"Authorization": f"Bearer {token}"}
    owns_client = client is None
    client = client or httpx.Client(base_url=remote, timeout=30.0)

    try:
        resp = client.post("/v1/search", json=body, headers=headers)
    except httpx.ConnectError as exc:
        print(f"could not connect to {remote}: {exc}", file=sys.stderr)
        return 1
    except httpx.TimeoutException as exc:
        print(f"{remote} did not respond in time: {exc}", file=sys.stderr)
        return 1
    finally:
        if owns_client:
            client.close()

    if resp.status_code == 401:
        print("the server rejected the token (401 unauthorized)", file=sys.stderr)
        return 1
    if resp.status_code == 403:
        print("the server rejected this request (403 forbidden)", file=sys.stderr)
        return 1
    if resp.is_error:
        print(f"server returned {resp.status_code}: {resp.text}", file=sys.stderr)
        return 1

    results: list[dict[str, Any]] = resp.json().get("results", [])

    if args.json:
        print(json.dumps({"results": results}, indent=2))
    elif not results:
        print(f"no results for {args.query!r}", file=sys.stderr)
    else:
        for i, r in enumerate(results, 1):
            heading = f" — {r['matched_in']}" if r.get("matched_in") else ""
            print(f"{i}. {r['name']} ({r['kind']}){heading}")
            if r.get("snippet"):
                print(f"   {r['snippet']}")
            print(f"   score={r['score']:.3f}  source={r.get('source')}")
            print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
