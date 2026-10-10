"""`headjack install` — the one command a new Claude Code or Cursor user runs to
start using a Headjack catalog: registers `tank` as an MCP server for the client,
places the `/tank` gateway skill so the catalog is reachable by hand (both to ask
it for knowledge and to hand new knowledge back via `/tank save ...`), and places
`tank-workflow`, the model-facing skill that teaches the model when to ask Tank
for knowledge on its own and how to hand new knowledge back.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from headjack import cliconfig
from tank.gateway import install_gateway, install_workflow_skill
from tank.register import _SCOPES, register

_CLIENTS = ("claude", "cursor")


def _default_skills_root(client: str) -> Path:
    """Computed at call time, not import time — `Path.home()` must stay patchable
    by tests, and a module-level constant would freeze it on first import instead."""
    if client == "claude":
        return Path.home() / ".claude" / "skills"  # mirrors tank.config's own default
    if client == "cursor":
        return Path.home() / ".cursor" / "skills-cursor"  # a real, observed path
    raise ValueError(f"unknown client {client!r}, expected one of {list(_CLIENTS)}")


def install(
    client: str,
    remote: str,
    token: str,
    project_dir: Path,
    skills_root: Path | None = None,
    *,
    scope: str = "global",
) -> tuple[Path, Path, Path]:
    """Register tank's MCP server for `client` and install both of Tank's static
    skills. Returns `(mcp_config_path, gateway_skill_path, workflow_skill_path)`.
    Raises `ValueError` for an unknown client — checked before any side effect
    runs. `scope` only affects the MCP registration (see `tank.register`'s module
    docstring for why Claude and Cursor need different mechanisms for "global");
    the skills always go to the client's skills directory, which is global by
    default for both clients already."""
    if client not in _CLIENTS:
        raise ValueError(f"unknown client {client!r}, expected one of {list(_CLIENTS)}")

    mcp_path = register(client, remote, token, project_dir, scope=scope)
    root = skills_root or _default_skills_root(client)
    gateway_path = install_gateway(root)
    workflow_path = install_workflow_skill(root)
    return mcp_path, gateway_path, workflow_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="headjack install",
        description="Register tank as an MCP server and install its gateway skill for a client",
    )
    parser.add_argument("--client", required=True, choices=_CLIENTS)
    parser.add_argument(
        "--scope",
        choices=_SCOPES,
        default="global",
        help="'global' (default, every project) or 'project' (this directory only)",
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path.cwd(),
        help="where to write a project-scope MCP config (default: cwd; ignored for --scope global, "
        "the default)",
    )
    parser.add_argument(
        "--skills-root",
        type=Path,
        default=None,
        help="override the client's default skills directory",
    )
    args = parser.parse_args(argv)

    config = cliconfig.load()
    remote = cliconfig.resolve_remote(None)
    token = config.get("token")
    if token is None:
        parser.error("no token configured — run `headjack init --token <TOKEN>` first")

    mcp_path, gateway_path, workflow_path = install(
        args.client,
        remote,
        token,
        args.project_dir,
        args.skills_root,
        scope=args.scope,
    )
    print(f"registered tank MCP server ({args.scope}) at {mcp_path}")
    print(f"installed /tank gateway skill at {gateway_path}")
    print(f"installed tank-workflow skill at {workflow_path}")
    if args.client == "claude" and args.scope == "project":
        print(
            "this needs one-time approval: run `claude` in this directory and "
            "approve the tank MCP server when prompted"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
