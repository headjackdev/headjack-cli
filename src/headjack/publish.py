"""The `headjack publish` command: chunk and embed a corpus locally, push the result
to a remote Headjack server over HTTP.

The server never crawls or embeds - this is the existing Phase 1
pipeline (`crawl` -> `chunk_markdown` -> `Embedder.encode`) with its last step
swapped: instead of writing to a local SQLite file (`P1-07`'s `headjack index`), it
`PUT`s each artifact to `P2-07`'s write API. `P1-05`'s crawl and `git_sha` pinning are
unaffected; this only changes where the result goes.
"""

from __future__ import annotations

import argparse
import base64
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

from headjack import cliconfig
from headjack.bundle import bundle_hash
from headjack.chunker import chunk_markdown
from headjack.corpus import crawl
from headjack.embed import Embedder

logger = logging.getLogger(__name__)


class PublishAuthError(RuntimeError):
    """A 401/403 from the remote - a token problem, not a per-artifact failure.
    Aborts the whole run immediately rather than being logged and skipped."""


@dataclass
class PublishStats:
    added: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    pruned: int = 0


def _resolve_git_sha(root: Path, rel_path: str) -> str | None:
    """Last commit touching `rel_path`, or `None` if the path isn't tracked (not a
    git checkout, or the file is untracked/ignored) - best-effort, never fatal."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%H", "--", rel_path],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = result.stdout.strip()
    return sha or None


def _raise_if_auth_error(response: httpx.Response) -> None:
    if response.status_code in (401, 403):
        raise PublishAuthError(
            f"{response.status_code} from the remote - check the token (never printed)"
        )


def publish(
    root: Path,
    remote: str,
    token: str,
    *,
    scope: str = "personal",
    use_git: bool = False,
    force: bool = False,
    prune: bool = False,
    embedder: Embedder | None = None,
    client: httpx.Client | None = None,
) -> PublishStats:
    """Crawl `root`, skip anything the remote already has at the same content hash,
    chunk and embed the rest locally, and `PUT` each to `remote`.

    Raises `PublishAuthError` immediately on any 401/403 - a token problem affects
    every request, not just one artifact. Any other per-artifact failure (a 422
    dimension mismatch, a transient 5xx) is logged and counted in `stats.failed`;
    one bad file must not abort a publish of five hundred.
    """
    embedder = embedder or Embedder()
    owns_client = client is None
    client = client or httpx.Client(base_url=remote, timeout=30.0)
    # Headers are passed per-request, not set as client defaults: a caller (tests,
    # mainly) can inject an already-built client - e.g. FastAPI's TestClient, which
    # is itself an httpx.Client subclass - without this function silently sending
    # unauthenticated requests through it.
    auth_header = {"Authorization": f"Bearer {token}"}
    stats = PublishStats()

    try:
        resp = client.get("/v1/artifacts", headers=auth_header)
        _raise_if_auth_error(resp)
        resp.raise_for_status()
        remote_index = {(row["kind"], row["name"]): row["content_hash"] for row in resp.json()}

        seen: set[tuple[str, str]] = set()
        for artifact in crawl(root, scope=scope):
            key = (artifact.kind, artifact.name)
            seen.add(key)

            # P2-11: content_hash is bundle_hash(files) server-side, never trusted
            # from the caller - so the hash that decides "unchanged" here has to
            # be computed the same way, over the same files that are about to be
            # sent, not crawl()'s own body. This is the one hash now, computed
            # once; there is no second one to keep in sync with it.
            local_hash = bundle_hash(artifact.files)
            if not force and remote_index.get(key) == local_hash:
                stats.skipped += 1
                continue

            chunks = chunk_markdown(artifact.body)
            if not chunks:
                stats.skipped += 1
                continue

            vectors = embedder.encode([c.text for c in chunks])
            payload = {
                "repo": artifact.repo,
                "path": artifact.path,
                "git_sha": _resolve_git_sha(root, artifact.path) if use_git else None,
                "version": artifact.version,
                "systems": artifact.systems,
                "description": artifact.description,
                "body": artifact.body,
                "chunks": [
                    {
                        "heading": c.heading,
                        "text": c.text,
                        "symptoms": c.symptoms,
                        "embedding": v.tolist(),
                    }
                    for c, v in zip(chunks, vectors, strict=True)
                ],
                "files": [
                    {"path": f.path, "content_b64": base64.b64encode(f.content).decode("ascii")}
                    for f in artifact.files
                ],
            }

            put_resp = client.put(
                f"/v1/artifacts/{artifact.kind}/{artifact.name}", json=payload, headers=auth_header
            )
            _raise_if_auth_error(put_resp)
            if put_resp.is_error:
                logger.warning(
                    "failed to publish %s/%s: %s %s",
                    artifact.kind,
                    artifact.name,
                    put_resp.status_code,
                    put_resp.text,
                )
                stats.failed += 1
                continue

            if key in remote_index:
                stats.updated += 1
            else:
                stats.added += 1

        missing = set(remote_index) - seen
        if prune:
            for kind, name in sorted(missing):
                del_resp = client.delete(f"/v1/artifacts/{kind}/{name}", headers=auth_header)
                _raise_if_auth_error(del_resp)
                del_resp.raise_for_status()
                stats.pruned += 1
        else:
            for kind, name in sorted(missing):
                print(f"remote has {kind}/{name}, not found locally (use --prune to remove)")
    finally:
        if owns_client:
            client.close()

    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="headjack publish", description="Chunk and embed a corpus locally, push it to a remote"
    )
    parser.add_argument("root", type=Path, help="corpus root directory to crawl")
    parser.add_argument(
        "--scope",
        default="personal",
        choices=["personal", "team", "org"],
        help="visibility scope recorded on every published artifact (default: personal)",
    )
    parser.add_argument(
        "--git", action="store_true", help="resolve git_sha per file from the local checkout"
    )
    parser.add_argument(
        "--force", action="store_true", help="re-upload every artifact regardless of content hash"
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="delete remote artifacts not found locally (default: report only)",
    )
    args = parser.parse_args(argv)

    config = cliconfig.load()
    remote = cliconfig.resolve_remote(None)
    token = config.get("token")
    if token is None:
        parser.error("no token configured — run `headjack init --token <TOKEN>` first")

    try:
        stats = publish(
            args.root,
            remote,
            token,
            scope=args.scope,
            use_git=args.git,
            force=args.force,
            prune=args.prune,
        )
    except PublishAuthError as exc:
        print(f"authentication failed: {exc}")
        return 1

    print(
        f"added {stats.added}, updated {stats.updated}, skipped {stats.skipped}, "
        f"failed {stats.failed}, pruned {stats.pruned}"
    )
    return 1 if stats.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
