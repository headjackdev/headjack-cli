"""Tests for tank.tools.search.skill_search.

`embedder` is always passed explicitly here rather than relying on
`resolve_embedder()`'s real import attempt - this dev environment likely has
`headjack[index]` installed, which would make the "unimportable" path untestable
and every test slow (a real model load) if tests depended on what happens to be on
PYTHONPATH. `resolve_embedder()` itself gets one minimal, separate test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest

from tank.client import HeadjackUnavailable
from tank.config import TankConfig
from tank.fencing import unfence
from tank.tools.search import resolve_embedder, skill_search

DIM = 384


class _FakeEmbedder:
    def encode_one(self, query: str) -> np.ndarray:
        return np.full(DIM, 0.1, dtype=np.float32)


class _FakeClient:
    """Duck-types `HeadjackClient`'s one method `skill_search` calls. Returning a
    real `httpx.Response` (not a hand-rolled stand-in) means `.status_code` and
    `.json()` behave exactly as the real client's would."""

    def __init__(
        self, response: httpx.Response | None = None, *, unavailable: bool = False
    ) -> None:
        self._response = response
        self._unavailable = unavailable
        self.last_body: dict[str, Any] | None = None

    def post(self, path: str, json: dict[str, Any]) -> httpx.Response:
        self.last_body = json
        if self._unavailable:
            raise HeadjackUnavailable("simulated")
        assert self._response is not None
        return self._response


def _response(status_code: int, payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        status_code, json=payload, request=httpx.Request("POST", "http://x/v1/search")
    )


@pytest.fixture
def config(tmp_path: Path) -> TankConfig:
    return TankConfig(token="t", skills_root=tmp_path / "skills", store=tmp_path / "store")


RESULTS_PAYLOAD = {
    "results": [
        {
            "name": "cassandra-operations",
            "kind": "runbook",
            "version": None,
            "installable": False,
            "source": "repo/docs/runbooks/cassandra-operations.md",
            "matched_in": "§ B — Hints are piling up",
            "snippet": "run nodetool disablehints before the alter",
            "score": 3.0,
            "reviewed": False,
        },
        {
            "name": "k8ssandra-node-replace",
            "kind": "skill",
            "version": "1.2",
            "installable": True,
            "source": "repo/skills/k8ssandra-node-replace/SKILL.md",
            "matched_in": "§ Replacing a dead node",
            "snippet": "K8SeedProvider3x must be updated before the pod is deleted",
            "score": 2.0,
            "reviewed": True,
        },
    ]
}


# --------------------------------------------------------------------------- shape


def test_installable_result_shows_the_exact_install_call(config: TankConfig) -> None:
    client = _FakeClient(_response(200, RESULTS_PAYLOAD))
    out = skill_search(client, config, "hints piling up")  # type: ignore[arg-type]
    assert 'skill_install("k8ssandra-node-replace")' in out


def test_non_installable_result_shows_reference_only_and_path(config: TankConfig) -> None:
    client = _FakeClient(_response(200, RESULTS_PAYLOAD))
    out = skill_search(client, config, "hints piling up")  # type: ignore[arg-type]
    assert "reference only" in out
    assert "repo/docs/runbooks/cassandra-operations.md" in out


def test_unreviewed_result_is_flagged(config: TankConfig) -> None:
    unreviewed_payload = {"results": [{**RESULTS_PAYLOAD["results"][1], "reviewed": False}]}
    client = _FakeClient(_response(200, unreviewed_payload))
    out = skill_search(client, config, "x")  # type: ignore[arg-type]
    assert "unreviewed" in out


def test_output_under_1200_chars_for_five_results(config: TankConfig) -> None:
    five = {"results": [RESULTS_PAYLOAD["results"][1]] * 5}
    client = _FakeClient(_response(200, five))
    out = skill_search(client, config, "x")  # type: ignore[arg-type]
    assert len(out) <= 1200


def test_zero_results_is_short_and_actionable(config: TankConfig) -> None:
    client = _FakeClient(_response(200, {"results": []}))
    out = skill_search(client, config, "a very specific query")  # type: ignore[arg-type]
    assert "a very specific query" in out
    assert len(out) < 200


# ------------------------------------------------------------------------- fencing


def test_snippet_is_fenced_with_the_result_name_and_an_honest_unverified_hash(
    config: TankConfig,
) -> None:
    """P4-05: a snippet is a body excerpt and gets the same fencing treatment as a
    full installed body. `/v1/search` never resolves or verifies a content hash for
    a preview result, so the fence must say so honestly rather than fabricate one."""
    client = _FakeClient(_response(200, RESULTS_PAYLOAD))
    out = skill_search(client, config, "hints piling up")  # type: ignore[arg-type]

    assert "[ARTIFACT boundary=" in out
    assert "[END ARTIFACT boundary=" in out
    assert "name=cassandra-operations" in out
    assert "sha256=unverified" in out
    # The raw snippet text itself still appears, unaltered.
    assert "run nodetool disablehints before the alter" in out


