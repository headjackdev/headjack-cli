"""Tests for tank.audit — the install audit log (P4-02).

Real files on a real temporary path throughout, no mocked filesystem: concurrency
and rotation are exactly the kind of bug a mock hides. The threaded test is the one
place this file accepts a little flakiness risk in exchange for actually exercising
the `flock` path rather than asserting it exists.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tank.audit import log_event, query, verification_failure_guard


def _read_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


# --------------------------------------------------------------------------- events


@pytest.mark.parametrize("event", ["install", "activation", "eviction", "verification_failure"])
def test_all_four_event_types_logged(log_path: Path, event: str) -> None:
    log_event(
        event,  # type: ignore[arg-type]
        name="example",
        version="1.0",
        content_hash="sha256:abc",
        signature_key="ed25519:ops-2026",
        source="http://localhost:8000",
        actor="alice",
        result="ok",
        path=log_path,
    )

    (entry,) = _read_lines(log_path)
    assert entry["event"] == event
    assert entry["name"] == "example"
    assert entry["actor"] == "alice"
    assert entry["result"] == "ok"


def test_unknown_event_type_rejected(log_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown audit event type"):
        log_event("made_up", name="x", result="ok", path=log_path)  # type: ignore[arg-type]
    assert not log_path.exists()


def test_actor_defaults_when_not_supplied(log_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USER", "ci-runner")
    log_event("install", name="example", result="ok", path=log_path)
    (entry,) = _read_lines(log_path)
    assert entry["actor"] == "ci-runner"


def test_record_shape_matches_spec(log_path: Path) -> None:
    log_event(
        "install",
        name="k8ssandra-node-replace",
        version="1.2",
        content_hash="sha256:deadbeef",
        signature_key="ed25519:ops-2026",
        source="http://localhost:8000",
        actor="alice",
        result="ok",
        path=log_path,
    )
    (entry,) = _read_lines(log_path)
    assert set(entry) == {
        "ts",
        "event",
        "name",
        "version",
        "content_hash",
        "signature_key",
        "source",
        "actor",
        "result",
    }
    # ts is a real, parseable UTC timestamp
    datetime.fromisoformat(str(entry["ts"]).replace("Z", "+00:00"))


# --------------------------------------------------------------- verification order


def test_verification_failure_logged_before_raising(log_path: Path) -> None:
    class _Boom(Exception):
        pass

    with (
        pytest.raises(_Boom),
        verification_failure_guard(name="example", version="1.0", path=log_path),
    ):
        raise _Boom("content hash mismatch")

    (entry,) = _read_lines(log_path)
    assert entry["event"] == "verification_failure"
    assert entry["result"] == "content hash mismatch"


def test_verification_failure_guard_lets_success_through_silently(log_path: Path) -> None:
    with verification_failure_guard(name="example", path=log_path):
        pass  # no exception -> no log line

    assert not log_path.exists()


# ------------------------------------------------------------------------ no bodies


def test_no_skill_body_content_in_any_line(log_path: Path) -> None:
    secret_body = "SKILL.md body text containing SECRET_TOKEN=sk-super-secret-value"

    log_event(
        "install",
        name="example",
        version="1.0",
        content_hash="sha256:abc",
        result=f"stored ({len(secret_body)} bytes)",
        path=log_path,
    )

    raw = log_path.read_text()
    assert "SECRET_TOKEN" not in raw
    assert "sk-super-secret-value" not in raw
    assert secret_body not in raw


# ------------------------------------------------------------------- concurrency


def test_concurrent_appends_are_atomic_per_line(log_path: Path) -> None:
    n_threads = 16
    writes_per_thread = 20

    def worker(thread_id: int) -> None:
        for i in range(writes_per_thread):
            log_event(
                "install",
                name=f"thread-{thread_id}",
                version=str(i),
                result="ok",
                path=log_path,
            )

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = log_path.read_text().splitlines()
    assert len(lines) == n_threads * writes_per_thread

    # Every single line must parse as exactly one JSON object — interleaved writes
    # would corrupt a line (half of one record spliced with half of another), which
    # JSON parsing all of them catches regardless of ordering.
    seen = set()
    for line in lines:
        entry = json.loads(line)
        seen.add((entry["name"], entry["version"]))
    assert len(seen) == n_threads * writes_per_thread


# ---------------------------------------------------------------------- rotation


def test_rotation_at_max_bytes(log_path: Path) -> None:
    small_max = 500  # exercise the rotation path without writing 10 MB in a test

    for i in range(50):
        log_event(
            "install",
            name="example",
            version=str(i),
            result="ok",
            path=log_path,
            max_bytes=small_max,
        )

    rotated = log_path.with_name(log_path.name + ".1")
    assert rotated.exists()
    assert log_path.exists()
    # Current file never grows past the threshold by more than the newest line.
    assert log_path.stat().st_size < small_max + 400


def test_rotation_keeps_only_one_generation(log_path: Path) -> None:
    small_max = 300
    for i in range(80):
        log_event(
            "install",
            name="example",
            version=str(i),
            result="ok",
            path=log_path,
            max_bytes=small_max,
        )

    rotated_2 = log_path.with_name(log_path.name + ".2")
    assert not rotated_2.exists()


# ------------------------------------------------------------------------- query


def test_corrupt_line_skipped_with_warning(log_path: Path) -> None:
    log_event("install", name="good-1", result="ok", path=log_path)
    with log_path.open("a") as f:
        f.write("{not json at all\n")
    log_event("install", name="good-2", result="ok", path=log_path)

    with pytest.warns(UserWarning, match="corrupt"):
        entries = query(log_path)

    assert [e["name"] for e in entries] == ["good-1", "good-2"]


def test_query_by_name(log_path: Path) -> None:
    log_event("install", name="alpha", result="ok", path=log_path)
    log_event("install", name="beta", result="ok", path=log_path)
    log_event("activation", name="alpha", result="ok", path=log_path)

    entries = query(log_path, name="alpha")

    assert [e["event"] for e in entries] == ["install", "activation"]
    assert all(e["name"] == "alpha" for e in entries)


def test_query_by_since_date(log_path: Path) -> None:
    old_ts = (datetime.now(UTC) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with log_path.open("a") as f:
        f.write(
            json.dumps(
                {
                    "ts": old_ts,
                    "event": "install",
                    "name": "stale",
                    "version": None,
                    "content_hash": None,
                    "signature_key": None,
                    "source": None,
                    "actor": "alice",
                    "result": "ok",
                }
            )
            + "\n"
        )
    log_event("install", name="fresh", result="ok", path=log_path)

    since = datetime.now(UTC) - timedelta(days=1)
    entries = query(log_path, since=since)

    assert [e["name"] for e in entries] == ["fresh"]


def test_query_missing_file_returns_empty(tmp_path: Path) -> None:
    assert query(tmp_path / "does-not-exist.jsonl") == []


def test_query_since_accepts_bare_date_string(log_path: Path) -> None:
    old_ts = (datetime.now(UTC) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with log_path.open("a") as f:
        f.write(
            json.dumps(
                {
                    "ts": old_ts,
                    "event": "install",
                    "name": "stale",
                    "version": None,
                    "content_hash": None,
                    "signature_key": None,
                    "source": None,
                    "actor": "alice",
                    "result": "ok",
                }
            )
            + "\n"
        )
    log_event("install", name="fresh", result="ok", path=log_path)

    since_today = datetime.now(UTC).strftime("%Y-%m-%d")
    entries = query(log_path, since=since_today)

    assert [e["name"] for e in entries] == ["fresh"]


def test_query_without_since_tolerates_missing_ts(log_path: Path) -> None:
    with log_path.open("a") as f:
        f.write(json.dumps({"event": "install", "name": "no-ts", "result": "ok"}) + "\n")

    entries = query(log_path, name="no-ts")

    assert [e["name"] for e in entries] == ["no-ts"]
