"""`headjack update`: ask the remote what CLI version it expects, compare to this
build, and say what to do about it.

No CLI flags — same reasoning as every other command in this package now:
the remote is `cliconfig.DEFAULT_REMOTE`/`$HEADJACK_REMOTE`, nothing else. No
token is needed; `GET /v1/cli/version` is unauthenticated.

This does not self-update. It prints the install command the server
advertises (`install_url`) so the operator can re-run `uv tool install` —
more honest than pretending an automated in-place update exists.
"""

from __future__ import annotations

import sys

import httpx

from headjack import __version__, cliconfig


def _version_tuple(version: str) -> tuple[int, ...]:
    """Plain dotted-integer comparison — every version this project has produced
    is `X.Y.Z`, so full PEP 440 parsing (a new dependency) buys nothing yet."""
    return tuple(int(part) for part in version.split("."))


def main(argv: list[str] | None = None) -> int:
    if argv:
        print("headjack update takes no arguments", file=sys.stderr)
        return 1

    remote = cliconfig.resolve_remote(None)
    return _check(remote)


def _check(remote: str, *, client: httpx.Client | None = None) -> int:
    """`client` is injectable so tests can pass a `TestClient` bound to a real,
    in-process server instead of hitting the network — production callers (`main`
    above) always get a real `httpx.Client`."""
    owns_client = client is None
    client = client or httpx.Client(base_url=remote, timeout=10.0)

    try:
        resp = client.get("/v1/cli/version")
    except httpx.ConnectError as exc:
        print(f"could not connect to {remote}: {exc}", file=sys.stderr)
        return 1
    except httpx.TimeoutException as exc:
        print(f"{remote} did not respond in time: {exc}", file=sys.stderr)
        return 1
    finally:
        if owns_client:
            client.close()

    if resp.is_error:
        print(f"server returned {resp.status_code}: {resp.text}", file=sys.stderr)
        return 1

    data = resp.json()
    latest = data["latest"]
    minimum = data["minimum"]
    install_url = data["install_url"]
    current = __version__

    if _version_tuple(current) >= _version_tuple(latest):
        print(f"headjack {current} is up to date ({remote})")
        return 0

    if _version_tuple(current) < _version_tuple(minimum):
        print(
            f"headjack {current} is below the minimum supported version {minimum} "
            f"for {remote}. Update: {install_url}",
            file=sys.stderr,
        )
        return 1

    print(f"headjack {current} — {latest} is available. Update: {install_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
