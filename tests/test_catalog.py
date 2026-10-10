"""Tests for headjack.catalog — shared by scripts/measure_catalog.py (P0-03) and
the metadata-only baseline (P1-17)."""

from __future__ import annotations

from pathlib import Path

from headjack.catalog import (
    count_tokens,
    find_skills,
    load_tiktoken_encoding,
    parse_skill_frontmatter,
    render_catalog_entry,
)


def test_render_catalog_entry() -> None:
    assert render_catalog_entry("foo", "does a thing") == "foo: does a thing"


def test_find_skills_recurses_and_dedupes(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "SKILL.md").write_text("---\nname: a\n---\nbody")
    (tmp_path / "b" / "c").mkdir(parents=True)
    (tmp_path / "b" / "c" / "SKILL.md").write_text("---\nname: c\n---\nbody")

    found = find_skills([tmp_path, tmp_path])  # same root twice: must not duplicate
    assert len(found) == 2


def test_find_skills_missing_root_is_skipped(tmp_path: Path) -> None:
    assert find_skills([tmp_path / "does-not-exist"]) == []


def test_parse_frontmatter_simple_scalar(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text('---\nname: foo\ndescription: "a simple one-liner"\n---\nbody')
    meta = parse_skill_frontmatter(path)
    assert meta["name"] == "foo"
    assert meta["description"] == "a simple one-liner"


def test_parse_frontmatter_folded_scalar(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text(
        "---\nname: foo\ndescription: >\n  This description\n  spans multiple lines\n  "
        "and should be joined with spaces.\n---\nbody"
    )
    meta = parse_skill_frontmatter(path)
    assert meta["description"] == (
        "This description spans multiple lines and should be joined with spaces."
    )


def test_parse_frontmatter_literal_scalar(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text("---\nname: foo\ndescription: |\n  line one\n  line two\n---\nbody")
    meta = parse_skill_frontmatter(path)
    assert meta["description"] == "line one\nline two"


def test_parse_frontmatter_bare_key_with_indented_continuation(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text("---\nname: foo\ndescription:\n  continued on the next line\n---\nbody")
    meta = parse_skill_frontmatter(path)
    assert meta["description"] == "continued on the next line"


def test_parse_frontmatter_no_frontmatter_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text("just a body, no frontmatter at all")
    assert parse_skill_frontmatter(path) == {}


def test_parse_frontmatter_missing_file_returns_empty(tmp_path: Path) -> None:
    assert parse_skill_frontmatter(tmp_path / "does-not-exist.md") == {}


def test_count_tokens_approximate_fallback() -> None:
    count, is_exact = count_tokens("a" * 40, encoding=None)
    assert is_exact is False
    assert count == 10  # 40 / 4


def test_count_tokens_approximate_never_zero_for_nonempty_text() -> None:
    count, _is_exact = count_tokens("x", encoding=None)
    assert count >= 1


def test_load_tiktoken_encoding_never_raises() -> None:
    # Whether tiktoken is importable/reachable varies by environment (this repo's
    # sandbox blocks the network fetch its encoding needs) — the only contract that
    # matters here is that this never raises, regardless of which way it resolves.
    result = load_tiktoken_encoding()
    assert result is None or hasattr(result, "encode")
