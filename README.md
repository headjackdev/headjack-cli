# Headjack

**Skills for your AI agents — on demand.**

Headjack is a catalog of agent skills. Install the CLI, connect Claude Code or
Cursor, and your agent can search and load the right skill when it needs it —
without stuffing everything into context up front.

## Quick install

macOS / Linux:

```bash
curl -fsSL https://github.com/headjackdev/headjack-cli/releases/latest/download/install.sh | bash
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install 'headjack-cli[client,dense]'
```

Requires Python 3.12+. Open a new shell after install, then check:

```bash
headjack --version
```

## Configure your token

You’ll get a bearer token from your Headjack workspace admin.

```bash
headjack init --token <TOKEN>
```

This validates the token and saves it to `~/.headjack/config.toml`. That’s all
you need — the CLI already knows how to reach Headjack.

## Connect your agent

Register **Tank** (Headjack’s local MCP server) with your coding agent:

```bash
headjack install --client claude --scope global
```

| Flag | Meaning |
|------|---------|
| `--client` | Which agent to configure: `claude` (Claude Code) or `cursor` |
| `--scope` | `global` (default) — every project for this user · `project` — this directory only |

Other common variants:

```bash
headjack install --client cursor --scope global
headjack install --client claude --scope project
headjack install --client cursor --scope project
```

After that, Claude Code / Cursor launch Tank automatically. Your agent can search
the catalog and install skills through MCP tools.

## What you get

- **Search** — find skills and runbooks by what you’re trying to do
- **Install on demand** — pull a skill into the agent’s workspace when it’s needed
- **MCP (Tank)** — the local bridge your agent talks to; works with the catalog over HTTPS
- **Offline cache** — skills you’ve already installed stay usable if the catalog is unreachable

Handy commands once you’re set up:

```bash
headjack search "fill a pdf form"
headjack list
```

## Docs & support

- Install notes (Python / sqlite-vec): [docs/install.md](docs/install.md)
- Issues: [github.com/headjackdev/headjack-cli/issues](https://github.com/headjackdev/headjack-cli/issues)

## License

Apache-2.0 — see [LICENSE](LICENSE).
