"""Write content under a base directory, with path containment that rejects
anything suspicious rather than trying to sanitise it clean.

Stripping `../` turns `....//` into `../` and is how these bugs get written.
Everything here either passes cleanly or raises `UnsafePathError` — there is no code
path that modifies a suspicious input and proceeds with it anyway.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

# Conservative; most filesystems cap a single component around 255 bytes.
_MAX_COMPONENT_LEN = 255


class UnsafePathError(ValueError):
    """Raised for any `rel_path` that could write outside `base`. The caller must
    supply a clean path — there is no recovery path, only rejection."""


def safe_write(base: Path, rel_path: str, content: bytes) -> Path:
    """Write `content` to `base / rel_path`. Raises `UnsafePathError` for anything
    that could escape `base` — an absolute path, a traversal, a symlink pointing
    outside `base`, or anything else this doesn't recognise as unambiguously safe.

    Only creates directories under `base`. Files land at mode 0600, directories at
    0700 — this is Tank's own private cache, not a shared tree.
    """
    _reject_if_unsafe(rel_path)

    base_resolved = base.resolve()
    # Resolve the full joined path (not just rel_path) so a symlink anywhere in
    # rel_path's ancestry — including the leaf itself — that points outside base
    # is caught: resolve() follows every symlink it finds, existing or not.
    target = (base_resolved / rel_path).resolve()

    if not target.is_relative_to(base_resolved):
        # is_relative_to, never a string prefix check — a prefix check would let
        # "/tmp/foo-evil" pass against a base of "/tmp/foo".
        raise UnsafePathError(f"{rel_path!r} resolves outside {base}")
    if target == base_resolved:
        # A path is "relative to itself", so the check above alone lets rel_path
        # values that normalise away to nothing (".", ".//", "a/..") through as
        # "safe" — but there is no file to write here, only base itself. Besides
        # being meaningless, target.parent in this case is base's *parent*, one
        # level outside base — walking "between base and itself" from there in
        # _dirs_between never reaches base and loops upward to the filesystem
        # root forever, since Path("/").parent == Path("/"). Reject before that.
        raise UnsafePathError(f"{rel_path!r} resolves to base itself, not a file under it")

    target.parent.mkdir(parents=True, exist_ok=True)
    for directory in _dirs_between(base_resolved, target.parent):
        directory.chmod(0o700)

    target.write_bytes(content)
    target.chmod(0o600)
    return target


def _reject_if_unsafe(rel_path: str) -> None:
    if "\x00" in rel_path:
        raise UnsafePathError("null byte in path")
    if not rel_path or rel_path in (".", ".."):
        raise UnsafePathError(f"degenerate path: {rel_path!r}")
    if "\\" in rel_path:
        raise UnsafePathError(f"backslash (Windows-style path) rejected: {rel_path!r}")
    if rel_path.startswith("/"):
        raise UnsafePathError(f"absolute path rejected: {rel_path!r}")
    if re.match(r"^[A-Za-z]:", rel_path):
        raise UnsafePathError(f"drive-letter path rejected: {rel_path!r}")

    # Check both the raw string and a URL-decoded form — "%2e%2e" or "..%2f" are
    # not real traversal unless something decodes them, but a lot of things do
    # decode a path somewhere in its lifetime, so both forms are rejected.
    for candidate in (rel_path, unquote(rel_path)):
        for segment in candidate.split("/"):
            # Any run of two or more dots, not just an exact "..": "....//x"
            # contains the substring "../" once (at index 2), so a naive sanitiser
            # doing a single-pass `.replace("../", "")` elsewhere in a pipeline
            # would strip it and leave a fresh "../" behind. Reject the whole
            # dot-run class rather than trust that nothing downstream ever does that.
            if len(segment) >= 2 and set(segment) == {"."}:
                raise UnsafePathError(f"dot-run segment in path: {rel_path!r}")
            if len(segment) > _MAX_COMPONENT_LEN:
                raise UnsafePathError(
                    f"path component exceeds {_MAX_COMPONENT_LEN} chars: {rel_path!r}"
                )


def _dirs_between(base: Path, leaf: Path) -> list[Path]:
    """`leaf` and every directory between it and `base`, inclusive of `leaf`,
    exclusive of `base`. Caller has already verified `leaf` is under `base`.

    Defense in depth, not just trust in that precondition: if `leaf` were ever
    outside `base` (the caller's bug, not this function's job to catch), walking
    `.parent` upward only reaches `base` by chance — otherwise it continues past
    the filesystem root forever, since `Path("/").parent == Path("/")`. Bound the
    walk so that mistake becomes a loud exception instead of a silent hang.
    """
    dirs = []
    current = leaf
    while current != base:
        if current == current.parent:  # reached the filesystem root without hitting base
            raise UnsafePathError(f"{leaf} is not actually under {base}")
        dirs.append(current)
        current = current.parent
    return dirs
