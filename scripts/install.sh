#!/usr/bin/env bash
# Installs the end-user `headjack` CLI onto this user's PATH via `uv tool
# install`, so `headjack install --scope global`, `headjack init`, etc. work
# from any directory — not just inside a checkout of this repo. There is no
# separate `tank` binary: "tank" is the MCP server's identity inside the
# harness (`mcpServers.tank`) and the package name, not a second CLI —
# `headjack install` registers it, `headjack tank-serve` is what a global
# registration actually launches.
#
# macOS and Linux only (uv's own installer covers both the same way; Windows
# needs a different PATH mechanism this script doesn't attempt).
#
# `headjack index`/`search` need a Python built with SQLite extension-loading
# enabled, for `sqlite-vec` — uv's own managed Python downloads disable this
# deliberately (see docs/install.md). This script looks for a system/Homebrew
# Python with it enabled and pins the tool install to that if found.
# `headjack install`/`init` don't need it either way, so a missing one is a
# warning, not a failure.
set -euo pipefail

case "$(uname -s)" in
  Darwin | Linux) ;;
  *)
    echo "error: this script supports macOS and Linux only (uname: $(uname -s))" >&2
    exit 1
    ;;
esac

has_ext_loading() {
  "$1" -c 'import sqlite3; sqlite3.connect(":memory:").enable_load_extension(True)' \
    >/dev/null 2>&1
}

python_bin=""
for candidate in \
  python3 \
  /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3 \
  /usr/local/bin/python3.12 /usr/local/bin/python3 \
  /usr/bin/python3.12 /usr/bin/python3
do
  if command -v "$candidate" >/dev/null 2>&1 && has_ext_loading "$candidate"; then
    python_bin="$(command -v "$candidate")"
    break
  fi
done

if [ -z "$python_bin" ]; then
  echo "warning: no Python found with SQLite extension-loading enabled." >&2
  echo "  'headjack install'/'init' will still work; 'headjack index'/'search'" >&2
  echo "  will not, until one is available. See docs/install.md." >&2
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found - installing it (https://docs.astral.sh/uv/)" >&2
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# Drop the old distribution name if present so only one `headjack` shim remains.
if uv tool list 2>/dev/null | grep -q '^headjack-rag'; then
  echo "uninstalling previous headjack-rag tool install..." >&2
  uv tool uninstall headjack-rag || true
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# `client` is mandatory — Tank's signature verification is not opt-in.
# `dense` rides along: `headjack publish` embeds on the operator's machine.
if [ -n "$python_bin" ]; then
  uv tool install --force --python "$python_bin" "${repo_root}[client,dense]"
else
  uv tool install --force "${repo_root}[client,dense]"
fi

uv tool update-shell

bin_dir="$(uv tool dir --bin)"

echo
"${bin_dir}/headjack" --version
echo "installed headjack to ${bin_dir}"
echo "open a new shell (uv tool update-shell just edited your shell's startup file,"
echo "not this running one) and run 'headjack --help'."
