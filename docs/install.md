# Install notes

## Python and sqlite-vec

`headjack index` and local vector search load the [sqlite-vec](https://github.com/asg017/sqlite-vec)
extension. That requires a Python whose `sqlite3` module was built with
`enable_load_extension`.

**uv-managed Python downloads disable extension loading on purpose.** Prefer a
system or Homebrew CPython 3.12+:

```bash
# check
python3 -c 'import sqlite3; sqlite3.connect(":memory:").enable_load_extension(True); print("ok")'

# pin the tool install to that interpreter
uv tool install --python "$(command -v python3)" 'headjack-cli[client,dense]'
```

`headjack init` / `install` / remote `search` / `list` do not need sqlite-vec.

## Upgrading from headjack-rag

The distribution was renamed to `headjack-cli`. If both tools are installed, two
shims fight over the `headjack` name:

```bash
uv tool uninstall headjack-rag
uv tool install 'headjack-cli[client,dense]'
```

## Config

| Path | Role |
|------|------|
| `~/.headjack/config.toml` | Token (and optional research `db` key) written by `headjack init` |
| `~/.headjack/index.db` | Local SQLite index for `headjack index` / local search |
| `$HEADJACK_REMOTE` | Override the default API (`https://api.headjack.dev`) |
