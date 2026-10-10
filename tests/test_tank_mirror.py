"""Tests for tank.mirror: the local catalog mirror sync, lexical probe search,
and the shared-systems-tag edge graph.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from tank import mirror
from tank.client import HeadjackUnavailable
from tank.config import TankConfig

_ARTIFACTS = [
    {
        "kind": "skill",
        "name": "cassandra-operator",
        "version": "1.0",
        "description": "Diagnose a Cassandra node that fails to rejoin.",
        "systems": ["cassandra", "k8ssandra"],
        "content_hash": "aaa",
    },
    {
        "kind": "skill",
        "name": "medusa-restore",
        "version": None,
        "description": "Restore a Cassandra backup via Medusa.",
        "systems": ["cassandra"],
        "content_hash": "bbb",
    },
    {
        "kind": "skill",
        "name": "kafka-lag",
        "version": None,
        "description": "Diagnose Kafka consumer lag.",
        "systems": ["kafka"],
        "content_hash": "ccc",
    },
]


class _FakeClient:
    def __init__(
        self, response: httpx.Response | None = None, *, unavailable: bool = False
    ) -> None:
        self._response = response
        self._unavailable = unavailable
        self.calls = 0

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        self.calls += 1
        if self._unavailable:
            raise HeadjackUnavailable("simulated")
        assert self._response is not None
        return self._response


def _config(tmp_path: Path, *, ttl_hours: float = 6.0) -> TankConfig:
    return TankConfig(
        token="t",
        store=tmp_path / "store",
        skills_root=tmp_path / "skills",
        mirror_path=tmp_path / "mirror.db",
        mirror_ttl_hours=ttl_hours,
    )


def test_sync_populates_catalog_from_the_response(tmp_path: Path) -> None:
    config = _config(tmp_path)
    client = _FakeClient(httpx.Response(200, json=_ARTIFACTS))

    assert mirror.sync(client, config) is True

    conn = mirror.connect(config.mirror_path)
    assert mirror.total_count(conn) == 3
    assert mirror.fetched_at(conn) is not None


def test_sync_computes_bidirectional_shared_system_edges(tmp_path: Path) -> None:
    config = _config(tmp_path)
    client = _FakeClient(httpx.Response(200, json=_ARTIFACTS))
    mirror.sync(client, config)

    conn = mirror.connect(config.mirror_path)
    assert mirror.related(conn, "skill", "cassandra-operator", 5) == ["medusa-restore"]
    assert mirror.related(conn, "skill", "medusa-restore", 5) == ["cassandra-operator"]
    # kafka-lag shares no system with anything
    assert mirror.related(conn, "skill", "kafka-lag", 5) == []


def test_sync_skips_when_not_stale_and_not_forced(tmp_path: Path) -> None:
    config = _config(tmp_path, ttl_hours=6.0)
    client = _FakeClient(httpx.Response(200, json=_ARTIFACTS))
    assert mirror.sync(client, config) is True
    assert mirror.sync(client, config) is False
    assert client.calls == 1


def test_sync_force_ignores_ttl(tmp_path: Path) -> None:
    config = _config(tmp_path, ttl_hours=6.0)
    client = _FakeClient(httpx.Response(200, json=_ARTIFACTS))
    mirror.sync(client, config)
    assert mirror.sync(client, config, force=True) is True
    assert client.calls == 2


def test_sync_unreachable_leaves_existing_mirror_untouched(tmp_path: Path) -> None:
    config = _config(tmp_path)
    client = _FakeClient(httpx.Response(200, json=_ARTIFACTS))
    mirror.sync(client, config)

    stale_client = _FakeClient(unavailable=True)
    assert mirror.sync(stale_client, config, force=True) is False

    conn = mirror.connect(config.mirror_path)
    assert mirror.total_count(conn) == 3


def test_search_matches_as_a_literal_phrase_not_an_fts_query(tmp_path: Path) -> None:
    config = _config(tmp_path)
    client = _FakeClient(httpx.Response(200, json=_ARTIFACTS))
    mirror.sync(client, config)
    conn = mirror.connect(config.mirror_path)

    hits = mirror.search(conn, "cassandra")
    names = {h["name"] for h in hits}
    assert names == {"cassandra-operator", "medusa-restore"}

    # Characters that are FTS5 operators/syntax must not raise or be interpreted.
    assert mirror.search(conn, 'cassandra" OR *:1') == []


def test_age_str_reports_never_synced_before_any_sync(tmp_path: Path) -> None:
    config = _config(tmp_path)
    conn = mirror.connect(config.mirror_path)
    assert mirror.age_str(conn) == "never synced"


def test_age_str_reports_recent_after_a_sync(tmp_path: Path) -> None:
    config = _config(tmp_path)
    client = _FakeClient(httpx.Response(200, json=_ARTIFACTS))
    mirror.sync(client, config)
    conn = mirror.connect(config.mirror_path)
    assert mirror.age_str(conn).endswith("m old")


def test_sync_error_response_does_not_raise_or_write(tmp_path: Path) -> None:
    config = _config(tmp_path)
    client = _FakeClient(httpx.Response(500, json={"detail": "boom"}))
    assert mirror.sync(client, config) is False
    conn = mirror.connect(config.mirror_path)
    assert mirror.total_count(conn) == 0


# ------------------------------------------- a catalog carrying several versions

# The shape that broke a live mirror: a bulk re-import added a 1.0.0 row beside an
# existing 2.x one, so /v1/artifacts returned the same (kind, name) twice. The
# mirror's UNIQUE (kind, name) made every subsequent sync raise IntegrityError out
# of skill_probe, which froze the catalog for a day and a half.
_VERSIONED = [
    {
        "kind": "skill",
        "name": "ai-seo",
        "version": "2.2.0",
        "description": "The current one.",
        "systems": ["marketing"],
        "content_hash": "newer",
        "updated_at": "2026-10-07T14:23:06+00:00",
    },
    {
        "kind": "skill",
        "name": "ai-seo",
        "version": "1.0.0",
        "description": "An older version re-imported later.",
        "systems": ["marketing"],
        "content_hash": "older",
        "updated_at": "2026-10-09T19:43:17+00:00",
    },
]


def test_sync_keeps_one_row_per_name_when_the_catalog_has_several_versions(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    client = _FakeClient(httpx.Response(200, json=_VERSIONED))

    assert mirror.sync(client, config) is True

    conn = mirror.connect(config.mirror_path)
    assert mirror.total_count(conn) == 1


def test_sync_prefers_the_highest_version_not_the_most_recently_written(
    tmp_path: Path,
) -> None:
    # The older row was written later, so "last one wins" would pick 1.0.0 — the
    # mirror would describe the catalog's current artifact with superseded text.
    config = _config(tmp_path)
    client = _FakeClient(httpx.Response(200, json=_VERSIONED))
    mirror.sync(client, config)

    conn = mirror.connect(config.mirror_path)
    row = conn.execute("SELECT version, content_hash FROM catalog").fetchone()
    assert row["version"] == "2.2.0"
    assert row["content_hash"] == "newer"


def test_sync_survives_a_body_that_is_not_the_catalog_shape(tmp_path: Path) -> None:
    config = _config(tmp_path)
    mirror.sync(_FakeClient(httpx.Response(200, json=_ARTIFACTS)), config)

    # 200, valid JSON, wrong shape — nothing in the client layer catches this.
    broken = _FakeClient(httpx.Response(200, json={"detail": "not a list"}))
    assert mirror.sync(broken, config, force=True) is False

    conn = mirror.connect(config.mirror_path)
    assert mirror.total_count(conn) == 3  # previous mirror intact, not emptied


def test_sync_survives_an_artifact_missing_a_required_field(tmp_path: Path) -> None:
    config = _config(tmp_path)
    mirror.sync(_FakeClient(httpx.Response(200, json=_ARTIFACTS)), config)

    incomplete = _FakeClient(httpx.Response(200, json=[{"kind": "skill", "name": "x"}]))
    assert mirror.sync(incomplete, config, force=True) is False

    conn = mirror.connect(config.mirror_path)
    assert mirror.total_count(conn) == 3


def test_latest_per_name_sorts_versions_numerically_not_lexically(tmp_path: Path) -> None:
    # "10.0.0" < "9.0.0" as strings; the mirror must not pick the older one.
    artifacts = [
        {"kind": "skill", "name": "a", "version": "9.0.0", "content_hash": "nine"},
        {"kind": "skill", "name": "a", "version": "10.0.0", "content_hash": "ten"},
    ]
    (kept,) = mirror.latest_per_name(artifacts)
    assert kept["content_hash"] == "ten"


def test_latest_per_name_tolerates_a_missing_or_odd_version(tmp_path: Path) -> None:
    artifacts = [
        {"kind": "skill", "name": "a", "version": None, "content_hash": "none"},
        {"kind": "skill", "name": "a", "version": "1.2.0-rc1", "content_hash": "rc"},
    ]
    (kept,) = mirror.latest_per_name(artifacts)
    assert kept["content_hash"] == "rc"
