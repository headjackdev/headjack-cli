"""skill_list_installed: what Tank is holding, locally, always (`P3-05`).

Purely local — this never touches Headjack, never accepts a `HeadjackClient`, and
must work with the catalog down; only *search* is allowed to degrade (CLAUDE.md).
All the data this needs — what's stored, what's active, when each was fetched and
last activated — lives in `manifest.json` (`tank.manifest`) and the skills root,
both read straight off disk.

This is the one place a human finds out how to invoke what Tank is holding
(`P3-09`'s rule): active entries print the literal `/tank-<scope>-<name>` string
`skill_install` (`P3-04`) actually activated, never an MCP function name.
"""

from __future__ import annotations

from datetime import UTC, datetime

from tank import manifest as tank_manifest
from tank.config import TankConfig

_EMPTY_CACHE_MESSAGE = "Tank's cache is empty — nothing installed yet."


def _relative_age(iso: str | None) -> str:
    """`"used 2h ago"`-style rendering, or `"never used"` for an entry a manifest
    rebuild couldn't honestly date (`tank.manifest._rebuild_from_store`)."""
    if not iso:
        return "never used"
    delta = datetime.now(UTC) - datetime.fromisoformat(iso)
    seconds = delta.total_seconds()
    if seconds < 3600:
        return f"used {max(1, int(seconds // 60))}m ago"
    if seconds < 86400:
        return f"used {int(seconds // 3600)}h ago"
    return f"used {int(seconds // 86400)}d ago"


def _render_entry(entry: tank_manifest.SkillEntry) -> str:
    marker = "●" if entry.active else "○"
    status = "active" if entry.active else "inactive"
    used = _relative_age(entry.last_used_at)
    signature = "UNSIGNED" if entry.signature_state == "unsigned" else entry.signature_state
    tail = f"/{entry.invocation}" if entry.active else f"→ /tank install {entry.name} to reactivate"
    return (
        f"{marker} {entry.name:<24} v{entry.version:<7} {status:<9} "
        f"{used:<14} {signature:<9} {tail}"
    )


def skill_list_installed(config: TankConfig) -> str:
    """List everything cached and everything active. Never raises, never makes a
    network call — the manifest and the skills root are both local, and reading
    them is all this needs."""
    entries, warning = tank_manifest.load(config)

    lines: list[str] = []
    if warning:
        lines.append(warning)

    if not entries:
        lines.append(_EMPTY_CACHE_MESSAGE)
        return "\n".join(lines)

    active_count = sum(1 for e in entries if e.active)
    lines.append(f"{len(entries)} cached, {active_count} active")
    lines.append("")
    lines.extend(_render_entry(e) for e in entries)
    return "\n".join(lines)
