"""MCP stdio smoke: build the Tank server without hanging on run()."""

from __future__ import annotations

import pytest


def test_build_server_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TANK_TOKEN", "smoke-token")
    monkeypatch.setenv("TANK_HEADJACK_URL", "https://api.example.test")
    # Clear optional overrides that might be set in a developer shell.
    monkeypatch.delenv("TANK_SKILLS_ROOT", raising=False)

    from tank.config import TankConfig
    from tank.server import build_server

    config = TankConfig.from_env()
    assert config.token == "smoke-token"
    assert str(config.headjack_url).rstrip("/") == "https://api.example.test"

    server = build_server(config)
    assert server is not None
    # Tool registration must include the core skill_* tools without starting stdio.
    tool_names = {t.name for t in server._tool_manager.list_tools()}
    assert "skill_search" in tool_names
    assert "skill_install" in tool_names


def test_tank_main_module_importable() -> None:
    import tank.main as tank_main

    assert callable(tank_main.main)


def test_headjack_help_does_not_need_network() -> None:
    """Sanity: importing the CLI entry and asking for help stays offline."""
    from headjack.cli import main

    assert main([]) == 0
