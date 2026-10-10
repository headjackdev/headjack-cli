"""`tank`'s own argparse dispatcher — not an installed console script. "Tank" is
the MCP server's identity inside the harness (`mcpServers.tank`, the Matrix
reference) and the package name (`src/tank/`), not a second CLI binary a human
runs alongside `headjack`; everything a person types goes through `headjack`
(`headjack install` wraps `register()` below, `headjack tank-serve` reaches
`tank.main`'s server loop). This dispatcher stays for direct module invocation
(`python3 -m tank.cli ...`) during development and for tests that exercise it
without going through `headjack`.

Matches `headjack.cli`'s pattern: each subcommand's own argument parsing lives in
its own module, imported lazily so a subcommand's own dependencies don't leak into
commands that don't need them.
"""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tank", description="Tank — the local operator")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "register", help="register tank as an MCP server for a client", add_help=False
    )

    args, rest = parser.parse_known_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "register":
        from tank.register import main as register_main

        return register_main(rest)

    print(f"tank {args.command}: not implemented yet")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
