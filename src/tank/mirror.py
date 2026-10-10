"""Tank's local catalog mirror: a second SQLite file recording what *exists* in
Headjack, separate from the manifest (P3-05, not yet built) which records what is
*installed*. Answers "is there anything near this?" locally and lexically, in
sub-millisecond time, instead of making `skill_search`'s remote, semantic, reranked
round trip for every curiosity.

Metadata only — no bodies. A mirror holding bodies would be a second store with its
own staleness and its own hash problem, and that's the store's job (P3-04), not
this one's. Lexical only — no embeddings, keeping tank's default install
dependency-free (P3-01); if a probe needs semantic matching, it's a search and
belongs in `tank.tools.search`.

Edges are a deliberately scoped-down slice of a fuller relationship graph: computed
purely from artifacts sharing a `systems` tag, since that data already exists in
every published skill's frontmatter today and costs nothing new to compute or
author. An explicit `related:`/`see-also:` frontmatter field is a natural
follow-up, not required for this to be useful.

Sync is pull-with-TTL, never push and never a daemon: `GET /v1/artifacts`,
transactional full replace, `fetched_at` recorded. An unreachable Headjack serves
the existing (possibly stale) mirror as-is — this is the one place degradation is
correct, never an error.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Any

from tank.client import HeadjackClient, HeadjackUnavailable
from tank.config import TankConfig

_SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,
    name         TEXT NOT NULL,
    version      TEXT,
    description  TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    UNIQUE (kind, name)
);
CREATE VIRTUAL TABLE IF NOT EXISTS catalog_fts USING fts5(
    name, description, content='catalog', content_rowid='id', tokenize='porter unicode61'
);
CREATE TABLE IF NOT EXISTS edges (
    from_kind TEXT NOT NULL,
    from_name TEXT NOT NULL,
    to_kind   TEXT NOT NULL,
    to_name   TEXT NOT NULL,
    reason    TEXT NOT NULL,
    PRIMARY KEY (from_kind, from_name, to_kind, to_name, reason)
);
CREATE TABLE IF NOT EXISTS mirror_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def fetched_at(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT value FROM mirror_meta WHERE key = 'fetched_at'").fetchone()
    return datetime.fromisoformat(row["value"]) if row else None


def age_str(conn: sqlite3.Connection) -> str:
    """Rendered age for every probe answer — a zero result must carry this loudly;
    see `tank.tools.probe`. Never silently omitted, because a stale negative that
    looks like a clean answer is worse than no answer at all."""
    when = fetched_at(conn)
    if when is None:
        return "never synced"
    minutes = (datetime.now(UTC) - when).total_seconds() / 60
    if minutes < 60:
        return f"{max(1, int(minutes))}m old"
    return f"{minutes / 60:.0f}h old"


def is_stale(conn: sqlite3.Connection, ttl_hours: float) -> bool:
    when = fetched_at(conn)
    if when is None:
        return True
    return (datetime.now(UTC) - when).total_seconds() / 3600 >= ttl_hours


def total_count(conn: sqlite3.Connection) -> int:
    (count,) = conn.execute("SELECT COUNT(*) FROM catalog").fetchone()
    return int(count)


def _version_key(version: str | None) -> tuple[int, ...]:
    """Sort key for a version string, lenient about everything that isn't a
    number: `2.2.0` beats `1.0.0`, `1.2.0-rc1` beats `1.1.0`, and an absent or
    unparseable version sorts lowest rather than raising."""
    if not version:
        return ()
    parts: list[int] = []
    for piece in str(version).split("."):
        digits = ""
        for ch in piece:
            if not ch.isdigit():
                break
            digits += ch
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def latest_per_name(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse the catalog to one artifact per `(kind, name)`. Headjack may hold
    several versions of the same artifact and return a row for each; the mirror is
    keyed by name and only answers "does something near this exist", so it wants
    the current one. Highest version wins, `updated_at` breaks ties — deliberately
    not "last row in the response", so that re-importing an *older* version later
    cannot displace the newer one."""
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for a in artifacts:
        key = (a["kind"], a["name"])
        rank = (_version_key(a.get("version")), a.get("updated_at") or "")
        incumbent = best.get(key)
        if incumbent is None:
            best[key] = a
            continue
        if rank > (_version_key(incumbent.get("version")), incumbent.get("updated_at") or ""):
            best[key] = a
    return list(best.values())


