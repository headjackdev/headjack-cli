"""skill_probe: many cheap local questions, one turn. The count is the payload —
it costs one or two tokens and answers the only question Tier 0 actually needs
answered, whether knowledge exists near the situation, without loading any of it.
Names and related artifacts are a convenience for a follow-up `skill_search`.
"""

from __future__ import annotations

from tank import mirror
from tank.client import HeadjackClient
from tank.config import TankConfig

_DEFAULT_LIMIT = 3

# A zero is the answer most likely to be acted on wrongly, so the next step is
# spelled out where it is read — in the result — rather than left to be recalled
# from a tool description the model saw once at startup. A real session probed
# three precise topics, got 0 on all three, and nearly published duplicates of
# two skills that a per-topic skill_search found immediately.
_ESCALATE = (
    "0 means this literal phrase is absent from a lexical index — "
    "no paraphrase matching, no semantics. It is not evidence the catalog "
    "lacks the topic."
)


def _escalation(empty: list[str]) -> list[str]:
    """The next step for probes that matched nothing. Named terms, not a general
    exhortation: a reader who has to reconstruct the search arguments usually
    doesn't."""
    if not empty:
        return []
    terms = ", ".join(f'"{p}"' for p in empty)
    return [
        "",
        _ESCALATE,
        f"Next: skill_search each of {terms} — required before saving anything "
        "that would cover these topics, and read the hits, not their titles.",
    ]


def skill_probe(
    client: HeadjackClient, config: TankConfig, probes: list[str], limit: int = _DEFAULT_LIMIT
) -> str:
    """One call, many probe strings. Never touches the network on the hot path
    beyond a lazy TTL-gated refresh — an unreachable Headjack serves the existing
    mirror with its age, which is never withheld, not even for a zero result."""
    if not probes:
        return "skill_probe needs at least one probe string."

    mirror.sync(client, config)  # lazy refresh if stale; no-op if not, never raises
    conn = mirror.connect(config.mirror_path)
    try:
        age = mirror.age_str(conn)
        # Still stale right after a refresh attempt means the refresh is failing,
        # not that it was skipped. Say so on every line that carries the age: a
        # mirror quietly frozen for days reads exactly like a healthy one, and a
        # zero answer from it looks like a clean negative.
        if mirror.is_stale(conn, config.mirror_ttl_hours):
            age += ", refresh failing"
        total = mirror.total_count(conn)
        lines = [f"mirror {age} · {total} artifacts"]

        empty: list[str] = []
        for p in probes:
            matches = mirror.search(conn, p)
            count = len(matches)
            if count == 0:
                lines.append(f"{p:<20} 0 (mirror {age})")
                empty.append(p)
                continue

            names = ", ".join(m["name"] for m in matches[:limit])
            lines.append(f"{p:<20} {count:<3} → {names}")

            top = matches[0]
            nearby = mirror.related(conn, top["kind"], top["name"], limit)
            if nearby:
                lines.append(f"{'':<20}     related to {top['name']}: {', '.join(nearby)}")

        lines.extend(_escalation(empty))
        return "\n".join(lines)
    finally:
        conn.close()
