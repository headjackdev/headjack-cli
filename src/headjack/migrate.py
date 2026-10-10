"""`headjack migrate`: move specific local skills into the cloud, one at a time,
on purpose — list what's installed locally, publish exactly the ones picked, then
take the local copy out of circulation.

"Migrate" means two things happen, in order, for each chosen skill:
1. The skill's own bundle (`SKILL.md` plus everything beneath it - P2-11; the
   same directory `headjack publish` would crawl as one bundle, scoped down to
   one skill instead of a whole corpus) is published to the remote.
2. Only once that succeeds, the skill's source directory is moved — never
   deleted outright — to a backup location no harness scans (not
   `~/.claude/skills`, not `~/.cursor/skills-cursor`), so a mistake is one `mv`
   away from undone, but the skill is gone from every surface that matters:
   `skill_search`, the gateway skill, a human's `ls`.

A skill whose artifacts fail to publish is never moved — the local copy is the
only copy until the remote confirms it has one too.

Choosing interactively (no names on the command line) shows each local skill
against the remote's own content hash for that name — up to date, changed since
the last publish, or never published at all — plus anything the remote already
has with no local copy. Migrating a skill is a one-way trip for the local
directory; knowing whether it would actually change anything up there first is
worth one extra round trip.
"""

from __future__ import annotations

import argparse
import base64
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from headjack import cliconfig
from headjack.bundle import bundle_hash
from headjack.chunker import chunk_markdown
from headjack.corpus import Artifact, crawl
from headjack.embed import Embedder

_DEFAULT_SKILLS_ROOT = Path.home() / ".claude" / "skills"
_DEFAULT_BACKUP_ROOT = Path.home() / ".headjack" / "migrated-skills"


class MigrateError(RuntimeError):
    """A migration step failed in a way that must stop before the one
    irreversible-ish step (the move) happens — raised, never worked around."""


@dataclass
class SkillMigration:
    name: str
    published: int
    skipped: int
    failed: int
    backup_path: Path | None  # None means the move did not happen


def discover_skills(skills_root: Path) -> list[str]:
    """Every immediate subdirectory of `skills_root` holding a `SKILL.md` — the
    same definition of "a skill" `corpus.py`'s crawler uses."""
    if not skills_root.exists():
        return []
    return sorted(p.parent.name for p in skills_root.glob("*/SKILL.md"))


def _artifacts_for_skill(skills_root: Path, skill_name: str, *, scope: str) -> list[Artifact]:
    """The one bundle artifact for this skill directory (P2-11: `SKILL.md` plus
    everything beneath it is a single artifact now, not one per file) - a list
    for call-site symmetry with the pre-bundle shape, but always 0 or 1 items."""
    return [
        a for a in crawl(skills_root, scope=scope) if a.kind == "skill" and a.path == skill_name
    ]


def _fetch_remote_index(
    client: httpx.Client, remote: str, token: str
) -> dict[tuple[str, str], str]:
    """`(kind, name) -> content_hash` for everything the token can see — shared by
    the actual publish step and the interactive listing's "already on remote?"
    comparison, so both answer that question the same way."""
    try:
        resp = client.get("/v1/artifacts", headers={"Authorization": f"Bearer {token}"})
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise MigrateError(f"could not reach {remote}: {exc}") from exc
    if resp.status_code in (401, 403):
        raise MigrateError(f"{resp.status_code} from {remote} - check the token")
    resp.raise_for_status()
    return {(row["kind"], row["name"]): row["content_hash"] for row in resp.json()}


def _local_skill_identity(skills_root: Path, *, scope: str) -> dict[str, tuple[str, str]]:
    """Local skill directory name -> (resolved catalog name, bundle hash) for
    each skill's own bundle (P2-11) — the same hash `_publish_skill_artifacts`
    compares against the remote's, so a skill shows as up to date exactly when
    migrating it would publish nothing."""
    result: dict[str, tuple[str, str]] = {}
    for artifact in crawl(skills_root, scope=scope):
        if artifact.kind != "skill":
            continue
        dirname = artifact.path.split("/", 1)[0]
        result[dirname] = (artifact.name, bundle_hash(artifact.files))
    return result


