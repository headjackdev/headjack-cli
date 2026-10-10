"""Tank's MCP server: the shell the six tools live in.

`skill_search` (`P3-02`), `skill_probe` (`P3-12`, a scoped-down local-mirror
slice), `skill_save` (`P3-09`), `skill_install` (`P3-04`) and
`skill_list_installed` (`P3-05`) are implemented. The remaining tool is a stub
until its own issue lands: `skill_evict` (`P3-06`). That stub still exercises the
real HTTP client against `/healthz`, so the degradation-wrapping contract (a dead
Headjack becomes a tool result, never a traceback) is genuinely tested now rather
than assumed until that issue lands. `skill_list_installed` itself never touches
the client at all — it is local-only, and must work with Headjack down.

IMPORTANT — registration setting, not something this file can set on itself: whoever
registers this server with Claude Code or the Agent SDK must mark it `alwaysLoad:
true`. Tool Search is on by default in both and withholds MCP tool definitions from
the context window; under `auto` it activates at 10% of the context and counts every
MCP tool not marked `alwaysLoad`. Without that flag, Tank's own gateway can be
deferred out of context before the agent ever gets the chance to call it — the
trigger fails silently and nothing looks broken. See
`docs/tank-mcp-config.example.json` for where this actually lives.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from tank.client import HeadjackClient, HeadjackUnavailable
from tank.config import TankConfig
from tank.tools.install import skill_install as _skill_install
from tank.tools.list import skill_list_installed as _skill_list_installed
from tank.tools.probe import skill_probe as _skill_probe
from tank.tools.save import skill_save as _skill_save
from tank.tools.search import resolve_embedder
from tank.tools.search import skill_search as _skill_search

_TOOL_DESCRIPTIONS = {
    "skill_search": (
        "Search the team skill catalog. Always invoke when the task matches no "
        "loaded skill, or skill_probe signaled something nearby. Include exact "
        "error strings, identifiers, metric names — not a paraphrase."
    ),
    "skill_probe": (
        "Always invoke before starting a task, whatever its topic — the catalog "
        "is the user's own skills, procedures and conventions across everything "
        "they work on, not one domain. Cheap: local, no network round trip. Pass "
        "the exact terms this task needs — identifiers, error strings, system and "
        "domain names, not a paraphrase. A hit means escalate to skill_search. A "
        "0 proves only that that literal phrase is absent from a lexical index "
        "with no paraphrase matching — it is not evidence the catalog lacks the "
        "topic, and it never licenses skipping the search before a save."
    ),
    "skill_save": (
        "Write one artifact back to the catalog — a corrected runbook, or "
        "something you just solved that nothing covered. Before creating a NEW "
        "skill: have you run skill_search for each core topic it would cover, and "
        "read the hits themselves rather than their titles, to rule out overlap "
        "with what is already there? A probe returning 0 does not answer that. "
        "This writes for real, "
        "immediately, for everyone who searches this catalog after you: draft "
        "name/kind/body and show the user exactly what will be saved before "
        "calling this, never call it unconfirmed. A skill (unlike a runbook) needs "
        "a non-empty description — it is what Tier 0 and skill_probe match "
        "against. To update an existing artifact, pass its current content_hash "
        "as supersedes_hash; a mismatch is refused, never silently overwritten."
    ),
    "skill_install": (
        "Verify, fetch and activate a skill by name. Live immediately, invocable "
        "as the name this tool returns — never the bare name you passed in."
    ),
    "skill_list_installed": "List what is cached, which version, when fetched, last used.",
    "skill_evict": "Deactivate a cached skill, or every stale one.",
}


def build_server(config: TankConfig) -> MCPServer:
    """Wire up the client and register the six tools. Never contacts Headjack at
    build time — the store directory is created locally, and every unimplemented
    tool is a stub (or, for the health probe below, a real but minimal call) until
    its own issue lands."""
    config.store.mkdir(parents=True, exist_ok=True)
    config.store.chmod(0o700)

    server = MCPServer(name="tank")
    client = HeadjackClient(config)
    embedder = resolve_embedder()  # attempted once at startup, never per search

    @server.tool(description=_TOOL_DESCRIPTIONS["skill_search"])
    def skill_search(query: str, scope: str | None = None) -> str:
        return _skill_search(client, config, query, scope, embedder=embedder)

    @server.tool(description=_TOOL_DESCRIPTIONS["skill_probe"])
    def skill_probe(probes: list[str], limit: int = 3) -> str:
        return _skill_probe(client, config, probes, limit)

    @server.tool(description=_TOOL_DESCRIPTIONS["skill_save"])
    def skill_save(
        name: str,
        kind: str,
        body: str,
        supersedes_hash: str | None = None,
        description: str = "",
    ) -> str:
        return _skill_save(
            client, name, kind, body, supersedes_hash, description=description, embedder=embedder
        )

    @server.tool(description=_TOOL_DESCRIPTIONS["skill_install"])
    def skill_install(name: str, version: str = "latest") -> str:
        return _skill_install(client, config, name, version)

    @server.tool(description=_TOOL_DESCRIPTIONS["skill_list_installed"])
    def skill_list_installed() -> str:
        return _skill_list_installed(config)

    @server.tool(description=_TOOL_DESCRIPTIONS["skill_evict"])
    def skill_evict(name: str | None = None, stale: bool = False) -> str:
        return _stub("skill_evict", "P3-06", client)

    return server


def _stub(tool_name: str, issue: str, client: HeadjackClient) -> str:
    """Every stub still makes one real call, so "a hung catalog must not hang the
    agent's turn" is exercised now, not merely asserted in a docstring. This is the
    one place that call lives; P3-02/04/05/06 replace it with their real logic."""
    try:
        client.get("/healthz")
        connectivity = "Headjack is reachable"
    except HeadjackUnavailable as exc:
        connectivity = f"Headjack is not reachable ({exc})"

    return f"{tool_name}: not implemented yet (see {issue}). {connectivity}."
