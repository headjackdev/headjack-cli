"""Local filesystem corpus crawler.

Walks a directory tree, classifies each file as a skill, runbook or script, and reads
it into an `Artifact`. Git (`P2-05`'s publish CLI) and remote fetch are layered on top
of this later — everything here is local filesystem only.

P2-11: a directory containing `SKILL.md` is one skill bundle — that file plus every
file beneath it, not one artifact per file. A runbook or script is still a one-file
bundle.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from headjack.bundle import (
    MAX_BUNDLE_BYTES,
    MAX_BUNDLE_FILES,
    MAX_FILE_BYTES,
    BundleFile,
    bundle_hash,
)
from headjack.catalog import parse_frontmatter_text

logger = logging.getLogger(__name__)

_SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__"}
_MAX_LOOSE_FILE_BYTES = 1_000_000
_MIN_SCRIPT_HEADER_CHARS = 40

# Public: `tank.tools.save` reuses this so a skill saved live through a session
# gets the same default a crawled one would, rather than a second hardcoded "1.0.0".
DEFAULT_SKILL_VERSION = "1.0.0"


@dataclass(frozen=True)
class Artifact:
    kind: str  # skill | runbook | script
    name: str
    path: str
    content_hash: str
    scope: str
    body: str
    updated_at: str
    systems: list[str] = field(default_factory=list)
    description: str = ""
    repo: str | None = None
    git_sha: str | None = None
    version: str | None = None
    owner: str | None = None
    reviewed_by: str | None = None
    # P2-11: what gets shipped - one member for a runbook or script, `SKILL.md`
    # plus everything beneath it for a skill.
    files: list[BundleFile] = field(default_factory=list)


def crawl(root: Path | str, *, scope: str = "personal") -> Iterator[Artifact]:
    """Walk `root`, classifying and reading every indexable file.

    Skips `.git/`, `node_modules/`, `.venv/`, `__pycache__/`; files over 1 MB;
    non-UTF-8 files (logged, not raised). Never follows directory symlinks —
    `os.walk`'s default (`followlinks=False`) is what keeps a symlink loop in the
    corpus from turning this into an infinite walk.

    A directory containing `SKILL.md` is consumed whole as one skill bundle - its
    own loose files and everything in its subdirectories - and is never also
    walked for loose runbook/script classification. `(kind, name)` must be unique
    across the whole crawl otherwise - the raw indexer and the publish-to-server
    upsert both key artifacts on it. Script and runbook names come from the bare
    filename stem, which collides whenever more than one skill bundles a
    same-named file (every skill with its own `scripts/__init__.py`, for
    example). Those collisions are detected and disambiguated below rather than
    left to crash `headjack index`'s raw insert or silently overwrite each other
    through `headjack publish`'s per-name upsert.

    A skill bundle over any of `headjack.bundle`'s caps is a hard error, not a
    silent drop - a skill published without its script is a skill that fails at
    the worst moment.
    """
    root = Path(root)
    staged: list[tuple[str, str, Path, list[str], str, bytes, str, str, str | None]] = []
    bundles: list[tuple[Path, list[BundleFile]]] = []

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        current = Path(dirpath)

        if "SKILL.md" in filenames:
            bundles.append((current, _collect_bundle_files(current)))
            dirnames[:] = []  # consumed whole; don't also walk its subtree below
            continue

        for filename in filenames:
            path = current / filename
            rel = path.relative_to(root)
            kind = _classify_loose(rel)
            if kind is None:
                continue

            body = _read_indexable(path, kind)
            if body is None:
                continue

            name, systems, description, version = _artifact_identity(kind, rel, body)
            staged.append(
                (
                    kind,
                    name,
                    rel,
                    systems,
                    body,
                    path.read_bytes(),
                    _mtime_iso(path),
                    description,
                    version,
                )
            )

    occurrences: dict[tuple[str, str], int] = {}
    for kind, name, _rel, _systems, _body, _raw, _mtime, _description, _version in staged:
        occurrences[(kind, name)] = occurrences.get((kind, name), 0) + 1

    for kind, name, rel, systems, body, raw_bytes, mtime, description, version in staged:
        if occurrences[(kind, name)] > 1:
            name = _disambiguate(name, rel)

        files = [BundleFile(rel.name, raw_bytes)]
        yield Artifact(
            kind=kind,
            name=name,
            path=str(rel),
            content_hash=bundle_hash(files),
            scope=scope,
            body=body,
            updated_at=mtime,
            systems=systems,
            description=description,
            version=version,
            files=files,
        )

    for skill_root, files in bundles:
        yield _build_skill_artifact(skill_root, files, root, scope)


def _walk(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for filename in filenames:
            yield Path(dirpath) / filename


def _collect_bundle_files(skill_root: Path) -> list[BundleFile]:
    """Every file under `skill_root` (itself included), as bundle members with
    POSIX paths relative to it. Raises `ValueError` naming whichever of
    `headjack.bundle`'s caps was hit - a hard error at crawl time, never a
    silent drop."""
    files: list[BundleFile] = []
    total_bytes = 0
    for path in _walk(skill_root):
        rel_posix = path.relative_to(skill_root).as_posix()
        content = path.read_bytes()
        if len(content) > MAX_FILE_BYTES:
            raise ValueError(
                f"{skill_root}: file {rel_posix!r} is {len(content)} bytes, over the "
                f"{MAX_FILE_BYTES}-byte per-file cap"
            )
        total_bytes += len(content)
        files.append(BundleFile(rel_posix, content))

    if len(files) > MAX_BUNDLE_FILES:
        raise ValueError(
            f"{skill_root}: {len(files)} files, over the {MAX_BUNDLE_FILES}-file bundle cap"
        )
    if total_bytes > MAX_BUNDLE_BYTES:
        raise ValueError(
            f"{skill_root}: bundle is {total_bytes} bytes, over the {MAX_BUNDLE_BYTES}-byte cap"
        )
    return files


def _build_skill_artifact(
    skill_root: Path, files: list[BundleFile], root: Path, scope: str
) -> Artifact:
    skill_md = next((f for f in files if f.path == "SKILL.md"), None)
    if skill_md is None:
        raise ValueError(f"{skill_root}: skill bundle has no SKILL.md at its root")
    try:
        body = skill_md.content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{skill_root}: SKILL.md is not valid UTF-8") from exc

    rel_skill_root = skill_root.relative_to(root)
    name, systems, description, version = _artifact_identity(
        "skill", rel_skill_root / "SKILL.md", body
    )
    return Artifact(
        kind="skill",
        name=name,
        path=str(rel_skill_root),
        content_hash=bundle_hash(files),
        scope=scope,
        body=body,
        updated_at=_mtime_iso(skill_root / "SKILL.md"),
        systems=systems,
        description=description,
        version=version,
        files=files,
    )


def _classify_loose(rel: Path) -> str | None:
    """Runbook/script classification for a file *outside* any skill bundle -
    `SKILL.md` is never reached here, since its directory is consumed whole by
    `crawl` before loose classification ever runs on it."""
    parts = rel.parts
    if rel.suffix == ".md" and ("runbooks" in parts or "docs" in parts):
        return "runbook"
    if rel.suffix in (".sh", ".py") and ("scripts" in parts or "bin" in parts):
        return "script"
    return None


def _read_indexable(path: Path, kind: str) -> str | None:
    try:
        if path.stat().st_size > _MAX_LOOSE_FILE_BYTES:
            return None
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except OSError as exc:
        logger.warning("skipping unreadable file %s: %s", path, exc)
        return None
    except UnicodeDecodeError:
        logger.warning("skipping non-UTF-8 file: %s", path)
        return None

    if kind != "script":
        return text

    header = _script_header(text)
    if len(header) < _MIN_SCRIPT_HEADER_CHARS:
        return None
    return header


def _script_header(text: str) -> str:
    """The leading comment block or module docstring, up to the first non-comment
    statement — never the executable body. Handles a shebang line followed by a
    Python module docstring as one combined header.
    """
    lines = text.splitlines()
    i = 0
    comment_lines: list[str] = []
    while i < len(lines) and lines[i].strip().startswith("#"):
        comment_lines.append(lines[i].strip().lstrip("#").strip())
        i += 1

    j = i
    while j < len(lines) and not lines[j].strip():
        j += 1

    if j < len(lines):
        first = lines[j].strip()
        if first.startswith('"""') or first.startswith("'''"):
            quote = first[:3]
            rest = "\n".join(lines[j:])
            end = rest.find(quote, 3)
            docstring = rest[3:end] if end != -1 else rest[3:]
            parts = [p for p in (" ".join(comment_lines), docstring.strip()) if p]
            return "\n".join(parts).strip()

    return "\n".join(comment_lines).strip()


