"""Canonical content hash over a multi-file skill bundle (P2-10).

Agent Skills defines a skill as a directory, not a single file — `SKILL.md` plus
optional `scripts/`, `references/` and `assets/`. `bundle_hash` is this project's
tree SHA: the one hash every machine that sees the same bundle agrees on, the way
`gh skill` records a git tree SHA rather than an author-written version string.

Every part of the canonicalisation is load-bearing. This module does not know what a skill is: no
`SKILL.md` requirement, no frontmatter, no filesystem access. Bytes in, a string out.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

# P2-11/#96: a catalog is not a binary artifact store. One module constant each,
# shared by the PUT /v1/artifacts caps (413) and the local crawler (a hard error
# at crawl time, never a silent drop) - both must agree, or a bundle that passes
# `headjack publish` locally could still be refused by the server.
MAX_BUNDLE_FILES = 64
MAX_FILE_BYTES = 256 * 1024
MAX_BUNDLE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class BundleFile:
    path: str  # POSIX, relative to the skill root: "SKILL.md", "scripts/check.sh"
    content: bytes  # the exact bytes, never a re-serialised form


def bundle_hash(files: Iterable[BundleFile]) -> str:
    """sha256 over a whole skill directory. Canonical: two machines agree.

    Files are sorted bytewise by their UTF-8 path before hashing, so input order
    never affects the result. Both `path` and `content` are length-prefixed (8-byte
    big-endian) before their bytes — without that, concatenation is ambiguous and a
    bundle can be given a colliding hash by moving a byte from one file's content
    into another file's path. Raises `ValueError` for anything that isn't an
    unambiguous, safe relative path, or an empty file set.
    """
    file_list = list(files)
    if not file_list:
        raise ValueError("a bundle must contain at least one file")

    seen_paths: set[str] = set()
    for file in file_list:
        _reject_if_unsafe(file.path)
        if file.path in seen_paths:
            raise ValueError(f"duplicate path in bundle: {file.path!r}")
        seen_paths.add(file.path)

    ordered = sorted(file_list, key=lambda f: f.path.encode("utf-8"))

    digest = hashlib.sha256()
    for file in ordered:
        path_bytes = file.path.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(file.content).to_bytes(8, "big"))
        digest.update(file.content)
    return digest.hexdigest()


def _reject_if_unsafe(path: str) -> None:
    """A plain `ValueError`, not `tank.safe.UnsafePathError` — this is a hashing
    function, not a filesystem one, and must not import `safe.py`."""
    if not path:
        raise ValueError("bundle file path must not be empty")
    if path.startswith("/"):
        raise ValueError(f"bundle file path must be relative, got absolute: {path!r}")
    if "\\" in path:
        raise ValueError(f"bundle file path must use POSIX separators: {path!r}")
    if path.startswith("./"):
        raise ValueError(f"bundle file path must not have a leading './': {path!r}")
    if not unicodedata.is_normalized("NFC", path):
        raise ValueError(f"bundle file path must be NFC-normalized: {path!r}")

    segments = path.split("/")
    if "" in segments:
        raise ValueError(f"bundle file path must not contain empty segments: {path!r}")
    if ".." in segments:
        raise ValueError(f"bundle file path must not contain '..': {path!r}")
