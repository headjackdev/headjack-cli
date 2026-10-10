"""The `headjack` command-line entry point.

Subcommands are wired lazily so `headjack --help` stays cheap and a missing heavy
dependency (torch, sqlite-vec) does not break commands that do not need it.

Every command resolves its remote from code (`cliconfig.DEFAULT_REMOTE`) and
`$HEADJACK_REMOTE` only, its token from `~/.headjack/config.toml` only (written
by `headjack init`), and its local index at the one fixed `cliconfig.DEFAULT_DB_PATH`
— none of the three is a CLI flag anywhere in this file's subcommands.
"""

from __future__ import annotations

import argparse

_EPILOG = """\
examples:
  headjack init --token <TOKEN>
  headjack install --client claude
  headjack publish ./skills-corpus
  headjack search "how do I fill a pdf form" -k 5
  headjack list
  headjack migrate pdf
  headjack index ./skills-corpus
  headjack update

`index` builds the one local SQLite index (~/.headjack/index.db) from a corpus, for
research/ops work — it never touches a remote catalog. `search` uses that local
index if it exists, otherwise queries the remote. `publish` is how a corpus
actually gets into a remote Headjack. Set `$HEADJACK_REMOTE` to point any command
at a server other than the public default.

run `headjack <command> --help` for a command's own flags.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="headjack",
        description="Headjack — search, publish, and load agent skills on demand.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"headjack {_version()}")
    subparsers = parser.add_subparsers(dest="command", metavar="command")
    # Each subparser here is a bare placeholder, just enough for `headjack --help`
    # to list it and for top-level dispatch to recognise the token. The real
    # argument parsing happens in each subcommand's own `main()`, reached via
    # parse_known_args below — that keeps this file from duplicating --db, --scope
    # and friends, and keeps them lazy-imported.
    subparsers.add_parser(
        "init", help="set up ~/.headjack/config.toml with a validated token", add_help=False
    )
    subparsers.add_parser(
        "install",
        help="register tank as an MCP server and install its gateway skill for a client",
        add_help=False,
    )
    subparsers.add_parser(
        "index",
        help="build or update a local SQLite index from a corpus (research/ops; "
        "see `publish` to add content to a remote catalog)",
        add_help=False,
    )
    subparsers.add_parser(
        "search",
        help="query the local index if one exists, else the remote catalog",
        add_help=False,
    )
    subparsers.add_parser(
        "publish",
        help="chunk and embed a corpus locally, push it to a remote Headjack server",
        add_help=False,
    )
    subparsers.add_parser(
        "list",
        help="list every artifact your token can see on a remote Headjack server",
        add_help=False,
    )
    subparsers.add_parser(
        "migrate",
        help="publish specific local skills to a remote, then move them out of the "
        "local skills root",
        add_help=False,
    )
    subparsers.add_parser("update", help="check the remote for a newer CLI version", add_help=False)
    subparsers.add_parser(
        "tank-serve",
        help="run Tank's MCP server over stdio - what a global `headjack install` "
        "registration launches, not a command to type by hand",
    )
    subparsers.add_parser("help", help="show this help message and exit", add_help=False)

    args, rest = parser.parse_known_args(argv)

    if args.command is None or args.command == "help":
        parser.print_help()
        return 0

    if args.command == "init":
        from headjack.init import main as init_main

        return init_main(rest)

    if args.command == "install":
        from headjack.install import main as install_main

        return install_main(rest)

    if args.command == "index":
        from headjack.indexer import main as index_main

        return index_main(rest)

    if args.command == "search":
        from headjack.search import main as search_main

        return search_main(rest)

    if args.command == "publish":
        from headjack.publish import main as publish_main

        return publish_main(rest)

    if args.command == "list":
        from headjack.listing import main as list_main

        return list_main(rest)

    if args.command == "migrate":
        from headjack.migrate import main as migrate_main

        return migrate_main(rest)

    if args.command == "update":
        from headjack.update import main as update_main

        return update_main(rest)

    if args.command == "tank-serve":
        from tank.main import main as tank_serve_main

        tank_serve_main()
        return 0

    print(f"headjack {args.command}: not implemented yet")
    return 1


def _version() -> str:
    from headjack import __version__

    return __version__
