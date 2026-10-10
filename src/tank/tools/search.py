"""skill_search: Tier 1 — one round trip, 3-5 candidates, gone from context after the
turn. The response is compact text, not JSON: the agent reads this, and JSON spends
tokens on punctuation the agent never needed.

Headjack never embeds a query. By default this sends `query_vector:
null` and gets sparse-only results — genuinely usable, since BM25 is what scores the
exact identifiers an incident actually contains. `tank[dense]` is the opt-in path
that embeds locally and sends a real vector for better recall on paraphrases.

Every snippet is a body excerpt and is fenced (`P4-05`) before it reaches this text —
`/v1/search` doesn't resolve or verify a content hash for a preview result, so the
fence says so honestly (`content_hash=None`) rather than fabricating one.
"""

from __future__ import annotations

from typing import Any

from tank.client import HeadjackClient, HeadjackUnavailable
from tank.config import TankConfig
from tank.fencing import fence

_MAX_RESPONSE_CHARS = 1200
_SEARCH_K = 5


def resolve_embedder() -> Any | None:
    """A lazy, optional import — the only place in Tank's default code path that
    even attempts it. `ImportError` is the expected, silent, common case when
    `tank[dense]` isn't installed; this must never raise for that reason. Called
    once when the server is built, not per search, so a missing dependency isn't
    re-discovered on every call.
    """
    try:
        from headjack.embed import Embedder

        return Embedder()
    except ImportError:
        return None


def _installed_skills(config: TankConfig) -> list[str]:
    """What's active in the skills root right now, for the degradation fallback.

    Deliberately not a `headjack` import: the default `tank` install must stay
    dependency-free (`P3-01`), so this is Tank's own minimal, local-only listing,
    not a reuse of `headjack.catalog`'s crawler.
    """
    if not config.skills_root.exists():
        return []
    return sorted(p.parent.name for p in config.skills_root.glob("*/SKILL.md"))


def skill_search(
    client: HeadjackClient,
    config: TankConfig,
    query: str,
    scope: str | None = None,
    *,
    embedder: Any | None = None,
) -> str:
    """Search the catalog. Returns compact, agent-facing text — never raises for a
    reachability or auth problem; every such case becomes a tool result describing
    the degradation (spec §5's offline requirement).
    """
    query_vector = embedder.encode_one(query).tolist() if embedder is not None else None
    body = {"query": query, "query_vector": query_vector, "k": _SEARCH_K, "scope": scope}

    try:
        resp = client.post("/v1/search", json=body)
    except HeadjackUnavailable:
        return _degraded_response(config)

    if resp.status_code == 401:
        return "Headjack rejected the configured token (401 unauthorized). Check TANK_TOKEN."
    if resp.status_code == 403:
        return "Headjack rejected this request (403 forbidden)."
    if resp.status_code >= 400:
        return f"Headjack returned an error ({resp.status_code}); search is unavailable right now."

    results = resp.json().get("results", [])
    return _cap(_render(query, results))


def _degraded_response(config: TankConfig) -> str:
    installed = _installed_skills(config)
    if installed:
        return (
            "Headjack is unreachable, so search is unavailable right now. "
            f"Already installed and usable: {', '.join(installed)}."
        )
    return (
        "Headjack is unreachable, so search is unavailable right now, and nothing is installed yet."
    )


def _render(query: str, results: list[dict[str, Any]]) -> str:
    if not results:
        return f'No results for "{query}". If you just solved this, `/tank save` it.'

    # Ranked, not scored: the served pipeline orders by fused rank, so position 1
    # is "closest of these", never "covers your topic". Saying so is the honest
    # version of a confidence signal the API cannot currently provide (#192).
    lines = [
        f'{len(results)} results for "{query}" — ranked by closeness, not scored '
        "for coverage; read before concluding one covers your topic",
        "",
    ]
    for i, r in enumerate(results, start=1):
        if r["installable"]:
            version = f" v{r['version']}" if r.get("version") else ""
            review = "reviewed" if r["reviewed"] else "unreviewed"
            lines.append(f"{i}. {r['name']} (skill{version} · installable · {review})")
        else:
            lines.append(f"{i}. {r['name']} ({r['kind']} · reference only)")
        if r.get("matched_in"):
            # The heading of the chunk that matched. A hit inside one section of a
            # larger skill is the "overlaps in part" case — distinguishable from a
            # skill that is *about* the topic only if the result says which it is.
            # Some headings already carry the § marker, some don't; normalise so
            # the line never reads "§ § Replacing a dead node".
            heading = str(r["matched_in"]).lstrip("§").strip()
            lines.append(f"   matched in § {heading} — one section, not the whole skill")
        if r.get("snippet"):
            # A snippet is a body excerpt, not Tank's own text, and gets the same
            # fencing treatment as a full installed body (P4-05) - the server's
            # SNIPPET_MAX_CHARS cap already bounds the raw text before fencing
            # adds its own small, constant overhead on top.
            fenced_snippet = fence(r["snippet"], name=r["name"], content_hash=r.get("content_hash"))
            lines.extend(f"   {line}" for line in fenced_snippet.split("\n"))
        if r["installable"]:
            lines.append(f'   → skill_install("{r["name"]}")')
        else:
            lines.append(f"   {r['source']}")
        lines.append("")

    return "\n".join(lines).rstrip()


def _cap(text: str) -> str:
    """Tier 1's budget is ~600 tokens and this is the only place it can be
    exceeded — a hard cap, not a suggestion.

    Each fenced snippet (`P4-05`) adds a small, bounded amount of header/footer
    text on top of its already-capped raw content — a deliberate, documented
    trade-off, not an oversight: the cap's purpose (bounding response size) still
    holds within a small constant factor, and fencing is explicitly not allowed to
    strip or shrink the text it wraps to compensate.
    """
    if len(text) <= _MAX_RESPONSE_CHARS:
        return text
    return text[: _MAX_RESPONSE_CHARS - 1].rstrip() + "…"