def _disambiguate(name: str, rel: Path) -> str:
    """Make a colliding script/runbook name unique by prefixing it with its full
    directory path (not just the immediate parent) — a relative path is always
    unique within one crawl, so this can never collide again, at the cost of a
    longer name than the common, non-colliding case keeps."""
    parts = rel.parts[:-1]
    if not parts:
        return name
    return f"{'-'.join(parts)}-{name}"


def _artifact_identity(kind: str, rel: Path, body: str) -> tuple[str, list[str], str, str | None]:
    if kind != "skill":
        return rel.stem, [], "", None

    meta = parse_frontmatter_text(body)
    name = meta.get("name") or rel.parent.name
    systems = meta.get("systems")
    if not isinstance(systems, list):
        systems = []
    description = meta.get("description")
    # A skill with no declared version still needs one once it's in the catalog -
    # "unversioned" everywhere it's listed or installed reads as broken, not as a
    # deliberate state. Scripts and runbooks are reference material, never
    # installed on their own, so they stay unversioned.
    version = meta.get("version") or DEFAULT_SKILL_VERSION
    return (
        str(name),
        [str(s) for s in systems],
        str(description) if description else "",
        str(version),
    )


def _mtime_iso(path: Path) -> str:
    ts = path.stat().st_mtime
    return datetime.fromtimestamp(ts, tz=UTC).isoformat().replace("+00:00", "Z")
