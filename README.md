# headjack-cli

**Headjack** is a catalog of agent skills. This package is the end-user CLI and
local MCP client (**Tank**) that talks to the public Headjack API over HTTPS.

```bash
uv tool install 'headjack-cli[client,dense]'
headjack init --token <TOKEN>
headjack search "how do I fill a pdf form"
headjack install --client cursor   # or: --client claude
```

Default API: `https://api.headjack.dev` (override with `$HEADJACK_REMOTE`).

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv tool install 'headjack-cli[client,dense]'
```

- `client` — MCP (Tank) + HTTPS CLI commands (`init`, `list`, `publish`, remote search)
- `dense` / `index` — local embeddings + SQLite index (`headjack index`, local search, `publish`)

If you previously installed the old `headjack-rag` tool name, uninstall it first
so only one `headjack` shim remains:

```bash
uv tool uninstall headjack-rag || true
```

Or use [`scripts/install.sh`](scripts/install.sh) from a checkout (uninstalls
`headjack-rag` when present, then installs this package).

Local `index` / SQLite vector search need a Python build with SQLite
extension-loading enabled — see [docs/install.md](docs/install.md).

## Quick start

1. Get a bearer token from your Headjack admin.
2. `headjack init --token <TOKEN>` — validates against the API, writes `~/.headjack/config.toml`.
3. `headjack search "…"`, `headjack list`, or `headjack publish ./my-skills`.
4. `headjack install --client cursor` (or `claude`) — registers Tank as an MCP
   server for that harness. Global installs launch via `headjack tank-serve`.

## Commands

| Command | Purpose |
|---------|---------|
| `init` | Save a validated token to `~/.headjack/config.toml` |
| `install` | Register Tank MCP + gateway skill for Claude or Cursor |
| `search` | Query local index if present, else the remote catalog |
| `list` | List artifacts your token can see |
| `publish` | Embed a corpus locally and push it to the remote API |
| `migrate` | Publish named local skills, then remove them from the skills root |
| `index` | Build/update the local SQLite index (`~/.headjack/index.db`) |
| `update` | Compare this CLI version to what the remote expects |
| `tank-serve` | Run Tank over stdio (what MCP hosts launch) |

## MCP setup

After `headjack install`, Claude Code / Cursor load Tank via stdio. A
secret-free template is in [`docs/tank-mcp-config.example.json`](docs/tank-mcp-config.example.json).

Environment (set by registration):

- `TANK_HEADJACK_URL` — catalog base URL (default public API)
- `TANK_TOKEN` — bearer token

## Development

```bash
uv sync --extra dev --extra client --extra index
uv run pytest -q -m "not slow"
uv build && uvx twine check dist/*
```

Publishing to PyPI uses Trusted Publishing — see [docs/release.md](docs/release.md).

## License

Apache-2.0 — see [LICENSE](LICENSE).
