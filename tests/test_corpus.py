"""Tests for headjack.corpus.

The straightforward classification cases live as a small static tree in
tests/fixtures/corpus/ (checked in, easy to read). The awkward edge cases — an
oversized file, a non-UTF-8 file, the skip-directories, a symlink loop — are
generated into tmp_path instead, so the repo doesn't carry a committed 1 MB blob or a
binary fixture just to exercise a skip rule.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from headjack.bundle import MAX_BUNDLE_FILES
from headjack.corpus import Artifact, crawl

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "corpus"


def _by_name(artifacts: list[Artifact], name: str) -> Artifact:
    matches = [a for a in artifacts if a.name == name]
    assert len(matches) == 1, f"expected exactly one artifact named {name!r}, got {matches}"
    return matches[0]


def test_fixture_tree_classification() -> None:
    artifacts = list(crawl(FIXTURE_ROOT))
    by_kind = {a.kind for a in artifacts}
    assert by_kind == {"skill", "runbook", "script"}


def test_skill_name_from_frontmatter() -> None:
    artifacts = list(crawl(FIXTURE_ROOT))
    skill = _by_name(artifacts, "cassandra-operator")
    assert skill.kind == "skill"
    assert skill.systems == ["cassandra", "k8ssandra"]
    assert (
        skill.description
        == "Diagnose and recover a Cassandra node that fails to rejoin the cluster."
    )


def test_skill_name_falls_back_to_directory_when_frontmatter_has_none() -> None:
    artifacts = list(crawl(FIXTURE_ROOT))
    skill = _by_name(artifacts, "no-frontmatter-name")
    assert skill.kind == "skill"
    assert skill.systems == []
    assert skill.description == ""


def test_non_skill_artifacts_have_empty_description() -> None:
    artifacts = list(crawl(FIXTURE_ROOT))
    non_skills = [a for a in artifacts if a.kind != "skill"]
    assert non_skills
    assert all(a.description == "" for a in non_skills)


def test_skill_without_a_declared_version_defaults_to_one_zero_zero() -> None:
    artifacts = list(crawl(FIXTURE_ROOT))
    # Neither fixture skill declares `version:` in its frontmatter.
    assert _by_name(artifacts, "cassandra-operator").version == "1.0.0"
    assert _by_name(artifacts, "no-frontmatter-name").version == "1.0.0"


def test_skill_version_from_frontmatter_is_honored(tmp_path: Path) -> None:
    skill_dir = tmp_path / "versioned"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: versioned\nversion: 2.3.1\n---\n\nBody text.\n")
    artifacts = list(crawl(tmp_path))
    assert _by_name(artifacts, "versioned").version == "2.3.1"


def test_non_skill_artifacts_have_no_version() -> None:
    artifacts = list(crawl(FIXTURE_ROOT))
    non_skills = [a for a in artifacts if a.kind != "skill"]
    assert non_skills
    assert all(a.version is None for a in non_skills)


def test_runbook_under_runbooks_subdirectory() -> None:
    artifacts = list(crawl(FIXTURE_ROOT))
    runbook = _by_name(artifacts, "cassandra-operations")
    assert runbook.kind == "runbook"
    assert "§B" in runbook.body


def test_runbook_directly_under_docs() -> None:
    artifacts = list(crawl(FIXTURE_ROOT))
    doc = _by_name(artifacts, "plain-doc")
    assert doc.kind == "runbook"


def test_script_under_scripts_dir_gets_only_its_header() -> None:
    artifacts = list(crawl(FIXTURE_ROOT))
    script = _by_name(artifacts, "restart-node")
    assert script.kind == "script"
    assert "kubectl delete pod" not in script.body  # body, not header
    assert "full cluster rebalance" in script.body  # header, kept


def test_script_under_bin_dir_with_shebang_and_docstring() -> None:
    artifacts = list(crawl(FIXTURE_ROOT))
    script = _by_name(artifacts, "migrate")
    assert script.kind == "script"
    assert "stock_event" in script.body
    assert "sys.exit(0)" not in script.body


def test_script_with_header_under_40_chars_is_skipped() -> None:
    artifacts = list(crawl(FIXTURE_ROOT))
    names = [a.name for a in artifacts]
    assert "short-header" not in names


def test_python_file_outside_scripts_or_bin_is_not_indexed() -> None:
    artifacts = list(crawl(FIXTURE_ROOT))
    names = [a.name for a in artifacts]
    assert "not_a_script" not in names


def test_content_hash_stable_and_sensitive_to_change(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "x"
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text("---\nname: x\n---\n\nversion one body text.\n")

    first = next(iter(crawl(tmp_path))).content_hash
    second = next(iter(crawl(tmp_path))).content_hash
    assert first == second

    skill_path.write_text("---\nname: x\n---\n\nversion two body text, one byte different.\n")
    third = next(iter(crawl(tmp_path))).content_hash
    assert third != first


def test_skip_dot_git_node_modules_venv_pycache(tmp_path: Path) -> None:
    for skip_dir in (".git", "node_modules", ".venv", "__pycache__"):
        d = tmp_path / skip_dir / "runbooks"
        d.mkdir(parents=True)
        (d / "should-not-appear.md").write_text("content that would otherwise classify fine")

    # one real artifact, so we know the crawl actually ran and isn't just empty
    real = tmp_path / "docs" / "runbooks"
    real.mkdir(parents=True)
    (real / "real.md").write_text("the only artifact that should survive this crawl")

    artifacts = list(crawl(tmp_path))
    assert [a.name for a in artifacts] == ["real"]


def test_file_over_1mb_is_skipped(tmp_path: Path) -> None:
    d = tmp_path / "docs" / "runbooks"
    d.mkdir(parents=True)
    (d / "huge.md").write_text("x" * (1_000_001))
    (d / "normal.md").write_text("a normal sized runbook file")

    artifacts = list(crawl(tmp_path))
    assert [a.name for a in artifacts] == ["normal"]


def test_non_utf8_file_is_skipped_not_raised(tmp_path: Path) -> None:
    d = tmp_path / "docs" / "runbooks"
    d.mkdir(parents=True)
    (d / "binary.md").write_bytes(b"\xff\xfe\x00\x01not valid utf-8 at all \xff")
    (d / "normal.md").write_text("a normal sized runbook file")

    artifacts = list(crawl(tmp_path))  # must not raise
    assert [a.name for a in artifacts] == ["normal"]


def test_scripts_with_same_stem_in_different_skills_get_disambiguated_names(
    tmp_path: Path,
) -> None:
    for skill in ("docx", "pptx"):
        d = tmp_path / skill / "scripts"
        d.mkdir(parents=True)
        (d / "base.py").write_text("# a shared base module header long enough to keep\n")

    artifacts = list(crawl(tmp_path))
    names = [a.name for a in artifacts]
    assert len(names) == len(set(names)), f"collided names: {names}"
    assert "docx-scripts-base" in names
    assert "pptx-scripts-base" in names


def test_non_colliding_script_name_is_left_as_the_bare_stem(tmp_path: Path) -> None:
    d = tmp_path / "only-skill" / "scripts"
    d.mkdir(parents=True)
    (d / "unique.py").write_text("# a header long enough to clear the 40 char minimum\n")

    artifacts = list(crawl(tmp_path))
    assert [a.name for a in artifacts] == ["unique"]


def test_runbooks_with_same_stem_in_different_skills_get_disambiguated_names(
    tmp_path: Path,
) -> None:
    for skill in ("alpha", "beta"):
        d = tmp_path / skill / "runbooks"
        d.mkdir(parents=True)
        (d / "readme.md").write_text("a runbook body")

    artifacts = list(crawl(tmp_path))
    names = [a.name for a in artifacts]
    assert len(names) == len(set(names)), f"collided names: {names}"
    assert "alpha-runbooks-readme" in names
    assert "beta-runbooks-readme" in names


def test_symlink_loop_does_not_hang_the_crawl(tmp_path: Path) -> None:
    d = tmp_path / "docs" / "runbooks"
    d.mkdir(parents=True)
    (d / "real.md").write_text("the artifact that should survive the loop")

    loop = d / "loop-back-to-root"
    try:
        os.symlink(tmp_path, loop, target_is_directory=True)
    except OSError:
        return  # platform/sandbox without symlink support; nothing to assert here

    artifacts = list(crawl(tmp_path))  # must terminate
    assert "real" in [a.name for a in artifacts]


def test_scope_is_set_on_every_artifact() -> None:
    artifacts = list(crawl(FIXTURE_ROOT, scope="team"))
    assert artifacts
    assert all(a.scope == "team" for a in artifacts)


def test_metadata_fields_not_set_by_local_crawl() -> None:
    artifacts = list(crawl(FIXTURE_ROOT))
    for a in artifacts:
        assert a.repo is None
        assert a.git_sha is None
        assert a.reviewed_by is None


def test_folded_block_scalar_description_is_reassembled_not_truncated(tmp_path: Path) -> None:
    """Regression test: a `description: >-` folded block scalar previously
    published as the literal string "`>-`" (the bare YAML indicator) instead of
    its real, multi-line text — found live against a real catalog entry whose
    actual description (recovered from a backup) was a full k8ssandra runbook
    summary. `headjack.catalog.parse_frontmatter_text` reassembles it correctly;
    `corpus.py` must actually be calling that function, not a private duplicate."""
    skill_dir = tmp_path / "k8s-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: k8s-skill\n"
        "description: >-\n"
        "  Operate the k8ssandra Cassandra clusters dbp05 and dbp42.\n"
        "  Use for ring or node problems, pods stuck at 2/3, or any\n"
        '  "Cassandra is slow" report on these clusters.\n'
        "---\n\n# body\n"
    )
    artifacts = list(crawl(tmp_path))
    skill = _by_name(artifacts, "k8s-skill")
    assert skill.description != ">-"
    assert skill.description == (
        "Operate the k8ssandra Cassandra clusters dbp05 and dbp42. "
        'Use for ring or node problems, pods stuck at 2/3, or any "Cassandra is '
        'slow" report on these clusters.'
    )


# ------------------------------------------------------------------- bundles (#177/P2-11)


def test_a_skill_directory_with_scripts_and_references_is_one_artifact(tmp_path: Path) -> None:
    """The exact bug #177 reported: a skill folder's own scripts/references must
    not become separate top-level artifacts."""
    skill_dir = tmp_path / "cassandra-operator"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: cassandra-operator\ndescription: d\n---\nbody")
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts" / "check.sh").write_text("#!/bin/sh\necho ok\n")
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "REF.md").write_text("reference material")

    artifacts = list(crawl(tmp_path))
    assert len(artifacts) == 1
    skill = artifacts[0]
    assert skill.kind == "skill"
    assert skill.name == "cassandra-operator"
    paths = {f.path for f in skill.files}
    assert paths == {"SKILL.md", "scripts/check.sh", "references/REF.md"}


def test_bundle_files_carry_exact_bytes_including_a_binary_asset(tmp_path: Path) -> None:
    skill_dir = tmp_path / "with-asset"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: with-asset\ndescription: d\n---\nbody")
    (skill_dir / "assets").mkdir()
    binary = bytes(range(256))
    (skill_dir / "assets" / "logo.png").write_bytes(binary)

    skill = next(iter(crawl(tmp_path)))
    asset = next(f for f in skill.files if f.path == "assets/logo.png")
    assert asset.content == binary


def test_bundle_content_hash_changes_when_a_script_changes(tmp_path: Path) -> None:
    skill_dir = tmp_path / "x"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: x\ndescription: d\n---\nbody")
    (skill_dir / "scripts").mkdir()
    script = skill_dir / "scripts" / "check.sh"
    script.write_text("echo v1\n")

    first = next(iter(crawl(tmp_path))).content_hash
    script.write_text("echo v2\n")
    second = next(iter(crawl(tmp_path))).content_hash
    assert first != second


def test_loose_runbook_and_script_are_still_one_file_bundles(tmp_path: Path) -> None:
    artifacts = list(crawl(FIXTURE_ROOT))
    runbook = _by_name(artifacts, "cassandra-operations")
    assert [f.path for f in runbook.files] == ["cassandra-operations.md"]
    script = _by_name(artifacts, "restart-node")
    assert [f.path for f in script.files] == ["restart-node.sh"]


def test_skill_bundle_over_the_file_count_cap_raises(tmp_path: Path) -> None:
    skill_dir = tmp_path / "huge"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: huge\ndescription: d\n---\nbody")
    (skill_dir / "scripts").mkdir()
    for i in range(MAX_BUNDLE_FILES):
        (skill_dir / "scripts" / f"s{i}.sh").write_text("echo hi\n")

    with pytest.raises(ValueError, match="file"):
        list(crawl(tmp_path))


def test_nested_skill_md_under_a_parent_skill_is_folded_into_the_parent_bundle(
    tmp_path: Path,
) -> None:
    """A SKILL.md two levels deep, inside the parent skill's own `references/`
    directory, is just another shipped file of the *outer* bundle - crawl()
    never treats it as a second, independent skill."""
    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / "SKILL.md").write_text("---\nname: outer\ndescription: d\n---\nbody")
    nested = outer / "references" / "example-of-another-skill"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("---\nname: should-not-appear\n---\nnested body")

    artifacts = list(crawl(tmp_path))
    assert len(artifacts) == 1
    assert artifacts[0].name == "outer"
    paths = {f.path for f in artifacts[0].files}
    assert "references/example-of-another-skill/SKILL.md" in paths
