"""skill_save: write one artifact back into Headjack from inside a live session —
the other half of the search loop. The agent notices a runbook is wrong, or just
solved something no artifact covered, and the fix lands in the catalog before the
thought is gone. Same publish mechanics `headjack publish`/`migrate` use — a single
`PUT /v1/artifacts/{kind}/{name}` — scoped to one call, reachable without leaving
the session.

Never silently overwrites. A `(kind, name)` that already has content on the remote
must be superseded explicitly, by the hash of what it is replacing. A hash mismatch
— including "there is no `supersedes_hash` at all, but something is already
there" — is an error, never a silent clobber (CLAUDE.md: "Hash mismatch is an
error. Never a warning, never a log line, never a retry.").

Needs `tank[dense]`: a saved artifact must carry a real embedding to be findable by
`skill_search` later, and the server never embeds on a caller's
behalf. Unlike `skill_search` (which degrades to sparse-only without an embedder),
there is no degraded path for a write that would otherwise produce an unfindable
artifact — `skill_save` refuses outright rather than save something silently
broken.

Every message this returns names Tank, never the function (`P3-09`: "the product
does the saving"; `skill_save` is an implementation detail and must not leak into
prose a human reads).
"""

from __future__ import annotations

import getpass
from typing import Any

from tank.client import HeadjackClient, HeadjackUnavailable


def _auth_error(status_code: int) -> str | None:
    """401 (bad/missing token) and 403 (`can_publish` is false) are different
    problems and must read differently — a scope problem is not the same fix as a
    bad token."""
    if status_code == 401:
        return "Headjack rejected the configured token (401)."
    if status_code == 403:
        return "Tank can't save: this token isn't allowed to publish to Headjack (403)."
    return None


def skill_save(
    client: HeadjackClient,
    name: str,
    kind: str,
    body: str,
    supersedes_hash: str | None = None,
    *,
    description: str = "",
    embedder: Any | None = None,
) -> str:
    """Publish one artifact. Returns a plain-text result meant to be shown to the
    user verbatim — never raises; every failure mode (no embedder, a conflicting
    hash, an unreachable Headjack, a rejected token) becomes a clear message
    instead of an exception reaching the caller."""
    if embedder is None:
        return (
            "Tank can't save: this needs the tank[dense] extra installed, so the "
            "saved artifact carries a real embedding and search can actually find "
            "it later. Ask whoever runs this Tank to install tank[dense]."
        )

    # Mirrors the server's own reject (`PUT /v1/artifacts`) - caught here first so
    # the agent gets a clear, immediate message instead of a round trip that ends
    # in a 422.
    if kind == "skill" and not description.strip():
        return (
            "Tank can't save that: a skill needs a non-empty description — it is "
            "what Tier 0 and skill_probe match against, not the body. Draft one "
            "and pass it as description."
        )

    # Lazy, not module-level: importing tank.server (which imports this module)
    # must never pull headjack into a default, dependency-free Tank install. By
    # the time execution reaches here, embedder is not None, which itself only
    # ever happens when tank[dense] — and therefore headjack — is installed.
    from headjack.chunker import chunk_markdown
    from headjack.corpus import DEFAULT_SKILL_VERSION

    chunks = chunk_markdown(body)
    if not chunks:
        return "Tank can't save that: the body produced no chunks — is it empty?"

    try:
        list_resp = client.get("/v1/artifacts")
    except HeadjackUnavailable as exc:
        return f"Tank can't save: {exc}"
    auth_error = _auth_error(list_resp.status_code)
    if auth_error is not None:
        return auth_error
    if list_resp.is_error:
        return f"Headjack returned an error ({list_resp.status_code}) listing artifacts."

    existing_hash = next(
        (
            row["content_hash"]
            for row in list_resp.json()
            if row["kind"] == kind and row["name"] == name
        ),
        None,
    )
    if existing_hash is not None and existing_hash != supersedes_hash:
        yours = f", yours was {supersedes_hash[:12]}…" if supersedes_hash else ""
        return (
            f"Tank won't save over {name}: Headjack already has it at "
            f"{existing_hash[:12]}…{yours}, which doesn't match supersedes_hash — "
            f"pass the current hash to confirm you mean to replace it, or save under "
            f"a different name."
        )

    vectors = embedder.encode([c.text for c in chunks])
    payload: dict[str, Any] = {
        "repo": None,
        "path": None,
        "git_sha": None,
        "version": DEFAULT_SKILL_VERSION if kind == "skill" else None,
        "systems": [],
        "description": description,
        "body": body,
        "chunks": [
            {
                "heading": c.heading,
                "text": c.text,
                "symptoms": c.symptoms,
                "embedding": v.tolist(),
            }
            for c, v in zip(chunks, vectors, strict=True)
        ],
    }

    try:
        put_resp = client.put(f"/v1/artifacts/{kind}/{name}", json=payload)
    except HeadjackUnavailable as exc:
        return f"Tank can't save: {exc}"
    auth_error = _auth_error(put_resp.status_code)
    if auth_error is not None:
        return auth_error
    if put_resp.is_error:
        return f"Headjack returned an error ({put_resp.status_code}): {put_resp.text}"

    new_hash = put_resp.json().get("content_hash", "")
    quote = f"{getpass.getuser()}, you know {kind} {name} now."
    if existing_hash is not None:
        return f"Tank updated {name} on Headjack · {new_hash[:12]} · visible to you now.\n{quote}"
    return f"Tank saved {name} to Headjack · {new_hash[:12]} · visible to you now.\n{quote}"
