# Headjack

**Access is private / invite-only for now.** You need an invite token from the
Headjack team before `headjack init` will work.

**Headjack** is a catalog of agent skills your coding agents can load on demand.
This package is the end-user CLI and local MCP client (**Tank**).

## Quickstart

```bash
curl -fsSL https://github.com/headjackdev/headjack-cli/releases/latest/download/install.sh | bash
```

Then:

```bash
headjack init --token <TOKEN>
headjack install --client claude --scope global
```

| Flag | Meaning |
|------|---------|
| `--client` | Which agent to configure: `claude` (Claude Code) or `cursor` |
| `--scope` | `global` (default) — every project for this user · `project` — this directory only |

`headjack init` validates your token and writes `~/.headjack/config.toml`.

## Everyday commands

| Command | Purpose |
|---------|---------|
| `init` | Save a validated token to `~/.headjack/config.toml` |
| `install` | Register Tank MCP + gateway skill for Claude or Cursor |
| `search` | Query the catalog (local index if present, else remote) |
| `list` | List artifacts your token can see |
| `publish` | Embed a corpus locally and push it to the catalog |
| `migrate` | Publish named local skills, then remove them from the skills root |
| `update` | Compare this CLI version to what the catalog expects |

After `headjack install`, Claude Code / Cursor load Tank over stdio. A
secret-free template is in [`docs/tank-mcp-config.example.json`](docs/tank-mcp-config.example.json).

Local `index` / SQLite vector search need a Python build with SQLite
extension-loading enabled — see [docs/install.md](docs/install.md).

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
