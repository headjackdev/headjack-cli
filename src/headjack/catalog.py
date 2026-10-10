"""The resident-catalog rendering and measurement logic, shared between
local indexing and catalog listing — callers must agree on how a catalog entry is
rendered, or their comparison means nothing.

Walks every `SKILL.md` under given root(s), extracts the frontmatter `name` and
`description` — that pair is what the harness holds resident in the agent's catalog,
not the body — and can render or token-count either.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def render_catalog_entry(name: str, description: str) -> str:
    """Render one catalog entry exactly as this project *assumes* the harness does:
    `name: description`, one line. This is a stated assumption, not a confirmed
    format — settling it for real is `P0-01`'s job (blocked in this environment on a
    live-harness probe). Stating it here, once, is what lets `P1-17` reuse it
    instead of re-guessing the format independently.
    """
    return f"{name}: {description}".strip()


def find_skills(roots: list[Path]) -> list[Path]:
    """Every `SKILL.md` under any of `roots`, deduplicated and sorted."""
    found: set[Path] = set()
    for root in roots:
        if root.exists():
            found.update(root.rglob("SKILL.md"))
    return sorted(found)


def parse_skill_frontmatter(path: Path) -> dict[str, Any]:
    """Read `path` and parse its frontmatter via `parse_frontmatter_text` below.
    Returns `{}` for a missing or undecodable file."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    return parse_frontmatter_text(text)


def parse_frontmatter_text(text: str) -> dict[str, Any]:
    """Minimal frontmatter parser — the one both `headjack.corpus` (publish/index)
    and the catalog-measurement scripts use, so a skill's description is parsed
    identically regardless of which path reads it. Handles multi-line folded
    (`description: >`) and literal (`description: |`) YAML block scalars (a naive
    line-by-line split captures only the bare `>`/`|` indicator and silently
    discards the real text — exactly the corruption this fixed in the published
    catalog) and inline `[a, b, c]` lists (`systems:`). Still not a general YAML
    parser."""
    if not text.startswith("---"):
        return {}
    try:
        _, frontmatter, _ = text.split("---", 2)
    except ValueError:
        return {}

    lines = frontmatter.splitlines()
    meta: dict[str, Any] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#") or line[:1].isspace():
            i += 1
            continue
        key, sep, rest = line.partition(":")
        if not sep:
            i += 1
            continue
        key, rest = key.strip(), rest.strip()
        i += 1

        if rest in (">", ">-", "|", "|-", ""):
            block: list[str] = []
            while i < len(lines) and (not lines[i].strip() or lines[i][:1].isspace()):
                block.append(lines[i].strip())
                i += 1
            joiner = "\n" if rest in ("|", "|-") else " "
            meta[key] = joiner.join(part for part in block if part)
        elif rest.startswith("[") and rest.endswith("]"):
            inner = rest[1:-1].strip()
            meta[key] = (
                [] if not inner else [item.strip().strip("\"'") for item in inner.split(",")]
            )
        else:
            meta[key] = rest.strip("\"'")
    return meta


def load_tiktoken_encoding() -> Any | None:
    """`tiktoken` being importable does not mean it can actually count a token:
    `get_encoding()` lazily fetches its BPE data over the network on first use, and
    that fetch can fail (a sandboxed environment with no network access, for one).
    Resolved once, broadly — catching any exception, not just `ImportError` — rather
    than per call, which would either crash on the first real use or retry the
    network call once per text counted.
    """
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def count_tokens(text: str, encoding: Any | None) -> tuple[int, bool]:
    """Returns `(count, is_exact)`. Uses `encoding` (from `load_tiktoken_encoding`)
    if available; falls back to a labelled approximation (`len(text) / 4`)."""
    if encoding is not None:
        return len(encoding.encode(text)), True
    return max(1, len(text) // 4), False