def _skill_status_label(
    dirname: str,
    local_identity: dict[str, tuple[str, str]],
    remote_index: dict[tuple[str, str], str],
) -> str:
    identity = local_identity.get(dirname)
    if identity is None:
        return ""
    name, body_hash = identity
    remote_hash = remote_index.get(("skill", name))
    if remote_hash is None:
        return "  [not on remote]"
    if remote_hash == body_hash:
        return "  [up to date on remote]"
    return "  [local changed since last publish]"


def _publish_skill_artifacts(
    artifacts: list[Artifact],
    remote: str,
    token: str,
    *,
    force: bool,
    embedder: Embedder,
    client: httpx.Client,
) -> tuple[int, int, int]:
    """Publish exactly these artifacts — the same unchanged-skip and payload
    shape as `headjack publish`, scoped to one skill's own files rather than a
    whole-corpus crawl."""
    auth = {"Authorization": f"Bearer {token}"}
    remote_index = _fetch_remote_index(client, remote, token)

    published = skipped = failed = 0
    for artifact in artifacts:
        local_hash = bundle_hash(artifact.files)
        key = (artifact.kind, artifact.name)
        if not force and remote_index.get(key) == local_hash:
            skipped += 1
            continue

        chunks = chunk_markdown(artifact.body)
        if not chunks:
            skipped += 1
            continue

        vectors = embedder.encode([c.text for c in chunks])
        payload = {
            "repo": artifact.repo,
            "path": artifact.path,
            "git_sha": None,
            "version": artifact.version,
            "systems": artifact.systems,
            "description": artifact.description,
            "body": artifact.body,
            "chunks": [
                {
                    "heading": c.heading,
                    "text": c.text,
                    "symptoms": c.symptoms,
                    "embedding": v.tolist(),
                }
                for c, v in zip(chunks, vectors, strict=True)
            ],
            "files": [
                {"path": f.path, "content_b64": base64.b64encode(f.content).decode("ascii")}
                for f in artifact.files
            ],
        }

        try:
            put_resp = client.put(
                f"/v1/artifacts/{artifact.kind}/{artifact.name}", json=payload, headers=auth
            )
        except (httpx.ConnectError, httpx.TimeoutException):
            failed += 1
            continue
        if put_resp.status_code in (401, 403):
            raise MigrateError(f"{put_resp.status_code} from {remote} - check the token")
        if put_resp.is_error:
            failed += 1
            continue
        published += 1

    return published, skipped, failed


def _move_to_backup(skill_dir: Path, backup_root: Path) -> Path:
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_root.chmod(0o700)
    dest = backup_root / skill_dir.name
    if dest.exists():
        raise MigrateError(
            f"backup already exists at {dest} - remove it or pass a different --backup-dir"
        )
    shutil.move(str(skill_dir), str(dest))
    return dest


def migrate_skill(
    skills_root: Path,
    skill_name: str,
    remote: str,
    token: str,
    *,
    scope: str = "personal",
    force: bool = False,
    backup_root: Path = _DEFAULT_BACKUP_ROOT,
    embedder: Embedder | None = None,
    client: httpx.Client | None = None,
) -> SkillMigration:
    skill_dir = skills_root / skill_name
    if not (skill_dir / "SKILL.md").exists():
        raise MigrateError(f"{skill_dir} has no SKILL.md - nothing to migrate")

    artifacts = _artifacts_for_skill(skills_root, skill_name, scope=scope)
    embedder = embedder or Embedder()
    owns_client = client is None
    client = client or httpx.Client(base_url=remote, timeout=30.0)
    try:
        published, skipped, failed = _publish_skill_artifacts(
            artifacts, remote, token, force=force, embedder=embedder, client=client
        )
    finally:
        if owns_client:
            client.close()

    if failed:
        return SkillMigration(skill_name, published, skipped, failed, backup_path=None)

    backup_path = _move_to_backup(skill_dir, backup_root)
    return SkillMigration(skill_name, published, skipped, failed, backup_path=backup_path)