def test_fenced_snippet_round_trips_to_the_exact_raw_snippet(config: TankConfig) -> None:
    one_result = {"results": [RESULTS_PAYLOAD["results"][0]]}
    client = _FakeClient(_response(200, one_result))
    out = skill_search(client, config, "hints piling up")  # type: ignore[arg-type]

    # _render() indents every line of the fenced block by 3 spaces; strip that
    # back off to recover the exact fenced text unfence() expects, then slice out
    # just the fenced block (from its header to the end of its footer line).
    start = out.index("[ARTIFACT boundary=")
    end = out.index("]", out.index("[END ARTIFACT boundary=")) + 1
    fenced_block = "\n".join(line.removeprefix("   ") for line in out[start:end].split("\n"))

    assert unfence(fenced_block) == RESULTS_PAYLOAD["results"][0]["snippet"]


# --------------------------------------------------------------------- degradation


def test_server_unreachable_lists_installed_skills(config: TankConfig) -> None:
    (config.skills_root / "foo").mkdir(parents=True)
    (config.skills_root / "foo" / "SKILL.md").write_text("---\nname: foo\n---\nbody")
    (config.skills_root / "bar").mkdir(parents=True)
    (config.skills_root / "bar" / "SKILL.md").write_text("---\nname: bar\n---\nbody")

    client = _FakeClient(unavailable=True)
    out = skill_search(client, config, "x")  # type: ignore[arg-type]
    assert "unreachable" in out
    assert "foo" in out
    assert "bar" in out


def test_server_unreachable_with_nothing_installed(config: TankConfig) -> None:
    client = _FakeClient(unavailable=True)
    out = skill_search(client, config, "x")  # type: ignore[arg-type]
    assert "unreachable" in out
    assert "nothing is installed" in out


def test_401_names_the_token_not_a_traceback(config: TankConfig) -> None:
    client = _FakeClient(_response(401, {"detail": "unauthorized"}))
    out = skill_search(client, config, "x")  # type: ignore[arg-type]
    assert "TANK_TOKEN" in out
    assert "Traceback" not in out


def test_403_does_not_raise(config: TankConfig) -> None:
    client = _FakeClient(_response(403, {"detail": "forbidden"}))
    out = skill_search(client, config, "x")  # type: ignore[arg-type]
    assert "403" in out


# ------------------------------------------------------------------------ embedding


def test_without_embedder_query_vector_is_null(config: TankConfig) -> None:
    client = _FakeClient(_response(200, {"results": []}))
    skill_search(client, config, "x", embedder=None)  # type: ignore[arg-type]
    assert client.last_body is not None
    assert client.last_body["query_vector"] is None


def test_with_embedder_query_vector_matches_encode_one(config: TankConfig) -> None:
    client = _FakeClient(_response(200, {"results": []}))
    fake = _FakeEmbedder()
    skill_search(client, config, "x", embedder=fake)  # type: ignore[arg-type]
    assert client.last_body is not None
    assert client.last_body["query_vector"] == fake.encode_one("x").tolist()


def test_resolve_embedder_never_raises() -> None:
    result = resolve_embedder()
    assert result is None or hasattr(result, "encode_one")


# ------------------------------------ telling partial overlap from coverage (#191)


def test_section_match_says_it_is_one_section_not_the_whole_skill(
    config: TankConfig,
) -> None:
    # A hit inside one section of a larger skill is the "overlaps in part" case.
    # Rendering the bare heading left that to be inferred, and it wasn't.
    client = _FakeClient(_response(200, RESULTS_PAYLOAD))
    out = skill_search(client, config, "hinted handoff")
    assert "one section, not the whole skill" in out


def test_section_heading_is_not_rendered_with_a_doubled_marker(
    config: TankConfig,
) -> None:
    client = _FakeClient(_response(200, RESULTS_PAYLOAD))
    out = skill_search(client, config, "hinted handoff")
    assert "§ §" not in out
    assert "§ Replacing a dead node" in out


def test_header_says_results_are_ranked_not_scored_for_coverage(
    config: TankConfig,
) -> None:
    # The served pipeline never reranks (#192), so position 1 means "closest of
    # these", never "covers your topic". Claiming otherwise is what let a near-miss
    # pass for full coverage.
    client = _FakeClient(_response(200, RESULTS_PAYLOAD))
    out = skill_search(client, config, "hinted handoff")
    assert "not scored for coverage" in out.splitlines()[0]
