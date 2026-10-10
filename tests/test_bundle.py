"""Tests for the canonical multi-file bundle hash (`P2-10`): `headjack.bundle`.

Every test here maps directly to an acceptance criterion on the issue — the
canonicalisation rules (bytewise path sort, length-prefixed fields, raw-byte
hashing, strict path rejection) are each load-bearing on their own, not just
implementation detail.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import unicodedata
from pathlib import Path

import pytest

from headjack.bundle import BundleFile, bundle_hash

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------- ordering


def test_reordering_input_does_not_change_the_hash() -> None:
    a = BundleFile("SKILL.md", b"---\nname: x\n---\nbody")
    b = BundleFile("scripts/check.sh", b"#!/bin/sh\necho ok\n")
    assert bundle_hash([a, b]) == bundle_hash([b, a])


def test_renaming_a_file_changes_the_hash() -> None:
    original = bundle_hash([BundleFile("SKILL.md", b"content")])
    renamed = bundle_hash([BundleFile("README.md", b"content")])
    assert original != renamed


# -------------------------------------------------------------- length prefixes


def test_length_prefixes_prevent_the_classic_concatenation_collision() -> None:
    """Without length prefixes, `path + content` for ("a", "bc") and ("ab", "c")
    both concatenate to "abc" — a renamed byte moved from content into the path
    would silently produce the same hash. The prefixes must prevent exactly this."""
    first = bundle_hash([BundleFile("a", b"bc")])
    second = bundle_hash([BundleFile("ab", b"c")])
    assert first != second


# ---------------------------------------------------------------- one-file bundle


def test_one_file_bundle_hash_differs_from_plain_sha256() -> None:
    content = b"hello, this is a one-file skill"
    assert bundle_hash([BundleFile("x.md", content)]) != hashlib.sha256(content).hexdigest()


# ------------------------------------------------------------------- round trip


def test_bundle_is_stable_across_base64_and_json_round_trip() -> None:
    files = [
        BundleFile("SKILL.md", b"---\nname: x\n---\nbody"),
        BundleFile("scripts/check.sh", b"#!/bin/sh\necho ok\n"),
    ]
    original_hash = bundle_hash(files)

    wire = json.dumps(
        [
            {"path": f.path, "content_b64": base64.b64encode(f.content).decode("ascii")}
            for f in files
        ]
    )
    decoded = json.loads(wire)
    restored = [BundleFile(item["path"], base64.b64decode(item["content_b64"])) for item in decoded]

    assert bundle_hash(restored) == original_hash


# ------------------------------------------------------------------ CRLF vs LF


def test_crlf_and_lf_content_produce_different_hashes() -> None:
    """Intentional, not an oversight: raw bytes are hashed verbatim, with no line
    ending normalisation — the same rule signing applies to the body it signs."""
    lf = bundle_hash([BundleFile("x.md", b"line one\nline two\n")])
    crlf = bundle_hash([BundleFile("x.md", b"line one\r\nline two\r\n")])
    assert lf != crlf


# ----------------------------------------------------------------- empty bundle


def test_empty_file_set_raises() -> None:
    with pytest.raises(ValueError, match="at least one file"):
        bundle_hash([])


# --------------------------------------------------------------- path rejection


def test_absolute_path_raises() -> None:
    with pytest.raises(ValueError, match="absolute"):
        bundle_hash([BundleFile("/etc/passwd", b"x")])


def test_dotdot_anywhere_raises() -> None:
    with pytest.raises(ValueError, match="\\.\\."):
        bundle_hash([BundleFile("scripts/../../etc/passwd", b"x")])


def test_backslash_raises() -> None:
    with pytest.raises(ValueError, match="POSIX"):
        bundle_hash([BundleFile("scripts\\check.sh", b"x")])


def test_leading_dot_slash_raises() -> None:
    with pytest.raises(ValueError, match="\\./"):
        bundle_hash([BundleFile("./SKILL.md", b"x")])


def test_empty_path_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        bundle_hash([BundleFile("", b"x")])


def test_duplicate_path_raises() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        bundle_hash([BundleFile("SKILL.md", b"a"), BundleFile("SKILL.md", b"b")])


def test_non_nfc_normalized_path_raises() -> None:
    decomposed = unicodedata.normalize("NFD", "café.md")
    assert decomposed != unicodedata.normalize("NFC", decomposed)  # sanity: actually decomposed
    with pytest.raises(ValueError, match="NFC"):
        bundle_hash([BundleFile(decomposed, b"x")])


# -------------------------------------------------------------------- isolation


def test_module_imports_nothing_from_headjack_beyond_itself() -> None:
    """`bundle_hash` must not reach into `tank.safe` or any other `headjack`
    module — it is a pure hashing function, not a filesystem one."""
    tree = ast.parse((REPO_ROOT / "src" / "headjack" / "bundle.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("headjack"), f"unexpected import: {node.module}"
            assert not node.module.startswith("tank"), f"unexpected import: {node.module}"
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("headjack"), f"unexpected import: {alias.name}"
                assert not alias.name.startswith("tank"), f"unexpected import: {alias.name}"
