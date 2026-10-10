"""Write Tank's MCP server registration into a client's config file, so
`skill_search` and friends become available there without hand-editing JSON.

Claude Code reads `.mcp.json` at the project root; Cursor reads `.cursor/mcp.json`
the same way. Both use the same `{"mcpServers": {...}}` shape, so one writer covers
both for project scope — only the path differs. See
docs/tank-mcp-config.example.json for the annotated shape this produces.

Global ("every project for this user") scope is not symmetric between clients.
Cursor's global config is a plain, dedicated file (`~/.cursor/mcp.json`, confirmed
by inspecting a real Cursor installation) — written the same way as project scope,
just at a different path. Claude Code's global ("user") scope lives inside
`~/.claude.json`, a large file the desktop app itself reads and writes continuously
as live state (session stats, onboarding flags, per-project settings, far more
than MCP config) — hand-editing it is a real corruption risk. `claude mcp add -s
user` is the official, safe way in: confirmed empirically that a user-scope entry
added this way needs no approval at all, unlike a project `.mcp.json` entry, which
Claude Code deliberately gates behind an interactive approval (reasonably so — a
cloned repo's `.mcp.json` could belong to anyone).

Every successful `register()` call also trust-on-first-use pins `remote`'s public
signing key (`GET /v1/signing-key`, unauthenticated - a public key isn't a secret)
into `tank.verify`'s trust store, so a freshly installed Tank can verify signed
content without an operator hand-copying a public key. This is a deliberate,
narrow weakening of that
module's own documented principle ("a key set the server controls is not a trust
anchor") in exchange for a zero-manual-steps install - not a silent one: see
`_tofu_pin_signing_key` for the fingerprint it prints and the one thing it refuses
to do automatically (replace a key already pinned under the same id).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

from tank import verify as tank_verify

_CONFIG_PATHS = {
    "claude": ".mcp.json",
    "cursor": ".cursor/mcp.json",
}
_SCOPES = ("project", "global")

# skill_install fetches and writes content a human never reviewed - the same
# supply-chain surface #147's signing work exists to narrow, not eliminate.
# Requiring an explicit approval the classifier can't silently override is a
# safety default for every global Claude Code registration, not an opt-in.
_SKILL_INSTALL_TOOL = "mcp__tank__skill_install"


def _launch_command(scope: str) -> tuple[str, list[str]]:
    """`global` scope is launched by the MCP host from whatever project the user
    actually has open — not this checkout — so `uv run` (which resolves against
    *that* directory's pyproject.toml, or fails outright if it has none) is the
    wrong command there. Use `headjack tank-serve` instead, reaching the same
    `tank.main` server loop through the one binary `scripts/install.sh` puts on
    PATH — there is no separate `tank` console script to type by hand; "tank" is
    the MCP server's identity inside the harness (`mcpServers.tank`), not a
    second CLI a human runs. `project` scope keeps `uv run --extra client
    python3 -m tank.main`, launched with cwd fixed to the registering project
    directory by the MCP host itself — the one case this project's own
    contributors actually want, so local edits to `src/tank/*.py` take effect
    without reinstalling."""
    if scope == "global":
        return "headjack", ["tank-serve"]
    return "uv", ["run", "--extra", "client", "python3", "-m", "tank.main"]


def _server_entry(remote: str, token: str, *, scope: str) -> dict[str, Any]:
    command, args = _launch_command(scope)
    return {
        "command": command,
        "args": args,
        "env": {"TANK_TOKEN": token, "TANK_HEADJACK_URL": remote},
        # Load-bearing, not decoration: Tool Search withholds MCP tool definitions
        # from context by default once a session crosses its activation threshold,
        # counting every non-alwaysLoad tool against it. Without this, Tank's tools
        # can be deferred out of context before skill_search is ever callable — see
        # docs/tank-mcp-config.example.json and src/tank/server.py.
        "alwaysLoad": True,
    }


def register(
    client: str, remote: str, token: str, project_dir: Path, *, scope: str = "global"
) -> Path:
    """Write/merge tank's MCP server entry for `client`. Returns the path or
    marker file written. Only the `mcpServers.tank` key is ever touched — every
    other entry and any top-level field already present is left exactly as it
    was. Idempotent: running twice produces the same result.

    `scope="global"` (default) applies to every project for this user — see the
    module docstring for why the two clients need genuinely different mechanisms
    for that, not just a different path. `scope="project"` writes under
    `project_dir` instead, shared the same way the rest of that project's config
    is — the exception, not the default, since a per-project `.mcp.json` entry
    needs one-time interactive approval that a global registration doesn't.

    `client == "claude" and scope == "global"` also requires explicit approval
    before `skill_install` ever runs, in Claude Code's own settings file - see
    `_require_approval_for_skill_install`.

    Every call, regardless of client or scope, also trust-on-first-use pins
    `remote`'s public signing key - the trust store it writes to
    (`tank.verify.TRUSTED_KEYS_PATH`) is a single per-user file, not scoped to a
    client or project. See `_tofu_pin_signing_key`."""
    if client not in _CONFIG_PATHS:
        raise ValueError(f"unknown client {client!r}, expected one of {sorted(_CONFIG_PATHS)}")
    if scope not in _SCOPES:
        raise ValueError(f"unknown scope {scope!r}, expected one of {list(_SCOPES)}")

    if client == "claude" and scope == "global":
        path = _register_claude_user_scope(remote, token)
        _require_approval_for_skill_install(Path.home() / ".claude" / "settings.json")
        _tofu_pin_signing_key(remote)
        return path

    config_path = (
        Path.home() / ".cursor" / "mcp.json"
        if client == "cursor" and scope == "global"
        else project_dir / _CONFIG_PATHS[client]
    )
    config_path.parent.mkdir(parents=True, exist_ok=True)

    config: dict[str, Any] = {}
    if config_path.exists():
        config = json.loads(config_path.read_text())

    servers = config.setdefault("mcpServers", {})
    servers["tank"] = _server_entry(remote, token, scope=scope)

    config_path.write_text(json.dumps(config, indent=2) + "\n")
    _tofu_pin_signing_key(remote)
    return config_path


def _tofu_pin_signing_key(remote: str) -> None:
    """Fetch `remote`'s public signing key and pin it into `tank.verify`'s trust
    store, so `skill_install` can verify signed content right after install
    without an operator hand-copying a public key.

    Never fatal: a network failure here must not fail the registration that
    already succeeded - it just means signed content stays unverifiable (the
    existing, fail-closed behavior) until a key is pinned some other way.

    Never silently replaces a *different* key already pinned under the same id -
    that is the one case trust-on-first-use cannot distinguish from a compromised
    or impersonating server, so it refuses outright and names the id rather than
    guessing which key the operator actually wants trusted."""
    try:
        resp = httpx.get(f"{remote.rstrip('/')}/v1/signing-key", timeout=5.0)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        print(
            f"warning: could not fetch {remote}'s signing key ({exc}) - signed "
            "content will stay unverifiable until a key is pinned manually "
            "or this command is re-run.",
            file=sys.stderr,
        )
        return

    key_id = data.get("key_id")
    public_key = data.get("public_key")
    if key_id is None or public_key is None:
        return  # this deployment doesn't sign - nothing to pin

    trusted = {
        k: base64.b64encode(v).decode("ascii") for k, v in tank_verify.load_trusted_keys().items()
    }
    if key_id in trusted:
        if trusted[key_id] == public_key:
            return  # already pinned, idempotent
        print(
            f"warning: a different key is already pinned for id {key_id!r} in "
            f"{tank_verify.TRUSTED_KEYS_PATH} - not overwriting it. If {remote} "
            "rotated its signing key on purpose, remove that entry yourself and "
            "re-run this command.",
            file=sys.stderr,
        )
        return

    trusted[key_id] = public_key
    _write_trusted_keys(trusted)
    fingerprint = hashlib.sha256(base64.b64decode(public_key)).hexdigest()[:16]
    print(f"pinned {remote}'s signing key {key_id!r} (fingerprint {fingerprint}) to trust store")


def _write_trusted_keys(keys: dict[str, str]) -> None:
    """Rewrite `tank.verify.TRUSTED_KEYS_PATH` from `keys` (id -> base64 public
    key). `tomllib` (stdlib) only reads TOML - this format is simple enough
    (two plain strings per table, no escaping concerns) to hand-generate rather
    than add a TOML-writing dependency for it."""
    path = tank_verify.TRUSTED_KEYS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tables = "\n".join(
        f'[[key]]\nid = "{key_id}"\npublic_key = "{public_key}"\n'
        for key_id, public_key in sorted(keys.items())
    )
    path.write_text(tables)


def _register_claude_user_scope(remote: str, token: str) -> Path:
    """Delegate to the real `claude` CLI rather than hand-editing ~/.claude.json
    directly — see the module docstring for why. Raises `RuntimeError` naming
    the cause if the CLI is missing or the add itself fails, rather than letting
    a raw CalledProcessError/FileNotFoundError be the first thing seen.

    `claude mcp add` has no overwrite flag and fails outright on a name that's
    already registered — the opposite of this function's own "idempotent,
    running twice produces the same result" promise (`register`'s docstring).
    Removing any existing entry first, ignoring whether one was actually there,
    is what makes a second run (e.g. after a token rotation) actually succeed
    instead of raising on "already exists"."""
    command, command_args = _launch_command("global")
    try:
        subprocess.run(
            ["claude", "mcp", "remove", "-s", "user", "tank"],
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "claude",
                "mcp",
                "add",
                "-s",
                "user",
                "tank",
                "-e",
                f"TANK_TOKEN={token}",
                "-e",
                f"TANK_HEADJACK_URL={remote}",
                "--",
                command,
                *command_args,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "the `claude` CLI was not found on PATH — it's required for "
            "--scope global with --client claude"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"`claude mcp add` failed: {exc.stderr or exc.stdout}") from exc

    return _patch_user_scope_always_load()


def _require_approval_for_skill_install(settings_path: Path) -> Path:
    """Merge `skill_install` into `permissions.ask` in Claude Code's own
    settings file - distinct from `~/.claude.json` (MCP server registry), this
    is `~/.claude/settings.json` (a plain, user-edited config, not live state
    the desktop app rewrites continuously, so a direct merge here is safe the
    way patching `~/.claude.json` by hand is not).

    `permissions.ask` is evaluated before the auto-mode classifier and always
    forces a prompt - a classifier `soft_deny` can be talked past by a later,
    explicit instruction ("install it, I reviewed it"), which is not a real
    human-in-the-loop guarantee the way an unconditional `ask` rule is.

    Only `permissions.ask` is ever touched; every other key, and any tool
    already listed there, is left exactly as it was. Idempotent."""
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings: dict[str, Any] = {}
    if settings_path.exists():
        settings = json.loads(settings_path.read_text())

    ask = settings.setdefault("permissions", {}).setdefault("ask", [])
    if _SKILL_INSTALL_TOOL not in ask:
        ask.append(_SKILL_INSTALL_TOOL)

    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    return settings_path


def _patch_user_scope_always_load() -> Path:
    """`claude mcp add` has no flag for `alwaysLoad`, so this patches it in as a
    single, targeted nested-key write — re-reading the file immediately
    beforehand rather than reusing an earlier copy, since Claude Code's desktop
    app may write to this same live file between steps."""
    config_path = Path.home() / ".claude.json"
    config = json.loads(config_path.read_text())
    config.setdefault("mcpServers", {}).setdefault("tank", {})["alwaysLoad"] = True
    config_path.write_text(json.dumps(config, indent=2))
    return config_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tank register", description="Register tank as an MCP server for a client"
    )
    parser.add_argument("--client", required=True, choices=sorted(_CONFIG_PATHS))
    parser.add_argument("--remote", required=True, help="Headjack server URL")
    parser.add_argument("--token", required=True, help="bearer token for this client")
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
        help="where to write a project-scope config (default: cwd; ignored for --scope global, "
        "the default)",
    )
    args = parser.parse_args(argv)

    path = register(args.client, args.remote, args.token, args.project_dir, scope=args.scope)
    print(f"wrote {args.client} ({args.scope}) MCP registration to {path}")
    if args.client == "claude" and args.scope == "project":
        print(
            "this needs one-time approval: run `claude` in this directory and "
            "approve the tank MCP server when prompted"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