# ------------------------------------------------------------------------------ CLI


def _prompt_for_skills(
    available: list[str],
    local_identity: dict[str, tuple[str, str]],
    remote_index: dict[tuple[str, str], str],
) -> list[str]:
    print("local skills:")
    for i, name in enumerate(available, 1):
        print(f"  {i}. {name}{_skill_status_label(name, local_identity, remote_index)}")

    remote_skill_names = {name for kind, name in remote_index if kind == "skill"}
    local_names = {name for name, _ in local_identity.values()}
    remote_only = sorted(remote_skill_names - local_names)
    if remote_only:
        print()
        print(f"on remote with no local copy: {', '.join(remote_only)}")

    raw = input("\nselect skills to migrate (comma-separated numbers, or 'all'): ").strip()
    if not raw:
        return []
    if raw.lower() == "all":
        return list(available)

    selected = []
    for part in raw.split(","):
        part = part.strip()
        if not part.isdigit() or not (1 <= int(part) <= len(available)):
            print(f"ignoring invalid selection: {part!r}", file=sys.stderr)
            continue
        selected.append(available[int(part) - 1])
    return selected


def _confirm(prompt: str) -> bool:
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="headjack migrate",
        description="Publish specific local skills to a remote Headjack, then move "
        "them out of the local skills root",
    )
    parser.add_argument(
        "skills", nargs="*", help="skill directory names to migrate (omit to choose interactively)"
    )
    parser.add_argument(
        "--skills-root",
        type=Path,
        default=_DEFAULT_SKILLS_ROOT,
        help=f"local skills directory to migrate from (default: {_DEFAULT_SKILLS_ROOT})",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=_DEFAULT_BACKUP_ROOT,
        help=f"where migrated skills are moved instead of deleted "
        f"(default: {_DEFAULT_BACKUP_ROOT})",
    )
    parser.add_argument(
        "--scope",
        default="personal",
        choices=["personal", "team", "org"],
        help="visibility scope recorded on every published artifact (default: personal)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-publish even if the remote already has this content",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be migrated; publish and move nothing",
    )
    parser.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args(argv)

    config = cliconfig.load()
    remote = cliconfig.resolve_remote(None)
    token = config.get("token")
    if token is None:
        parser.error("no token configured — run `headjack init --token <TOKEN>` first")

    available = discover_skills(args.skills_root)
    if not available:
        print(f"no skills with a SKILL.md found under {args.skills_root}", file=sys.stderr)
        return 1

    if args.skills:
        unknown = [s for s in args.skills if s not in available]
        if unknown:
            print(f"not found under {args.skills_root}: {', '.join(unknown)}", file=sys.stderr)
            return 1
        selected = args.skills
    else:
        client = httpx.Client(base_url=remote, timeout=30.0)
        try:
            remote_index = _fetch_remote_index(client, remote, token)
        except MigrateError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        finally:
            client.close()
        local_identity = _local_skill_identity(args.skills_root, scope=args.scope)

        selected = _prompt_for_skills(available, local_identity, remote_index)
        if not selected:
            print("nothing selected", file=sys.stderr)
            return 1

    print(f"about to migrate to {remote}:")
    for name in selected:
        print(f"  {name}  ->  publish, then move to {args.backup_dir / name}")

    if args.dry_run:
        return 0
    if not args.yes and not _confirm("proceed?"):
        print("aborted", file=sys.stderr)
        return 1

    failures = 0
    for name in selected:
        try:
            result = migrate_skill(
                args.skills_root,
                name,
                remote,
                token,
                scope=args.scope,
                force=args.force,
                backup_root=args.backup_dir,
            )
        except MigrateError as exc:
            print(f"{name}: {exc}", file=sys.stderr)
            failures += 1
            continue

        if result.failed:
            print(
                f"{name}: {result.failed} artifact(s) failed to publish - not moved",
                file=sys.stderr,
            )
            failures += 1
            continue

        print(
            f"{name}: published {result.published}, skipped {result.skipped} (unchanged) "
            f"-> moved to {result.backup_path}"
        )

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
