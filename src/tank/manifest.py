"""tank.manifest: the read side of `manifest.json` — what `skill_install` (`P3-04`)
wrote, reconciled against the store and the skills root on every read (`P3-05`).

The store is the source of truth; the manifest is only ever an index over it
(`src/tank/store.py`'s own words, written when `P3-04` first touched
`manifest.json` and left this module's job undone on purpose). That means two
things here, not one:

- If `manifest.json` itself is unreadable — truncated, not JSON, missing the
  `entries` key — it is rebuilt from the `meta.json` sidecar every store entry
  already carries, with a warning, never a crash.
- Even when `manifest.json` parses fine, each entry's `active` flag is recomputed
  from whether its activation name actually exists under the skills root, and any
  entry whose store copy is gone is dropped. A manifest that disagrees with the
  store is wrong, by definition, and is corrected back to disk the moment it is
  read rather than left to keep disagreeing.

`last_used_at` is set only by `skill_install`'s activation path (`P3-04`) — Tank
cannot observe the harness actually invoking a skill, so this measures activation
recency, not usage. A rebuilt entry has no such history and is honestly reported
as "never used" rather than backdated to its `fetched_at`.
"""

from __future__ import annotations

import json
import os
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tank import store as tank_store
from tank.config import TankConfig

_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SkillEntry:
    """One manifest entry, already reconciled against the store and skills root —
    `active` here is always the real, on-disk state, never a stale flag."""

    name: str
    version: str
    content_hash: str
    scope: str
    signature_state: str
    fetched_at: str
    last_used_at: str | None
    active: bool

    @property
    def invocation(self) -> str:
        """The literal `tank-<scope>-<name>` string a human types to invoke this,
        matching what `P3-04` actually activated — never an MCP function name."""
        return tank_store.activation_name(self.scope, self.name)


def _manifest_path(store_root: Path) -> Path:
    return store_root / "manifest.json"


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """Temp file plus `os.replace`, matching every other write in this project —
    a crash between the two leaves the previous `manifest.json` exactly as it
    was, never a half-written file in its place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, path)


def _entry_to_dict(entry: SkillEntry) -> dict[str, Any]:
    return {
        "version": entry.version,
        "content_hash": entry.content_hash,
        "scope": entry.scope,
        "signature_state": entry.signature_state,
        "fetched_at": entry.fetched_at,
        "last_used_at": entry.last_used_at,
        "active": entry.active,
    }


def _rebuild_from_store(store_root: Path) -> dict[str, Any]:
    """Reconstruct a manifest from `meta.json` sidecars alone. The manifest keys
    entries by name only (`P3-04`'s `record_manifest_entry` does the same), so
    when a name has more than one version stored, the most recently fetched one
    wins — a rebuild has no better signal than that to pick the "current" one.
    `last_used_at` is left unset: that history lived only in the manifest that
    just got lost, and fabricating it from `fetched_at` would misreport it as
    used, when all that is actually known is that it was once fetched."""
    entries: dict[str, Any] = {}
    if not store_root.exists():
        return {"version": _SCHEMA_VERSION, "entries": entries}

    for name_dir in sorted(p for p in store_root.iterdir() if p.is_dir()):
        candidates: list[tuple[str, dict[str, Any]]] = []
        for version_dir in sorted(p for p in name_dir.iterdir() if p.is_dir()):
            meta_path = version_dir / "meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text())
            except ValueError:
                continue
            candidates.append((version_dir.name, meta))
        if not candidates:
            continue
        version, meta = max(candidates, key=lambda vm: vm[1].get("fetched_at", ""))
        entries[name_dir.name] = {
            "version": version,
            "content_hash": meta.get("content_hash", ""),
            "scope": meta.get("scope", "personal"),
            "signature_state": meta.get("signature_state", "unsigned"),
            "fetched_at": meta.get("fetched_at", ""),
            "last_used_at": None,
            "active": False,
        }
    return {"version": _SCHEMA_VERSION, "entries": entries}


def load(config: TankConfig) -> tuple[list[SkillEntry], str | None]:
    """Load manifest entries, reconciled against the store and skills root.

    Returns `(entries sorted by name, a warning string if manifest.json was
    corrupt and had to be rebuilt)`. Never raises — a bad manifest costs a
    warning, not Tank's ability to answer "what do I have"."""
    path = _manifest_path(config.store)
    warning: str | None = None

    if not path.exists():
        data: dict[str, Any] = {"version": _SCHEMA_VERSION, "entries": {}}
    else:
        try:
            data = json.loads(path.read_text())
            if not isinstance(data.get("entries"), dict):
                raise ValueError("manifest.json missing 'entries'")
        except ValueError:
            warning = "Tank's manifest was corrupt and has been rebuilt from the store."
            warnings.warn(warning, stacklevel=2)
            data = _rebuild_from_store(config.store)
            _atomic_write(path, data)

    entries: list[SkillEntry] = []
    changed = False
    for name, raw in data["entries"].items():
        version = raw.get("version", "")
        # The store is the source of truth: an entry whose store copy is gone is
        # dropped rather than shown as cached.
        if not tank_store.is_stored(config.store, name, version):
            changed = True
            continue

        scope = raw.get("scope", "personal")
        actual_active = tank_store.is_active(
            config.skills_root, tank_store.activation_name(scope, name)
        )
        if actual_active != bool(raw.get("active", False)):
            changed = True

        entries.append(
            SkillEntry(
                name=name,
                version=version,
                content_hash=raw.get("content_hash", ""),
                scope=scope,
                signature_state=raw.get("signature_state", "unsigned"),
                fetched_at=raw.get("fetched_at", ""),
                last_used_at=raw.get("last_used_at"),
                active=actual_active,
            )
        )

    entries.sort(key=lambda e: e.name)

    if changed:
        data = {
            "version": data.get("version", _SCHEMA_VERSION),
            "entries": {e.name: _entry_to_dict(e) for e in entries},
        }
        _atomic_write(path, data)

    return entries, warning
