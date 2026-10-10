"""`headjack init`: write `~/.headjack/config.toml` with a validated token — the one
prerequisite `search`, `publish` and `list` all assume is already in place.

Validates the token against the server before saving anything: a token written
unchecked just moves the 401 to some later, less obvious command, with no reason
left to suspect the config file over anything else.

Separate from `headjack install` (`P3-10`'s Tank MCP registration + gateway skill):
that sets up a client (Claude/Cursor) to talk to Tank inside a chat session; this
sets up the `headjack` CLI itself, for a human running it directly in a terminal.
Different consumer, different config file, no overlap.
"""

from __future__ import annotations

import argparse
import sys

import httpx

from headjack import cliconfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="headjack init", description="Set up ~/.headjack/config.toml"
    )
    parser.add_argument("--token", required=True, help="bearer token issued by your Headjack admin")
    parser.add_argument("--force", action="store_true", help="overwrite an existing config file")
    args = parser.parse_args(argv)

    if cliconfig.CONFIG_PATH.exists() and not args.force:
        print(f"{cliconfig.CONFIG_PATH} already exists (use --force to overwrite)", file=sys.stderr)
        return 1

    remote = cliconfig.resolve_remote(None)
    if not _token_works(remote, args.token):
        return 1

    cliconfig.write({"token": args.token})

    print(f"wrote {cliconfig.CONFIG_PATH} (validated against {remote})")
    return 0


def _token_works(remote: str, token: str, *, client: httpx.Client | None = None) -> bool:
    """A lightweight auth check — `GET /v1/artifacts` accepts any authenticated
    token (`P2-07`), so a 200 here means the token is real without requiring
    anything be published yet.

    `client` is injectable so tests can pass a `TestClient` bound to a real,
    in-process server instead of hitting the network — production callers (`main`
    above) always get a real `httpx.Client`."""
    owns_client = client is None
    client = client or httpx.Client(base_url=remote, timeout=10.0)

    try:
        resp = client.get("/v1/artifacts", headers={"Authorization": f"Bearer {token}"})
    except httpx.ConnectError as exc:
        print(f"could not connect to {remote}: {exc}", file=sys.stderr)
        return False
    except httpx.TimeoutException as exc:
        print(f"{remote} did not respond in time: {exc}", file=sys.stderr)
        return False
    finally:
        if owns_client:
            client.close()

    if resp.status_code == 401:
        print("the server rejected the token (401 unauthorized)", file=sys.stderr)
        return False
    if resp.is_error:
        print(f"server returned {resp.status_code}: {resp.text}", file=sys.stderr)
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
