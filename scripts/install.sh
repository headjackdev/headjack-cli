#!/usr/bin/env bash
# Installs the end-user `headjack` CLI onto this user's PATH via `uv tool
# install`, so `headjack install --scope global`, `headjack init`, etc. work
# from any directory. There is no separate `tank` binary: "tank" is the MCP
# server's identity inside the harness (`mcpServers.tank`) and the package
# name, not a second CLI — `headjack install` registers it, `headjack
# tank-serve` is what a global registration actually launches.
#
# Safe to run from a git checkout or as the standalone release asset:
#   curl -fsSL https://github.com/headjackdev/headjack-cli/releases/latest/download/install.sh | bash
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
  # uv's installer drops the binary here; expose it for the rest of this run.
  export PATH="$HOME/.local/bin:$PATH"
fi

# Drop the old distribution name if present so only one `headjack` shim remains.
if uv tool list 2>/dev/null | grep -q '^headjack-rag'; then
  echo "uninstalling previous headjack-rag tool install..." >&2
  uv tool uninstall headjack-rag || true
fi

# When this file lives in a source checkout, install from that tree. When it is
# the release asset (curl | bash or a downloaded copy), install from PyPI, with
# a GitHub fallback if the package is not published yet.
pkg='headjack-cli[client,dense]'
script_path="${BASH_SOURCE[0]:-$0}"
if [ -n "${script_path}" ] && [ -f "${script_path}" ]; then
  repo_root="$(cd "$(dirname "${script_path}")/.." && pwd 2>/dev/null || true)"
  if [ -n "${repo_root}" ] && [ -f "${repo_root}/pyproject.toml" ] \
    && grep -q 'name = "headjack-cli"' "${repo_root}/pyproject.toml" 2>/dev/null; then
    pkg="${repo_root}[client,dense]"
  fi
fi

# `client` is mandatory — Tank's signature verification is not opt-in.
# `dense` rides along: `headjack publish` embeds on the operator's machine.
python_args=()
if [ -n "$python_bin" ]; then
  python_args=(--python "$python_bin")
fi

if [[ "${pkg}" == /* ]] || [[ "${pkg}" == ./* ]]; then
  uv tool install --force "${python_args[@]}" "${pkg}"
elif uv tool install --force "${python_args[@]}" "${pkg}"; then
  :
else
  echo "PyPI install failed; falling back to GitHub source..." >&2
  uv tool install --force "${python_args[@]}" \
    'headjack-cli[client,dense] @ git+https://github.com/headjackdev/headjack-cli.git'
fi

bin_dir="$(uv tool dir --bin)"
# Make `headjack` resolvable in this process immediately.
export PATH="${bin_dir}:${PATH}"
# Persist for future shells (edits shell startup files).
uv tool update-shell >/dev/null 2>&1 || true

echo
"${bin_dir}/headjack" --version

# Piped installs (`curl … | bash`) cannot update the caller's interactive
# shell PATH — a new shell is unavoidable then. Interactive runs already have
# PATH exported above.
if [ -t 0 ] && command -v headjack >/dev/null 2>&1; then
  echo
  echo "Next:"
else
  echo
  echo "Open a new shell so \`headjack\` is on your PATH, then:"
fi
echo "  1. Get your invite token from the Headjack team"
echo "  2. headjack init --token <TOKEN>"
echo "  3. headjack install --client claude --scope global"
