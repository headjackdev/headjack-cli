"""The `headjack list` command: show every artifact your token can see on a remote
Headjack server — the CLI counterpart to `GET /v1/artifacts` (`P2-07`), for a human
asking "what's out there" rather than a client like Tank's mirror (`P3-12`).

No retrieval logic lives here - `GET /v1/artifacts` already does the scope/owner
filtering; this only renders whatever it returns.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx

from headjack import cliconfig

_KINDS = ("skill", "runbook", "script")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="headjack list",
        description="List every artifact your token can see on a remote server",
    )
    parser.add_argument(
        "--kind",
        choices=[*_KINDS, "all"],
        default="skill",
        help="artifact kind to show (default: skill)",
    )
    parser.add_argument("--json", action="store_true", help="emit raw JSON instead of a table")
    args = parser.parse_args(argv)

    config = cliconfig.load()
    remote = cliconfig.resolve_remote(None)
    token = config.get("token")
    if token is None:
        parser.error("no token configured — run `headjack init --token <TOKEN>` first")

    return _list_remote(remote, token, args)


def _list_remote(
    remote: str, token: str, args: argparse.Namespace, *, client: httpx.Client | None = None
) -> int:
    """`client` is injectable so tests can pass a `TestClient` bound to a real,
    in-process server instead of hitting the network — production callers (the CLI
    above) always get a real `httpx.Client`."""
    headers = {"Authorization": f"Bearer {token}"}
    owns_client = client is None
    client = client or httpx.Client(base_url=remote, timeout=30.0)

    try:
        resp = client.get("/v1/artifacts", headers=headers)
    except httpx.ConnectError as exc:
        print(f"could not connect to {remote}: {exc}", file=sys.stderr)
        return 1
    except httpx.TimeoutException as exc:
        print(f"{remote} did not respond in time: {exc}", file=sys.stderr)
        return 1
    finally:
        if owns_client:
            client.close()

    if resp.status_code == 401:
        print("the server rejected the token (401 unauthorized)", file=sys.stderr)
        return 1
    if resp.is_error:
        print(f"server returned {resp.status_code}: {resp.text}", file=sys.stderr)
        return 1

    items: list[dict[str, Any]] = resp.json()
    if args.kind != "all":
        items = [item for item in items if item["kind"] == args.kind]
    items.sort(key=lambda item: (item["kind"], item["name"]))

    if args.json:
        print(json.dumps(items, indent=2))
    elif not items:
        print("no artifacts found for this token", file=sys.stderr)
    else:
        for item in items:
            version = f"v{item['version']}" if item.get("version") else "unversioned"
            print(f"{item['name']} ({item['kind']}, {version})")
            if item.get("description"):
                print(f"   {item['description']}")
            systems = ", ".join(item["systems"]) if item.get("systems") else "-"
            print(f"   systems={systems}  updated={item['updated_at']}")
            print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
