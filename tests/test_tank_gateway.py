"""Tests for tank.gateway's scoped-down slice of P3-10: the /tank skill file
itself, not the telemetry/manifest/eviction integration that's deferred."""

from __future__ import annotations

from pathlib import Path

from tank.gateway import install_gateway, install_workflow_skill


def test_writes_skill_md_under_a_tank_subdirectory(tmp_path: Path) -> None:
    path = install_gateway(tmp_path)
    assert path == tmp_path / "tank" / "SKILL.md"
    assert path.exists()


def test_frontmatter_disables_model_invocation(tmp_path: Path) -> None:
    path = install_gateway(tmp_path)
    body = path.read_text()
    assert body.startswith("---\n")
    assert "name: tank" in body
    assert "disable-model-invocation: true" in body


def test_body_instructs_forwarding_to_skill_search(tmp_path: Path) -> None:
    body = install_gateway(tmp_path).read_text()
    assert "skill_search" in body


def test_installing_twice_leaves_exactly_one_file(tmp_path: Path) -> None:
    install_gateway(tmp_path)
    install_gateway(tmp_path)
    matches = list((tmp_path / "tank").glob("*"))
    assert matches == [tmp_path / "tank" / "SKILL.md"]


def test_installing_removes_the_old_headjack_named_skill(tmp_path: Path) -> None:
    old_dir = tmp_path / "headjack"
    old_dir.mkdir()
    (old_dir / "SKILL.md").write_text("old content under the wrong name")

    path = install_gateway(tmp_path)

    assert not old_dir.exists()
    assert path == tmp_path / "tank" / "SKILL.md"


def test_installing_is_a_no_op_when_no_old_headjack_skill_exists(tmp_path: Path) -> None:
    # Must not raise just because there is nothing to clean up.
    path = install_gateway(tmp_path)
    assert path.exists()


# ------------------------------------------------------------------- the save verb


def test_body_documents_the_save_verb(tmp_path: Path) -> None:
    body = install_gateway(tmp_path).read_text()
    assert "/tank save" in body
    assert "supersedes_hash" in body


def test_no_user_visible_string_names_the_mcp_tool_id(tmp_path: Path) -> None:
    # P3-09: the product that does the saving is Tank, never the function —
    # `skill_save` must not leak into anything a human reads.
    body = install_gateway(tmp_path).read_text()
    assert "skill_save" not in body


# --------------------------------------------------------------- the workflow skill


def test_workflow_skill_writes_under_its_own_subdirectory(tmp_path: Path) -> None:
    path = install_workflow_skill(tmp_path)
    assert path == tmp_path / "tank-workflow" / "SKILL.md"
    assert path.exists()


def test_workflow_skill_has_no_disable_model_invocation(tmp_path: Path) -> None:
    # Unlike the gateway, this one is meant to be read by the model on its own.
    body = install_workflow_skill(tmp_path).read_text()
    assert "name: tank-workflow" in body
    assert "disable-model-invocation" not in body


def test_workflow_skill_covers_both_asking_and_giving_back(tmp_path: Path) -> None:
    body = install_workflow_skill(tmp_path).read_text()
    assert "skill_probe" in body
    assert "skill_search" in body
    assert "supersedes_hash" in body


def _prose(path: Path) -> str:
    """The skill body as one lowercase line — the source is hard-wrapped, so a
    phrase assertion would otherwise fail on wherever the line happens to break."""
    return " ".join(path.read_text().lower().split())


def test_workflow_skill_scopes_the_gate_to_the_user_not_a_subject_area(tmp_path: Path) -> None:
    # The catalog holds whatever this user has accumulated, on any topic, so the
    # skill must not read as being about one kind of work — "operational" on its
    # own reliably does, and a model that reads it that way skips the check on
    # everything that doesn't feel like infra.
    body = _prose(install_workflow_skill(tmp_path))
    assert "whatever the topic" in body
    assert "every domain they work in" in body
    # Nor may that be fixed by listing the domains it also covers: an enumeration
    # is just a longer fence, and the next task is always off the end of it.
    assert "whatever the task is" in body


def test_workflow_skill_asks_the_user_before_going_solo(tmp_path: Path) -> None:
    # An empty catalog means "ask", not "improvise quietly" — the user's
    # specialist or convention often exists without ever having been written down.
    body = _prose(install_workflow_skill(tmp_path))
    assert "tank has nothing for this" in body
    assert "specialist" in body


def test_workflow_skill_installing_twice_leaves_exactly_one_file(tmp_path: Path) -> None:
    install_workflow_skill(tmp_path)
    install_workflow_skill(tmp_path)
    matches = list((tmp_path / "tank-workflow").glob("*"))
    assert matches == [tmp_path / "tank-workflow" / "SKILL.md"]
