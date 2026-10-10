"""Tests for tank.manifest (manifest read/reconcile) and tank.tools.list
(skill_list_installed), P3-05.

No mocked SQLite or HTTP here — these are pure local-filesystem concerns, so
fixtures build real store entries directly through `tank.store`'s own public
primitives (the same ones `skill_install`, P3-04, uses) rather than hand-writing
`manifest.json` by hand wherever a real pipeline would do.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from tank import manifest as tank_manifest
from tank import store as tank_store
from tank.config import TankConfig
from tank.tools.list import skill_list_installed

_FORBIDDEN_TOOL_NAMES = {
    "skill_search",
    "skill_probe",
    "skill_save",
    "skill_install",
    "skill_list_installed",
    "skill_evict",
}


@pytest.fixture
def config(tmp_path: Path) -> TankConfig:
    return TankConfig(
        token="test-token",
        store=tmp_path / "store",
        skills_root=tmp_path / "skills",
    )


def _store_entry(
    config: TankConfig,
    name: str,
    *,
    version: str = "1.0",
    scope: str = "personal",
    signature_state: str = "unsigned",
    content_hash: str = "deadbeef",
    body: str = "skill body",
    active: bool,
) -> str:
    """Write one skill into the store and manifest, exactly the way
    `skill_install`'s `_activate_and_record` does, and activate it when
    requested. Returns the activation name."""
    tank_store.write_to_store(
        config.store,
        name,
        version,
        body,
        content_hash=content_hash,
        scope=scope,
        signature_state=signature_state,
    )
    act_name = tank_store.activation_name(scope, name)
    if active:
        tank_store.activate(config.skills_root, body, act_name)
    tank_store.record_manifest_entry(
        config.store,
        name,
        version=version,
        content_hash=content_hash,
        scope=scope,
        signature_state=signature_state,
        active=active,
    )
    return act_name


# ---- empty cache --------------------------------------------------------------------


def test_empty_cache_returns_short_clear_message(config: TankConfig) -> None:
    result = skill_list_installed(config)
    assert result == "Tank's cache is empty — nothing installed yet."


def test_empty_cache_entries_load_as_empty_list(config: TankConfig) -> None:
    entries, warning = tank_manifest.load(config)
    assert entries == []
    assert warning is None


# ---- store and active set distinguished ----------------------------------------------


def test_store_and_active_set_distinguished_in_output(config: TankConfig) -> None:
    _store_entry(config, "active-skill", active=True)
    _store_entry(config, "inactive-skill", active=False)

    result = skill_list_installed(config)

    assert "2 cached, 1 active" in result
    active_line = next(line for line in result.splitlines() if "active-skill" in line)
    inactive_line = next(line for line in result.splitlines() if "inactive-skill" in line)
    assert "active" in active_line and "inactive" not in active_line
    assert "inactive" in inactive_line


def test_active_entry_prints_literal_invocation_string(config: TankConfig) -> None:
    act_name = _store_entry(config, "k8ssandra-node-replace", scope="team", active=True)
    assert act_name == "tank-team-k8ssandra-node-replace"

    result = skill_list_installed(config)

    assert f"/{act_name}" in result


def test_reactivation_hint_shown_for_inactive_entries(config: TankConfig) -> None:
    _store_entry(config, "argocd-sync-stuck", active=False)

    result = skill_list_installed(config)

    assert "/tank install argocd-sync-stuck to reactivate" in result


def test_unsigned_entries_are_visibly_flagged(config: TankConfig) -> None:
    _store_entry(config, "medusa-restore", signature_state="unsigned", active=False)

    result = skill_list_installed(config)

    assert "UNSIGNED" in result


def test_no_line_names_an_mcp_function(config: TankConfig) -> None:
    _store_entry(config, "active-skill", active=True)
    _store_entry(config, "inactive-skill", active=False)

    result = skill_list_installed(config)

    for line in result.splitlines():
        for forbidden in _FORBIDDEN_TOOL_NAMES:
            assert forbidden not in line, f"{forbidden!r} leaked into: {line!r}"


# ---- offline, by construction ---------------------------------------------------------


def test_skill_list_installed_takes_no_client_argument() -> None:
    # skill_list_installed must work with Headjack down (CLAUDE.md: "only search
    # may degrade"). Taking no HeadjackClient at all is what makes that structural
    # rather than something a future edit could accidentally break.
    params = inspect.signature(skill_list_installed).parameters
    assert list(params) == ["config"]


def test_skill_list_installed_works_with_unreachable_headjack_url(tmp_path: Path) -> None:
    config = TankConfig(
        token="t",
        headjack_url="http://127.0.0.1:1",  # nothing listens here
        store=tmp_path / "store",
        skills_root=tmp_path / "skills",
        timeout_s=0.1,
    )
    _store_entry(config, "offline-skill", active=True)
    result = skill_list_installed(config)
    assert "offline-skill" in result


# ---- atomic writes --------------------------------------------------------------------


def test_manifest_write_is_atomic_on_simulated_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "manifest.json"
    original = {"version": 1, "entries": {"good": {"active": True}}}
    path.write_text(json.dumps(original))

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated crash between tmp write and replace")

    monkeypatch.setattr(tank_manifest.os, "replace", _boom)

    with pytest.raises(OSError):
        tank_manifest._atomic_write(path, {"version": 1, "entries": {}})

    # The crash happened after the temp file was written but before the replace
    # — the real manifest.json must be exactly what it was before.
    assert json.loads(path.read_text()) == original


# ---- corruption: rebuilt from the store, with a warning --------------------------------


def test_corrupt_manifest_is_rebuilt_from_store_with_warning(config: TankConfig) -> None:
    tank_store.write_to_store(
        config.store,
        "rebuilt-skill",
        "2.0",
        "body",
        content_hash="abc123",
        scope="personal",
        signature_state="unsigned",
    )
    manifest_path = config.store / "manifest.json"
    manifest_path.write_text("not valid json {{{")

    with pytest.warns(UserWarning, match="corrupt"):
        entries, warning = tank_manifest.load(config)

    assert warning is not None and "rebuilt" in warning
    assert len(entries) == 1
    assert entries[0].name == "rebuilt-skill"
    assert entries[0].version == "2.0"
    assert entries[0].content_hash == "abc123"
    # No usage history survives a rebuild — reported honestly, not fabricated.
    assert entries[0].last_used_at is None

    # The rebuild is persisted, so a second read is no longer corrupt.
    entries_again, warning_again = tank_manifest.load(config)
    assert warning_again is None
    assert len(entries_again) == 1


def test_rebuilt_entry_renders_as_never_used(config: TankConfig) -> None:
    tank_store.write_to_store(
        config.store,
        "rebuilt-skill",
        "2.0",
        "body",
        content_hash="abc123",
        scope="personal",
        signature_state="unsigned",
    )
    (config.store / "manifest.json").write_text("not valid json {{{")

    with pytest.warns(UserWarning):
        result = skill_list_installed(config)

    assert "never used" in result


def test_missing_manifest_file_is_treated_as_empty_not_corrupt(
    config: TankConfig, recwarn: pytest.WarningsRecorder
) -> None:
    # No manifest.json at all (a fresh store) is a normal empty state, not a
    # corruption to warn about.
    entries, warning = tank_manifest.load(config)
    assert entries == []
    assert warning is None
    assert len(recwarn) == 0


# ---- reconciliation: the store wins over a disagreeing manifest ------------------------


def test_stale_active_flag_is_reconciled_to_actual_disk_state(config: TankConfig) -> None:
    # record_manifest_entry says active=True, but activate() is never called —
    # simulating a manifest that drifted from reality (e.g. the activated file
    # was removed by hand).
    tank_store.write_to_store(
        config.store,
        "drifted-skill",
        "1.0",
        "body",
        content_hash="h1",
        scope="personal",
        signature_state="unsigned",
    )
    tank_store.record_manifest_entry(
        config.store,
        "drifted-skill",
        version="1.0",
        content_hash="h1",
        scope="personal",
        signature_state="unsigned",
        active=True,
    )

    entries, _ = tank_manifest.load(config)

    assert len(entries) == 1
    assert entries[0].active is False

    # The correction is persisted back to manifest.json, not just in-memory.
    on_disk = json.loads((config.store / "manifest.json").read_text())
    assert on_disk["entries"]["drifted-skill"]["active"] is False


def test_entry_with_no_store_copy_is_dropped(config: TankConfig) -> None:
    # The manifest references a name/version that was never (or no longer) in
    # the store — e.g. the store directory was wiped out from under it.
    tank_store.record_manifest_entry(
        config.store,
        "ghost-skill",
        version="1.0",
        content_hash="h1",
        scope="personal",
        signature_state="unsigned",
        active=True,
    )

    entries, _ = tank_manifest.load(config)

    assert entries == []
    on_disk = json.loads((config.store / "manifest.json").read_text())
    assert "ghost-skill" not in on_disk["entries"]
