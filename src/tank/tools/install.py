"""skill_install: fetch, verify, store, activate (`P3-04`).

Order is the specification — each step guards the next, and reordering silently
weakens it:

1. ``GET /v1/skill/{name}?version=…``
2. ``bundle_hash(files) == content_hash`` (`P2-11`) — mismatch refuses, nothing
   written
3. signature check (``#147``) — a real signature is verified against the pinned
   trust store (``~/.headjack/trusted_keys.toml``, ``tank.verify.load_trusted_keys``);
   an absent signature is still refused by default, only accepted with
   ``TANK_ALLOW_UNSIGNED=1`` set explicitly — content published before a signing key
   existed has no signature to check and isn't retroactively invalid
4. write to the store
5. record the manifest entry
6. activate into the skills root under ``tank-<scope>-<name>``
7. return the confirmation, naming Tank, never the function

Already-in-the-store-at-that-exact-version skips steps 1-3 entirely and goes
straight to activation — what makes reinstall work with Headjack unreachable.

`P4-05`'s fencing deliberately does **not** wrap the body here, even though this
module is in that issue's scope. Every return path below is a short confirmation
or refusal string — never the body itself — so there is no model-facing tool
result containing artifact text to fence, the way a search snippet is one.  The
body's only destination is `tank_store.activate()`, which writes it verbatim to
`SKILL.md` under the skills root; that file is a harness-owned format the host
parses for YAML frontmatter (name/description) before ever showing it to a model,
not a tool-result string Tank controls the presentation of. Wrapping it would put
non-YAML text before the required frontmatter `---` and break skill discovery for
everything Tank installs — `tests/test_tank_install.py`'s exact-equality
assertion on the activated file's contents already guards against exactly that.
"""

from __future__ import annotations

import base64
import os

from tank import store as tank_store
from tank import verify as tank_verify
from tank.bundle import BundleFile, bundle_hash
from tank.client import HeadjackClient, HeadjackUnavailable
from tank.config import TankConfig


def _check_signature(signature: str | None, body: bytes) -> tuple[str | None, str]:
    """Returns `(refusal_message_or_None, signature_state)`. Never raises — every
    caller of this is a tool result, not an exception boundary."""
    if signature is None:
        if os.environ.get("TANK_ALLOW_UNSIGNED") != "1":
            return (
                "Tank refuses to install this unsigned: Headjack returned no "
                "signature and TANK_ALLOW_UNSIGNED is not set. Set "
                "TANK_ALLOW_UNSIGNED=1 to accept this explicitly — the default "
                "must fail closed.",
                "",
            )
        return (None, "unsigned")

    trusted = tank_verify.load_trusted_keys()
    try:
        key_id = tank_verify.verify_signature(body, signature, trusted)
    except tank_verify.SignatureError as exc:
        return (f"Tank refuses to install this: signature verification failed: {exc}", "")
    return (None, f"verified:{key_id}")


def _activate_and_record(
    config: TankConfig,
    name: str,
    version: str,
    body: str,
    content_hash: str,
    scope: str,
    *,
    signature_state: str,
    from_cache: bool,
) -> str:
    """Returns the confirmation message, or a "can't activate" refusal — the
    caller tells the two apart by checking for the refusal's one source, the
    `max_active` cap, rather than sniffing message text."""
    act_name = tank_store.activation_name(scope, name)
    already_active = tank_store.is_active(config.skills_root, act_name)
    if not already_active and tank_store.active_count(config.store) >= config.max_active:
        return (
            f"Tank can't activate {name}: at the cap ({config.max_active} active "
            f"skills). Automatic eviction isn't built yet — evict something "
            f"manually first."
        )

    tank_store.activate(config.skills_root, body, act_name)
    tank_store.record_manifest_entry(
        config.store,
        name,
        version=version,
        content_hash=content_hash,
        scope=scope,
        signature_state=signature_state,
        active=True,
    )
    suffix = " (already cached, no network needed)" if from_cache else ""
    return f"Tank loaded {name} v{version} · invoke it with /{act_name}{suffix}"


def skill_install(
    client: HeadjackClient, config: TankConfig, name: str, version: str = "latest"
) -> str:
    """Fetch, verify, store and activate one skill. Returns a plain-text result
    meant to be shown to the user verbatim — never raises; every failure mode
    becomes a clear message instead of an exception reaching the caller."""
    if version != "latest" and tank_store.is_stored(config.store, name, version):
        meta = tank_store.read_meta(config.store, name, version)
        body = tank_store.read_body(config.store, name, version)
        return _activate_and_record(
            config,
            name,
            version,
            body,
            meta["content_hash"],
            meta["scope"],
            signature_state=meta["signature_state"],
            from_cache=True,
        )

    try:
        resp = client.get(f"/v1/skill/{name}", params={"version": version})
    except HeadjackUnavailable as exc:
        return f"Tank can't install {name}: {exc}"

    if resp.status_code == 404:
        return f"Tank can't find {name} — not found, or not visible to this token."
    if resp.status_code == 403:
        detail = resp.json().get("detail", "unreviewed")
        return f"Tank can't install {name}: {detail}."
    if resp.status_code == 500:
        return (
            f"Headjack reported a content integrity failure serving {name} — refusing to install."
        )
    if resp.is_error:
        return f"Headjack returned an error ({resp.status_code}) fetching {name}."

    payload = resp.json()
    body = payload["body"]

    # P2-11: content_hash is bundle_hash(files), not sha256(body) - a row-less
    # `files` list (an artifact published before this column existed) can never
    # match a real stored hash, which is intentional: it refuses rather than
    # silently trusting unverifiable content. Republishing fixes it exactly once.
    files = [BundleFile(f["path"], base64.b64decode(f["content_b64"])) for f in payload["files"]]
    try:
        recomputed = bundle_hash(files) if files else None
    except ValueError:
        recomputed = None
    if recomputed != payload["content_hash"]:
        reported = recomputed[:12] if recomputed else "none (no files in response)"
        return (
            f"Tank refuses to install {name}: content hash mismatch (server said "
            f"{payload['content_hash'][:12]}…, recomputed {reported}…). "
            f"Nothing written."
        )

    refusal, signature_state = _check_signature(payload.get("signature"), body.encode())
    if refusal:
        return refusal

    resolved_version = payload["version"]
    scope = payload["scope"]
    tank_store.write_to_store(
        config.store,
        name,
        resolved_version,
        body,
        content_hash=payload["content_hash"],
        scope=scope,
        signature_state=signature_state,
    )
    return _activate_and_record(
        config,
        name,
        resolved_version,
        body,
        payload["content_hash"],
        scope,
        signature_state=signature_state,
        from_cache=False,
    )
