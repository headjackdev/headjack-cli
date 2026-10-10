"""Tests for tank.safe.safe_write — adversarial before anything calls it for real."""

from __future__ import annotations

import inspect
import random
import string
from pathlib import Path

import pytest

from tank.safe import UnsafePathError, safe_write

REJECTION_CASES = [
    ("/etc/passwd", "absolute"),
    ("../../.ssh/authorized_keys", "traversal"),
    ("a/../../b", "traversal after a legal segment"),
    ("....//x", "survives naive stripping"),
    ("%2e%2e/x", "URL-encoded traversal"),
    ("..%2fx", "mixed encoding"),
    ("a/./././../b", "normalises out of base"),
    (r"C:\Windows\x", "Windows-style"),
    (r"\\server\share", "UNC"),
    ("", "empty"),
    (".", "degenerate"),
    ("..", "degenerate"),
    ("name\x00withnull", "null byte"),
    ("x" * 300, "300-character component"),
]


@pytest.fixture
def base(tmp_path: Path) -> Path:
    b = tmp_path / "base"
    b.mkdir()
    return b


@pytest.mark.parametrize("rel_path,reason", REJECTION_CASES, ids=[r for _, r in REJECTION_CASES])
def test_rejects(base: Path, rel_path: str, reason: str) -> None:
    with pytest.raises(UnsafePathError):
        safe_write(base, rel_path, b"content")


def test_symlink_inside_base_pointing_outside_is_rejected(base: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (base / "sym").symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafePathError):
        safe_write(base, "sym/file.txt", b"content")


def test_symlink_leaf_pointing_outside_is_rejected(base: Path, tmp_path: Path) -> None:
    outside_target = tmp_path / "outside-file.txt"
    outside_target.write_text("preexisting")
    (base / "evil-link").symlink_to(outside_target)

    with pytest.raises(UnsafePathError):
        safe_write(base, "evil-link", b"overwrite attempt")


def test_legitimate_nested_path_is_written(base: Path) -> None:
    path = safe_write(base, "scripts/check.sh", b"#!/bin/sh\necho hi\n")
    assert path == base / "scripts" / "check.sh"
    assert path.read_bytes() == b"#!/bin/sh\necho hi\n"


def test_containment_uses_is_relative_to_not_string_prefix(tmp_path: Path) -> None:
    base = tmp_path / "foo"
    base.mkdir()
    evil_sibling = tmp_path / "foo-evil"
    evil_sibling.mkdir()
    # A naive string-prefix check ("/tmp/foo-evil".startswith("/tmp/foo")) would
    # wrongly treat the sibling as contained. is_relative_to must not.
    (evil_sibling / "payload.txt").write_text("should never be reachable from base")

    # There's no rel_path that legitimately points at a sibling directory through
    # base — this test instead confirms resolution is correct by checking a path
    # that escapes base by exactly the sibling's name structurally.
    with pytest.raises(UnsafePathError):
        safe_write(base, "../foo-evil/payload.txt", b"x")


def test_written_file_is_mode_0600(base: Path) -> None:
    path = safe_write(base, "a.txt", b"x")
    assert (path.stat().st_mode & 0o777) == 0o600


def test_created_directories_are_mode_0700(base: Path) -> None:
    safe_write(base, "a/b/c.txt", b"x")
    assert ((base / "a").stat().st_mode & 0o777) == 0o700
    assert ((base / "a" / "b").stat().st_mode & 0o777) == 0o700


def test_no_sanitising_code_path_exists() -> None:
    """The module must not contain a code path that strips or rewrites a suspicious
    component instead of rejecting it outright — check the actual code (not prose
    that merely discusses sanitising, which the module's own docstring does) for
    common sanitisation idioms."""
    import ast

    import tank.safe as safe_module

    tree = ast.parse(inspect.getsource(safe_module))
    forbidden_call_names = {
        "replace",
        "strip",
        "lstrip",
        "rstrip",
        "normpath",
        "sanitize",
        "sanitise",
    }
    found = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in forbidden_call_names
    ]
    assert not found, f"found call(s) that look like sanitising in tank/safe.py: {found}"


# ---- property test: random path strings never escape base ---------------------------


def _random_path_string(rng: random.Random) -> str:
    pieces = [
        "..",
        ".",
        "",
        "/",
        "\\",
        "%2e",
        "etc",
        "passwd",
        "a" * rng.randint(1, 400),
        "\x00",
        "sym",
        "..%2f",
        string.ascii_letters[: rng.randint(0, 10)],
    ]
    count = rng.randint(1, 6)
    return "/".join(rng.choice(pieces) for _ in range(count))


def test_property_random_paths_never_escape_base(base: Path) -> None:
    rng = random.Random(0)  # fixed seed: deterministic, reproducible failures
    base_resolved = base.resolve()

    for _i in range(500):
        candidate = _random_path_string(rng)
        try:
            written = safe_write(base, candidate, b"x")
        except UnsafePathError:
            continue
        except OSError:
            # a legitimate-looking but filesystem-invalid name (e.g. empty
            # trailing component) — not an escape, just not writable; not this
            # property's concern.
            continue
        assert written.resolve().is_relative_to(base_resolved), (
            f"escaped base for input {candidate!r} -> {written}"
        )
