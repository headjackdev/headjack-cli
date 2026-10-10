# Releasing `headjack-cli` to PyPI

The distribution name on PyPI is **`headjack-cli`** (console script remains
`headjack`). Publishing uses [Trusted Publishing](https://docs.pypi.org/trusted-publishers/)
(GitHub Actions OIDC) — no long-lived API tokens in repo secrets.

Workflow: [`.github/workflows/publish-pypi.yml`](../.github/workflows/publish-pypi.yml).

## Pending publisher values (configure once per index)

Create a **pending publisher** on each index before the first upload (the project
does not exist yet; that is expected).

### PyPI (`https://pypi.org`)

| Field | Value |
|-------|--------|
| Owner | `headjackdev` |
| Repository | `headjack-cli` |
| Workflow name | `publish-pypi.yml` |
| Environment name | `pypi` |

### TestPyPI (`https://test.pypi.org`)

| Field | Value |
|-------|--------|
| Owner | `headjackdev` |
| Repository | `headjack-cli` |
| Workflow name | `publish-pypi.yml` |
| Environment name | `testpypi` |

Also create matching GitHub Environments on the `headjackdev/headjack-cli` repo
(`pypi`, `testpypi`) so the workflow `environment:` keys resolve. Optional: add
required reviewers on `pypi`.

## Triggers

| Event | Publishes to |
|-------|----------------|
| `workflow_dispatch` with `target=testpypi` | TestPyPI |
| `workflow_dispatch` with `target=pypi` | PyPI |
| `push` of tags matching `v*` | PyPI |

## Operator checklist

1. Re-verify the name is free: `curl -s -o /dev/null -w '%{http_code}\n' https://pypi.org/pypi/headjack-cli/json` → `404` until claimed.
2. Use a **shared** PyPI / TestPyPI account (org/project), not a personal account that leaves with one person. Enable **2FA** on every owner/maintainer.
3. Configure the two pending publishers (tables above) and the two GitHub Environments on `headjackdev/headjack-cli`.
4. Rehearse: Actions → **Publish to PyPI** → `target=testpypi`. Confirm `https://test.pypi.org/project/headjack-cli/` and `uv tool install --index-url https://test.pypi.org/simple headjack-cli`.
5. Claim production: either dispatch `target=pypi` for `0.1.0`, or tag `v0.1.0` on the release commit after it is on the branch you intend to ship from.
6. Confirm `https://pypi.org/project/headjack-cli/` and `uv tool install headjack-cli` installs the `headjack` console script.

## GitHub Release asset

On every push to `main`, CI creates (or updates) the GitHub Release for the
version in `pyproject.toml` and attaches `scripts/install.sh` as the asset
named `install.sh`. That powers the public install one-liner:

```bash
curl -fsSL https://github.com/headjackdev/headjack-cli/releases/latest/download/install.sh | bash
```

## Out of band (not automated here)

- Creating the PyPI/TestPyPI accounts and pending publishers (human).
- Enabling 2FA on those accounts (human).
- PEP 541 takeover of the abandoned bare name `headjack` (optional, later).

