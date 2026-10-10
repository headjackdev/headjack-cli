"""Tank's store: everything fetched from Headjack, by version, kept outside the
skills root (a store the harness can read reintroduces the catalog
floor this whole project removes).

Two directories, two jobs:

    Store       ~/.headjack/cache/<name>/<version>/SKILL.md   never harness-visible
    Active set  <skills-root>/tank-<scope>-<name>/SKILL.md    what Tank activated

Install = fetch -> verify -> write to store -> activate. Evict (`P3-06`, not yet
built) = deactivate only; the store copy stays, which is what makes eviction
reversible with Headjack unreachable — the bytes never left the machine.

Each store entry carries its own `meta.json` sidecar (content_hash, scope,
signature_state) so a version already on disk can be reactivated without the
network *or* the manifest — the manifest (`P3-05`, not yet built) is a derived
index over the store, same relationship store.py already has to it: "the store is
the source of truth; the manifest is an index over it" is that issue's own words,
and the sidecar is what makes that true rather than aspirational.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tank.safe import safe_write

_MANIFEST_SCHEMA_VERSION = 1


# --------------------------------------------------------------------------- store


def _entry_dir(store_root: Path, name: str, version: str) -> Path:
    return store_root / name / version


def is_stored(store_root: Path, name: str, version: str) -> bool:
    """Whether `name`/`version` is already in the store — what makes reinstall
    skip the network entirely. Deliberately does not accept `version="latest"`:
    "latest" cannot be resolved without asking Headjack what is actually newest."""
    return (_entry_dir(store_root, name, version) / "SKILL.md").exists()


def read_meta(store_root: Path, name: str, version: str) -> dict[str, Any]:
    return json.loads(  # type: ignore[no-any-return]
        (_entry_dir(store_root, name, version) / "meta.json").read_text()
    )


def read_body(store_root: Path, name: str, version: str) -> str:
    return (_entry_dir(store_root, name, version) / "SKILL.md").read_text()


def write_to_store(
    store_root: Path,
    name: str,
    version: str,
    body: str,
    *,
    content_hash: str,
    scope: str,
    signature_state: str,
) -> None:
    """Write a verified skill into the store, through `safe_write` (`P3-03`) for
    the same containment guarantee every other write in this project gets, even
    though `name`/`version` come from a database key and a semver string, not
    caller-controlled path segments — the boundary is what's trusted, not the
    caller upstream of it."""
    base = store_root / name
    safe_write(base, f"{version}/SKILL.md", body.encode())
    meta = {
        "content_hash": content_hash,
        "scope": scope,
        "signature_state": signature_state,
        "fetched_at": datetime.now(UTC).isoformat(),
    }
    safe_write(base, f"{version}/meta.json", json.dumps(meta, indent=2, sort_keys=True).encode())


# ------------------------------------------------------------------------ activate

_SCOPE_SEGMENT = {"personal": "user", "team": "team", "org": "org"}


def activation_name(scope: str, name: str) -> str:
    """`tank-<scope>-<name>` — the flat, hyphenated form `P0-01` proved the harness
    actually invokes. `personal` maps to the segment `user`, matching the table in
    `P3-04`; `team`/`org` map to themselves."""
    return f"tank-{_SCOPE_SEGMENT.get(scope, scope)}-{name}"


def activate(skills_root: Path, body: str, name: str) -> Path:
    """Materialise `body` at `skills_root/<name>/SKILL.md` — copy, not a symlink
    (`P0-01`'s provisional default: copy is the option that cannot fail silently).

    Atomic at the file level: a temp file in the same directory, then one
    `os.replace`. Deliberately not a directory-level atomic swap — a skill
    directory that already exists and already holds a `SKILL.md` is non-empty,
    and POSIX `rename()` refuses to replace a non-empty directory; replacing the
    one file inside it is both sufficient (bundles are out of scope until
    `P3-13`) and avoids that failure mode entirely. A reader that opens
    `SKILL.md` either gets the old complete file or the new complete file, never
    a partial one.
    """
    dest_dir = skills_root / name
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_file = dest_dir / "SKILL.md"
    tmp_file = dest_dir / f".SKILL.md.tmp-{os.getpid()}"
    tmp_file.write_text(body)
    os.replace(tmp_file, dest_file)
    return dest_file


def is_active(skills_root: Path, name: str) -> bool:
    return (skills_root / name / "SKILL.md").exists()


# -------------------------------------------------------------------------- manifest
# Minimal, atomic primitives only — `P3-05` owns `skill_list_installed` and the
# rebuild-from-store-on-corruption recovery path. This exists because `P3-04`'s own
# acceptance criteria require the manifest to record what was just activated, and
# `P3-04`'s scope does not include `manifest.py`; writing the documented schema
# directly, here, is narrower than building that module early.


def _manifest_path(store_root: Path) -> Path:
    return store_root / "manifest.json"


def _load_manifest(store_root: Path) -> dict[str, Any]:
    path = _manifest_path(store_root)
    if not path.exists():
        return {"version": _MANIFEST_SCHEMA_VERSION, "entries": {}}
    try:
        data: dict[str, Any] = json.loads(path.read_text())
    except ValueError:
        # A real rebuild-from-store is P3-05's job; an empty manifest is the
        # minimal safe fallback here — never a crash over metadata.
        return {"version": _MANIFEST_SCHEMA_VERSION, "entries": {}}
    return data


def active_count(store_root: Path) -> int:
    manifest = _load_manifest(store_root)
    return sum(1 for e in manifest["entries"].values() if e.get("active"))


def record_manifest_entry(
    store_root: Path,
    name: str,
    *,
    version: str,
    content_hash: str,
    scope: str,
    signature_state: str,
    active: bool,
) -> None:
    """Atomic write: temp file plus `os.replace`, matching every other write in
    this project — a corrupted manifest costs the whole cache's metadata."""
    manifest = _load_manifest(store_root)
    manifest["entries"][name] = {
        "version": version,
        "content_hash": content_hash,
        "scope": scope,
        "signature_state": signature_state,
        "fetched_at": datetime.now(UTC).isoformat(),
        "last_used_at": datetime.now(UTC).isoformat(),
        "active": active,
    }
    path = _manifest_path(store_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    os.replace(tmp, path)
