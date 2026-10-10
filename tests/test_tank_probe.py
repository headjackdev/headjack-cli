"""Tests for tank.tools.probe.skill_probe."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from tank.client import HeadjackUnavailable
from tank.config import TankConfig
from tank.tools.probe import skill_probe

_ARTIFACTS = [
    {
        "kind": "skill",
        "name": "cassandra-operator",
        "version": "1.0",
        "description": "Diagnose a Cassandra node that fails to rejoin.",
        "systems": ["cassandra"],
        "content_hash": "aaa",
    },
    {
        "kind": "skill",
        "name": "medusa-restore",
        "version": None,
        "description": "Restore a Cassandra backup.",
        "systems": ["cassandra"],
        "content_hash": "bbb",
    },
]


class _FakeClient:
    def __init__(
        self, response: httpx.Response | None = None, *, unavailable: bool = False
    ) -> None:
        self._response = response
        self._unavailable = unavailable

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        if self._unavailable:
            raise HeadjackUnavailable("simulated")
        assert self._response is not None
        return self._response


def _config(tmp_path: Path) -> TankConfig:
    return TankConfig(
        token="t",
        store=tmp_path / "store",
        skills_root=tmp_path / "skills",
        mirror_path=tmp_path / "mirror.db",
    )


def test_no_probes_returns_a_clear_message_without_syncing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    client = _FakeClient(httpx.Response(200, json=_ARTIFACTS))
    assert "at least one probe" in skill_probe(client, config, [])


def test_probe_reports_counts_and_names(tmp_path: Path) -> None:
    config = _config(tmp_path)
    client = _FakeClient(httpx.Response(200, json=_ARTIFACTS))
    out = skill_probe(client, config, ["cassandra"])
    assert "2 artifacts" in out
    assert "cassandra" in out
    assert "cassandra-operator" in out
    assert "medusa-restore" in out


def test_probe_shows_related_skills_for_the_top_hit(tmp_path: Path) -> None:
    config = _config(tmp_path)
    client = _FakeClient(httpx.Response(200, json=_ARTIFACTS))
    out = skill_probe(client, config, ["cassandra-operator"])
    assert "related to cassandra-operator" in out
    assert "medusa-restore" in out


def test_probe_renders_a_loud_zero_with_mirror_age(tmp_path: Path) -> None:
    config = _config(tmp_path)
    client = _FakeClient(httpx.Response(200, json=_ARTIFACTS))
    out = skill_probe(client, config, ["kafka"])
    assert "kafka" in out
    zero_line = next(line for line in out.splitlines() if line.strip().startswith("kafka"))
    assert "0 (mirror" in zero_line


def test_probe_works_with_headjack_unreachable_using_an_empty_mirror(tmp_path: Path) -> None:
    config = _config(tmp_path)
    client = _FakeClient(unavailable=True)
    out = skill_probe(client, config, ["anything"])
    assert "never synced" in out
    assert "0 artifacts" in out


def test_probe_serves_stale_mirror_when_headjack_goes_unreachable_later(tmp_path: Path) -> None:
    config = _config(tmp_path)
    warm_client = _FakeClient(httpx.Response(200, json=_ARTIFACTS))
    skill_probe(warm_client, config, ["cassandra"])

    dead_client = _FakeClient(unavailable=True)
    out = skill_probe(dead_client, config, ["cassandra"])
    assert "cassandra-operator" in out


def test_many_probes_in_one_call(tmp_path: Path) -> None:
    config = _config(tmp_path)
    client = _FakeClient(httpx.Response(200, json=_ARTIFACTS))
    out = skill_probe(client, config, ["cassandra", "kafka", "BackoffLimitExceeded"])
    lines = out.splitlines()
    assert len(lines) >= 4  # header + 3 probes (plus a related line)


_DUPLICATED = [
    dict(_ARTIFACTS[0], version="2.2.0", content_hash="newer"),
    dict(_ARTIFACTS[0], version="1.0.0", content_hash="older"),
]


def test_probe_answers_when_the_catalog_has_duplicate_names(tmp_path: Path) -> None:
    # The live failure: a catalog carrying two versions of one artifact made
    # mirror.sync raise IntegrityError straight out of skill_probe, so every call
    # died — one probe string or four, before any of them was looked at.
    config = _config(tmp_path)
    client = _FakeClient(httpx.Response(200, json=_DUPLICATED))

    out = skill_probe(client, config, ["cassandra"])

    assert "cassandra-operator" in out


def test_probe_still_answers_locally_when_the_refresh_fails(tmp_path: Path) -> None:
    config = _config(tmp_path)
    skill_probe(_FakeClient(httpx.Response(200, json=_ARTIFACTS)), config, ["cassandra"])

    # ttl 0 so the second call actually attempts the refresh rather than skipping
    # it as fresh — otherwise this passes without exercising anything.
    config = config.model_copy(update={"mirror_ttl_hours": 0.0})
    broken = _FakeClient(httpx.Response(200, json={"detail": "not a list"}))
    out = skill_probe(broken, config, ["cassandra"])

    assert "cassandra-operator" in out  # served from the mirror we already had


def test_probe_says_so_when_the_mirror_is_stale_after_trying_to_refresh(
    tmp_path: Path,
) -> None:
    # A mirror frozen because every refresh fails reads exactly like a healthy
    # one, and its zero answers look like clean negatives. It has to say so.
    config = _config(tmp_path)
    config = config.model_copy(update={"mirror_ttl_hours": 0.0})
    unreachable = _FakeClient(unavailable=True)

    out = skill_probe(unreachable, config, ["cassandra"])

    assert "refresh failing" in out.splitlines()[0]


def test_probe_does_not_cry_wolf_on_a_fresh_mirror(tmp_path: Path) -> None:
    config = _config(tmp_path)
    out = skill_probe(_FakeClient(httpx.Response(200, json=_ARTIFACTS)), config, ["cassandra"])
    assert "refresh failing" not in out


# ----------------------------------------------- escalation when nothing matches (#191)


def test_zero_result_names_the_search_to_run_and_the_terms(tmp_path: Path) -> None:
    # The observed failure: three precise probes returned 0, which read as "nothing
    # there, carry on", and two strongly overlapping skills were nearly duplicated.
    # The next step belongs in the result, where it is already being read.
    config = _config(tmp_path)
    client = _FakeClient(httpx.Response(200, json=_ARTIFACTS))

    out = skill_probe(client, config, ["kafka rebalance", "istio mtls"])

    assert "skill_search" in out
    assert '"kafka rebalance"' in out
    assert '"istio mtls"' in out


def test_zero_result_says_what_a_zero_actually_proves(tmp_path: Path) -> None:
    config = _config(tmp_path)
    client = _FakeClient(httpx.Response(200, json=_ARTIFACTS))

    out = skill_probe(client, config, ["kafka rebalance"])

    assert "lexical" in out
    assert "not evidence" in out


def test_escalation_names_only_the_probes_that_found_nothing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    client = _FakeClient(httpx.Response(200, json=_ARTIFACTS))

    out = skill_probe(client, config, ["cassandra", "istio mtls"])

    assert '"istio mtls"' in out
    assert '"cassandra"' not in out  # it matched; nothing to escalate


def test_no_escalation_when_every_probe_matched(tmp_path: Path) -> None:
    config = _config(tmp_path)
    client = _FakeClient(httpx.Response(200, json=_ARTIFACTS))

    out = skill_probe(client, config, ["cassandra"])

    assert "skill_search" not in out
