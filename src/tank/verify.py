"""Ed25519 signature verification — Tank's half of `P4-01`.

Trusted public keys are pinned in a client-local file, ``~/.headjack/trusted_keys.toml``
(`load_trusted_keys`). This module only ever reads that file — `tank.register`'s
trust-on-first-use pin (`_tofu_pin_signing_key`) is what writes to it, as a deliberate,
narrow weakening of "a key set the server controls is not a trust anchor" in exchange
for a zero-manual-steps install: it happens once, at an operator-initiated `headjack
install`, prints the key id and a fingerprint rather than pinning silently, and
refuses outright rather than ever replacing a *different* key already pinned under the
same id. `verify_signature` itself is unchanged by any of that: it fails closed on
every path it can take — an absent, empty or malformed signature, an unrecognised key
id, or a signature that does not match the body, all raise `SignatureError`. There is
no warn-and-continue branch anywhere in this module; a caller that wants to permit
unsigned artifacts has to make that decision itself, explicitly, before ever calling
this.
"""

from __future__ import annotations

import base64
import binascii
import tomllib
from collections.abc import Mapping
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# Pinned client-side, per P4-01's spec. Written either by a human (or their own
# tooling) copying in a public key, or by
# `tank.register`'s trust-on-first-use pin at `headjack install` time - see that
# module's docstring for why the latter is safe enough to default to.
TRUSTED_KEYS_PATH = Path.home() / ".headjack" / "trusted_keys.toml"


class SignatureError(RuntimeError):
    """Any reason a signature failed to verify: absent, empty, malformed, an unknown
    key id, or a body that doesn't match. Every raise site in this module uses this
    one type — a caller that needs to tell cases apart reads `str(exc)`, not a family
    of subclasses it would have to keep in sync with this module's checks. There is
    no path through `verify_signature` that returns without having verified."""


def load_trusted_keys(path: Path | None = None) -> dict[str, bytes]:
    """Read the pinned trust store at `path` (default `TRUSTED_KEYS_PATH`): one or
    more `[[key]]` tables, each an `id` and a `public_key` (base64-encoded raw
    32-byte Ed25519 public key), keyed by `id` in the returned mapping.

    ```toml
    [[key]]
    id = "ops-2026"
    public_key = "base64-encoded-32-byte-ed25519-public-key"

    [[key]]
    id = "ops-2027"
    public_key = "..."
    ```

    An absent file returns `{}` — "nobody has pinned a trust store yet" already
    fails closed on its own (every key id is unknown), so this isn't a distinct
    error case. A malformed file (bad TOML, a missing `id`/`public_key`, invalid
    base64) raises rather than silently skipping the bad entry — a trust store is
    exactly the file that must never degrade quietly.
    """
    target = path or TRUSTED_KEYS_PATH
    if not target.exists():
        return {}

    with target.open("rb") as f:
        data = tomllib.load(f)

    trusted: dict[str, bytes] = {}
    for entry in data.get("key", []):
        key_id = entry["id"]
        trusted[key_id] = base64.b64decode(entry["public_key"], validate=True)
    return trusted


def verify_signature(body: bytes, signature: str | None, trusted: Mapping[str, bytes]) -> str:
    """Returns the key id that verified `signature` over `body`. Raises
    `SignatureError` for every way this can fail: `signature` is `None` or empty,
    it isn't `ed25519:<keyid>:<base64sig>`, `<base64sig>` isn't valid base64, the
    key id names a key not in `trusted`, or the signature does not match `body`
    under that key. Nothing here warns and continues — every failure raises."""
    if not signature:
        raise SignatureError(
            "no signature present — Tank refuses to treat an absent signature as valid"
        )

    parts = signature.split(":", 2)
    if len(parts) != 3 or parts[0] != "ed25519":
        raise SignatureError(
            f"malformed signature {signature!r} — expected 'ed25519:<keyid>:<base64sig>'"
        )
    _, key_id, sig_b64 = parts

    if key_id not in trusted:
        raise SignatureError(f"unknown signing key id {key_id!r} — not in the trusted key set")

    try:
        sig_bytes = base64.b64decode(sig_b64, validate=True)
    except binascii.Error as exc:
        raise SignatureError(f"malformed signature {signature!r}: not valid base64") from exc

    try:
        public_key = Ed25519PublicKey.from_public_bytes(trusted[key_id])
    except ValueError as exc:
        raise SignatureError(f"trusted key {key_id!r} is not a valid Ed25519 public key") from exc

    try:
        public_key.verify(sig_bytes, body)
    except InvalidSignature as exc:
        raise SignatureError(
            f"signature verification failed for key id {key_id!r} — body does not match"
        ) from exc

    return key_id
