"""Shared config-file fallback for the `headjack` CLI's `--db`/`--token` flags, so a
working local setup doesn't need repeating on every invocation, plus the one place
that resolves which server a command talks to.

`~/.headjack/config.toml`, read once per command. A CLI flag always wins over the
file — this only fills in a default when a flag was left unset. `headjack init`
(the only writer) validates a token against the server before ever saving it, so a
file that exists is trustworthy enough that a parse failure should be loud, not a
silent fallback to "nothing configured".

The remote server is deliberately *not* a config-file key: every command talks to
the same public Headjack by default, so there is nothing per-machine to remember.
`resolve_remote()` below is the one place that default can be overridden, and it is
an environment variable (`HEADJACK_REMOTE`), not the file — a file entry would
silently persist past the local-testing session it was meant for, redirecting every
later command on the machine with no visible reminder that it's still there.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

CONFIG_PATH = Path.home() / ".headjack" / "config.toml"

# The public, production Headjack catalog.
DEFAULT_REMOTE = "https://api.headjack.dev"

# The one local index every dot-directory-based command reads/writes. Not a flag,
# not a config-file key — `index`/`search` operate on a single, fixed local index
# per machine, the same way `config.toml` is a fixed location.
# A research setup that genuinely needs several side-by-side index files is already
# outside what the installed CLI is for; invoke the library functions directly.
DEFAULT_DB_PATH = Path.home() / ".headjack" / "index.db"

# Set to point commands at a local dev server instead - e.g. while working on
# Headjack itself. Deliberately not a config-file key; see the module docstring.
_REMOTE_ENV_VAR = "HEADJACK_REMOTE"

# Every key `~/.headjack/config.toml` will ever read a default from, and the only
# keys `write()` will accept — keeps a typo'd key in the file from silently doing
# nothing, and keeps `load()` from returning arbitrary caller-uncontrolled keys.
# Deliberately not `scope`: which scope to publish or search as varies by what
# you're doing, not by "my setup", and a forgotten config entry silently changing
# that is a worse failure mode than just asking for it each time. Deliberately not
# `remote` either — see the module docstring. `db` is accepted for advanced local
# tooling; the CLI's own `index`/`search` use the fixed `DEFAULT_DB_PATH` and
# never read this key.
_KNOWN_KEYS = ("db", "token")


class HeadjackConfigError(RuntimeError):
    """The config file exists but isn't valid TOML, or isn't a flat key=value
    table. Raised rather than silently ignored — the file's existence means
    someone meant to configure something, and a silent fallback to "nothing
    configured" would be a confusing way to find out it didn't work."""


def load() -> dict[str, str]:
    """Return whatever of `_KNOWN_KEYS` are present in the config file as
    strings. An absent file returns `{}` — that is the expected, silent case,
    not an error."""
    if not CONFIG_PATH.exists():
        return {}

    try:
        with CONFIG_PATH.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise HeadjackConfigError(f"{CONFIG_PATH} is not valid TOML: {exc}") from exc

    result: dict[str, str] = {}
    for key in _KNOWN_KEYS:
        if key not in data:
            continue
        value = data[key]
        if not isinstance(value, str):
            raise HeadjackConfigError(
                f"{CONFIG_PATH}: {key!r} must be a string, got {type(value).__name__}"
            )
        result[key] = value
    return result


def write(values: dict[str, str]) -> None:
    """Replace the config file wholesale with `values` (a subset of `_KNOWN_KEYS`).
    `headjack init` is the only writer and always rewrites the whole file, so a
    partial in-place update isn't something this needs to support yet.

    Creates `~/.headjack` if needed. Mirrors Tank's own convention (`safe.py`):
    directories at 0700, the file itself at 0600 once written — this holds a bearer
    token, not shareable config.
    """
    unknown = sorted(set(values) - set(_KNOWN_KEYS))
    if unknown:
        raise HeadjackConfigError(f"not a config key: {', '.join(unknown)}")

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.parent.chmod(0o700)
    lines = [f"{key} = {json.dumps(value)}" for key, value in values.items()]
    CONFIG_PATH.write_text("\n".join(lines) + "\n")
    CONFIG_PATH.chmod(0o600)


def resolve_remote(cli_value: str | None) -> str:
    """CLI flag > `$HEADJACK_REMOTE` (local testing) > the public default. Always
    returns something - callers never need a "no remote configured" error path."""
    return cli_value or os.environ.get(_REMOTE_ENV_VAR) or DEFAULT_REMOTE
