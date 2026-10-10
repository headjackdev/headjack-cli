"""Tests for tank.server, tank.config, tank.client.

The import-graph test runs in a subprocess rather than checking `sys.modules`
in-process — other tests in this same pytest run (e.g. test_embed.py) may have
already imported sentence_transformers for unrelated reasons, which would make an
in-process check meaningless. A fresh interpreter is the only way to get a true
answer to "does importing tank.server pull in headjack or an ML library".
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from tank.client import HeadjackClient, HeadjackUnavailable
from tank.config import TankConfig, TankConfigError
from tank.server import build_server

REPO_ROOT = Path(__file__).parent.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def dead_port_config(tmp_path: Path) -> TankConfig:
    return TankConfig(
        token="test-token",
        headjack_url=f"http://127.0.0.1:{_free_port()}",
        store=tmp_path / "store",
        skills_root=tmp_path / "skills",
        timeout_s=0.5,
    )


# ---- registration -------------------------------------------------------------------


def test_six_tools_register_with_spec_descriptions(dead_port_config: TankConfig) -> None:
    server = build_server(dead_port_config)
    tools = {t.name: t for t in server._tool_manager.list_tools()}
    assert set(tools) == {
        "skill_search",
        "skill_probe",
        "skill_save",
        "skill_install",
        "skill_list_installed",
        "skill_evict",
    }
    # "Always invoke for ..." is the one documented, measured wording rule in this
    # whole backlog (P3-08: n=20,020, 46.3% -> 67.3% micro-recall at 0.14% FP cost)
    # — both tools must carry it, not just skill_search.
    assert "Always invoke" in tools["skill_search"].description
    assert "Always invoke" in tools["skill_probe"].description
    # P3-09: Tank does the saving, never the function — the human-readable half
    # of the tool description must not leak the MCP tool id itself.
    assert "skill_save" not in tools["skill_save"].description
    # Tier 0 is all the model sees before the skill is loaded, so the probe
    # trigger has to be scoped to the user's whole catalog rather than to one
    # kind of work — "operational" is what made a model skip the check on
    # anything that didn't feel like infra.
    probe = tools["skill_probe"].description.lower()
    assert "whatever its topic" in probe
    assert "operational" not in probe


def test_probe_description_denies_that_a_zero_licenses_skipping_the_search(
    dead_port_config: TankConfig,
) -> None:
    # A bare 0 reads as a clean negative. It is not one: the mirror matches the
    # probe as a literal FTS5 phrase, so 0 means "this wording is absent", not
    # "the catalog lacks this topic" (#191).
    server = build_server(dead_port_config)
    tools = {t.name: t for t in server._tool_manager.list_tools()}
    probe = tools["skill_probe"].description

    assert "lexical" in probe
    assert "no paraphrase" in probe
    assert "never licenses skipping the search before a save" in probe


def test_save_description_asks_the_overlap_question_at_the_point_of_writing(
    dead_port_config: TankConfig,
) -> None:
    # Carried on skill_save itself, not only two tools upstream: by the time the
    # save is being called, the probe's guidance is far behind in the transcript.
    server = build_server(dead_port_config)
    tools = {t.name: t for t in server._tool_manager.list_tools()}
    save = tools["skill_save"].description

    assert "Before creating a NEW skill" in save
    assert "read the hits themselves rather than their titles" in save


def test_server_starts_with_dead_port(dead_port_config: TankConfig) -> None:
    server = build_server(dead_port_config)  # must not raise or attempt a connection
    assert server is not None


def test_store_directory_created_with_mode_0700(dead_port_config: TankConfig) -> None:
    build_server(dead_port_config)
    mode = dead_port_config.store.stat().st_mode & 0o777
    assert mode == 0o700


# ---- config ---------------------------------------------------------------------------


def test_missing_tank_token_raises_naming_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TANK_TOKEN", raising=False)
    with pytest.raises(TankConfigError, match="TANK_TOKEN"):
        TankConfig.from_env()


def test_config_from_env_reads_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TANK_TOKEN", "abc123")
    monkeypatch.setenv("TANK_HEADJACK_URL", "http://example.invalid:9000")
    monkeypatch.setenv("TANK_STORE", str(tmp_path / "custom-store"))
    config = TankConfig.from_env()
    assert config.token == "abc123"
    assert config.headjack_url == "http://example.invalid:9000"
    assert config.store == tmp_path / "custom-store"


# ---- degradation: timeouts and dead connections become tool results, not tracebacks --


def test_stub_tool_reports_unreachable_headjack_without_raising(
    dead_port_config: TankConfig,
) -> None:
    # skill_search, skill_probe, skill_install and skill_list_installed are
    # implemented and have their own degradation (or, for skill_list_installed,
    # offline-only) tests (test_tank_search.py, test_tank_install.py,
    # test_manifest.py); skill_evict remains the last stub and still exercises
    # this generic "a stub still makes a real /healthz call" contract from P3-01.
    server = build_server(dead_port_config)
    tools = {t.name: t for t in server._tool_manager.list_tools()}
    result = tools["skill_evict"].fn()
    assert "not reachable" in result
    assert "not implemented yet" in result


def test_client_connect_error_becomes_headjack_unavailable(tmp_path: Path) -> None:
    config = TankConfig(
        token="t",
        headjack_url=f"http://127.0.0.1:{_free_port()}",
        store=tmp_path,
        skills_root=tmp_path,
        timeout_s=0.5,
    )
    client = HeadjackClient(config)
    with pytest.raises(HeadjackUnavailable):
        client.get("/healthz")


def test_every_transport_failure_becomes_headjack_unavailable(tmp_path: Path) -> None:
    # Only ConnectError and TimeoutException were wrapped, so a dropped read or a
    # protocol-level fault propagated out of whichever tool happened to be running
    # — exactly what HeadjackUnavailable exists to prevent.
    config = TankConfig(token="t", store=tmp_path, skills_root=tmp_path)
    for failure in (
        httpx.ReadError("connection reset"),
        httpx.RemoteProtocolError("server disconnected"),
        httpx.ProxyError("proxy refused"),
    ):
        client = HeadjackClient(config)
        client._http = httpx.Client(transport=httpx.MockTransport(_raising(failure)))
        with pytest.raises(HeadjackUnavailable):
            client.get("/healthz")


def _raising(exc: Exception) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


# ---- import graph: the default install must stay dependency-free --------------------


def test_default_install_never_imports_headjack_or_ml_libraries() -> None:
    script = (
        "import tank.server, tank.config, tank.client, tank.main\n"
        "import sys\n"
        "forbidden = ['headjack', 'torch', 'sentence_transformers', 'sqlite_vec']\n"
        "leaked = [m for m in forbidden if m in sys.modules]\n"
        "assert not leaked, f'forbidden modules imported: {leaked}'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


# ---- the alwaysLoad registration setting ---------------------------------------------


def test_example_mcp_config_sets_always_load_with_rationale() -> None:
    config_path = REPO_ROOT / "docs" / "tank-mcp-config.example.json"
    payload = json.loads(config_path.read_text())
    assert payload["mcpServers"]["tank"]["alwaysLoad"] is True
    assert "Tool Search" in payload["_comment"]