def _compute_edges(artifacts: list[dict[str, Any]]) -> list[tuple[str, str, str, str, str]]:
    """Every pair of artifacts sharing a `systems` tag gets a bidirectional edge,
    tagged with which system they share. O(n^2) within each system's member list,
    which is fine — a system's artifact count is small, not the whole catalog."""
    by_system: dict[str, list[tuple[str, str]]] = {}
    for a in artifacts:
        for system in a.get("systems") or []:
            by_system.setdefault(system, []).append((a["kind"], a["name"]))

    edges: set[tuple[str, str, str, str, str]] = set()
    for system, members in by_system.items():
        reason = f"shared-system:{system}"
        for (k1, n1), (k2, n2) in combinations(sorted(set(members)), 2):
            edges.add((k1, n1, k2, n2, reason))
            edges.add((k2, n2, k1, n1, reason))
    return sorted(edges)


def sync(client: HeadjackClient, config: TankConfig, *, force: bool = False) -> bool:
    """Pull `GET /v1/artifacts` and replace the mirror's catalog and edges
    transactionally. Returns whether a sync actually happened — `False` means "not
    stale yet", "Headjack unreachable", or "the catalog could not be applied", all
    of which leave the existing mirror untouched rather than raising.

    Nothing in here may propagate: a refresh is a side errand of whatever tool
    happened to run first, and a probe asking a purely local question must not die
    because the catalog on the far end was surprising. The caller distinguishes a
    failed refresh from a skipped one by the mirror still being stale afterwards —
    see `tank.tools.probe`."""
    conn = connect(config.mirror_path)
    try:
        if not force and not is_stale(conn, config.mirror_ttl_hours):
            return False
        try:
            resp = client.get("/v1/artifacts")
        except HeadjackUnavailable:
            return False
        if resp.is_error:
            return False

        try:
            artifacts = latest_per_name(resp.json())
            edges = _compute_edges(artifacts)
            with conn:
                conn.execute("DELETE FROM catalog")
                conn.execute("DELETE FROM edges")
                for a in artifacts:
                    conn.execute(
                        "INSERT INTO catalog (kind, name, version, description, content_hash) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            a["kind"],
                            a["name"],
                            a.get("version"),
                            a.get("description") or "",
                            a["content_hash"],
                        ),
                    )
                # catalog_fts is external-content; 'rebuild' resyncs it from catalog's
                # current rows in one step rather than tracking each rowid by hand.
                conn.execute("INSERT INTO catalog_fts(catalog_fts) VALUES ('rebuild')")
                for edge in edges:
                    conn.execute("INSERT OR IGNORE INTO edges VALUES (?, ?, ?, ?, ?)", edge)
                conn.execute(
                    "INSERT OR REPLACE INTO mirror_meta (key, value) VALUES ('fetched_at', ?)",
                    (datetime.now(UTC).isoformat(),),
                )
        except (ValueError, TypeError, KeyError, sqlite3.Error):
            # A body that isn't the catalog shape, or a write sqlite refuses: the
            # `with conn` block has already rolled back, so the previous mirror is
            # intact and still answerable. Staying stale is the degraded mode this
            # module is built around; taking the tool call down with us is not.
            return False
        return True
    finally:
        conn.close()


def search(conn: sqlite3.Connection, probe: str) -> list[sqlite3.Row]:
    """Match `probe` as one literal phrase — never parsed as an FTS5 query. No
    operators, no wildcards, no query language; those are a search's job."""
    phrase = '"' + probe.replace('"', '""') + '"'
    return conn.execute(
        "SELECT c.kind, c.name FROM catalog_fts "
        "JOIN catalog c ON c.id = catalog_fts.rowid "
        "WHERE catalog_fts MATCH ? ORDER BY rank",
        (phrase,),
    ).fetchall()


def related(conn: sqlite3.Connection, kind: str, name: str, limit: int) -> list[str]:
    rows = conn.execute(
        "SELECT to_name FROM edges WHERE from_kind = ? AND from_name = ? LIMIT ?",
        (kind, name, limit),
    ).fetchall()
    return [r["to_name"] for r in rows]
