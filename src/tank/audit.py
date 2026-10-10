"""Tank's install audit log (`P4-02`): append-only JSONL at `~/.headjack/audit.jsonl`.

When a skill turns out to be wrong — or hostile — this is how you find out where it
went and what else came from the same source. One line per `install`, `activation`,
`eviction` and `verification_failure`:

    {"ts":"2026-10-06T09:12:03Z","event":"install","name":"k8ssandra-node-replace",
     "version":"1.2","content_hash":"sha256:...","signature_key":"ed25519:ops-2026",
     "source":"http://localhost:8000","actor":"alice","result":"ok"}

Verification failures are logged *before* the exception propagates — a rejected
install is the single most interesting line in the file, and it is the one a naive
"log on success only" implementation loses. `verification_failure_guard` makes that
order structural rather than a convention callers can forget: the log write happens
inside the `except` clause, which runs before the `raise` that follows it.

Never logs skill **bodies** — only hashes and identifiers — so the log stays safe to
hand to someone debugging an incident. Each append is a single `os.write` of one
already-newline-terminated line, serialised (across threads *and* processes) by an
`flock` on a sidecar lock file, which also sequences the size check that triggers
rotation. No read-modify-write of the log itself, ever — concurrent Tank instances
must not be able to truncate each other.
"""

from __future__ import annotations

import contextlib
import fcntl
import getpass
import json
import os
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

EventType = Literal["install", "activation", "eviction", "verification_failure"]

_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {"install", "activation", "eviction", "verification_failure"}
)

DEFAULT_AUDIT_LOG: Final[Path] = Path.home() / ".headjack" / "audit.jsonl"

# Rotate at 10 MB, keep exactly one rotated generation (`.1`) — unbounded growth in
# a home directory is its own small incident.
_MAX_BYTES: Final[int] = 10 * 1024 * 1024


def _default_actor() -> str:
    """The OS login name, best-effort. Never raises — an audit log entry missing
    `actor` is far cheaper than one that crashes the install it's recording."""
    for var in ("USER", "USERNAME", "LOGNAME"):
        if value := os.environ.get(var):
            return value
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - exotic sandboxes with no pwd entry
        return "unknown"


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Hold an exclusive `flock` on a sidecar lock file for the duration of the
    block. Serialises both the rotate-check and the append across threads and
    separate processes — a plain `open(path, "a")` only gets atomicity from the
    kernel for a single `write(2)` call, and rotation (a rename plus starting a
    fresh file) is not a single syscall at all."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path(path)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _rotate_if_needed(path: Path, incoming_len: int, max_bytes: int) -> None:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    if size + incoming_len <= max_bytes:
        return
    rotated = path.with_name(path.name + ".1")
    os.replace(path, rotated)  # overwrites any existing .1 — "keep one"


def log_event(
    event: EventType,
    *,
    name: str,
    version: str | None = None,
    content_hash: str | None = None,
    signature_key: str | None = None,
    source: str | None = None,
    actor: str | None = None,
    result: str,
    path: Path = DEFAULT_AUDIT_LOG,
    max_bytes: int = _MAX_BYTES,
) -> None:
    """Append one audit line for `event` on skill `name`. Raises `ValueError` for
    an unrecognised `event` — every other failure (a missing parent directory, a
    first write to a fresh file) is handled rather than raised, since a tool call
    that loses its audit trail to a `FileNotFoundError` is worse than one with a
    slightly late directory creation.

    Only ever writes `name`/`version`/`content_hash`/`signature_key`/`source`/
    `actor`/`result` — there is no parameter through which a skill's body text
    could reach this function, let alone the file it writes.
    """
    if event not in _EVENT_TYPES:
        raise ValueError(f"unknown audit event type: {event!r}")

    record = {
        "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": event,
        "name": name,
        "version": version,
        "content_hash": content_hash,
        "signature_key": signature_key,
        "source": source,
        "actor": actor if actor is not None else _default_actor(),
        "result": result,
    }
    line = json.dumps(record, sort_keys=True) + "\n"
    encoded = line.encode()

    with _locked(path):
        _rotate_if_needed(path, len(encoded), max_bytes)
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)


@contextlib.contextmanager
def verification_failure_guard(
    *,
    name: str,
    version: str | None = None,
    content_hash: str | None = None,
    signature_key: str | None = None,
    source: str | None = None,
    actor: str | None = None,
    path: Path = DEFAULT_AUDIT_LOG,
) -> Iterator[None]:
    """Wrap a verification step: any exception raised inside the block is logged
    as a `verification_failure` — with `result` set to `str(exc)` — before it is
    re-raised. Makes "logged before the exception propagates" structural: the log
    write is the first thing the `except` clause does, and `raise` only runs
    after it returns.
    """
    try:
        yield
    except Exception as exc:
        log_event(
            "verification_failure",
            name=name,
            version=version,
            content_hash=content_hash,
            signature_key=signature_key,
            source=source,
            actor=actor,
            result=str(exc),
            path=path,
        )
        raise


def query(
    path: Path = DEFAULT_AUDIT_LOG,
    *,
    name: str | None = None,
    since: datetime | str | None = None,
) -> list[dict[str, Any]]:
    """Read `path`, returning entries matching `name` and/or `since` (inclusive),
    newest-last in file order. A corrupt line is skipped with a `UserWarning`
    rather than raised — a single damaged line must not make the rest of the log
    unreadable.
    """
    if not path.exists():
        return []

    since_dt = _parse_since(since) if since is not None else None

    entries: list[dict[str, Any]] = []
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
            if since_dt is not None and _parse_since(entry.get("ts")) < since_dt:
                continue
        except (json.JSONDecodeError, TypeError, ValueError):
            warnings.warn(f"{path}:{lineno}: skipping corrupt audit log line", stacklevel=2)
            continue
        if name is not None and entry.get("name") != name:
            continue
        entries.append(entry)
    return entries


def _parse_since(value: datetime | str | None) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise TypeError(f"expected datetime or ISO 8601 string, got {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt
