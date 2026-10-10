"""Tests for headjack.install: wiring tank's MCP registration and gateway skill
together behind one `headjack install` command."""

from __future__ import annotations

from pathlib import Path

import pytest

from headjack.cli import main as headjack_main
from headjack.install import install


def test_claude_registers_mcp_and_installs_gateway_under_default_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    mcp_path, gateway_path, workflow_path = install(
        "claude", "http://127.0.0.1:8000", "tok", project_dir, scope="project"
    )

    assert mcp_path == project_dir / ".mcp.json"
    assert mcp_path.exists()
    assert gateway_path == fake_home / ".claude" / "skills" / "tank" / "SKILL.md"
    assert gateway_path.exists()
    assert workflow_path == fake_home / ".claude" / "skills" / "tank-workflow" / "SKILL.md"
    assert workflow_path.exists()


def test_cursor_uses_the_observed_skills_cursor_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    _mcp_path, gateway_path, workflow_path = install(
        "cursor", "http://127.0.0.1:8000", "tok", project_dir, scope="project"
    )

    assert gateway_path == fake_home / ".cursor" / "skills-cursor" / "tank" / "SKILL.md"
    assert workflow_path == fake_home / ".cursor" / "skills-cursor" / "tank-workflow" / "SKILL.md"


def test_skills_root_override_is_honoured(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    custom_root = tmp_path / "custom-skills"

    _mcp_path, gateway_path, workflow_path = install(
        "claude",
        "http://127.0.0.1:8000",
        "tok",
        project_dir,
        skills_root=custom_root,
        scope="project",
    )

    assert gateway_path == custom_root / "tank" / "SKILL.md"
    assert workflow_path == custom_root / "tank-workflow" / "SKILL.md"


def test_unknown_client_raises_before_any_write(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown client"):
        install("vscode", "http://x", "t", tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_cli_prints_both_written_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import headjack.cliconfig as cliconfig

    config_path = tmp_path / "config.toml"
    config_path.write_text('token = "tok"\n')
    monkeypatch.setattr(cliconfig, "CONFIG_PATH", config_path)
    monkeypatch.setenv("HEADJACK_REMOTE", "http://127.0.0.1:8000")

    exit_code = headjack_main(
        [
            "install",
            "--client",
            "claude",
            "--scope",
            "project",
            "--project-dir",
            str(tmp_path),
            "--skills-root",
            str(tmp_path / "skills"),
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert str(tmp_path / ".mcp.json") in out
    assert str(tmp_path / "skills" / "tank" / "SKILL.md") in out


# --------------------------------------------------------------------- config file


def test_cli_falls_back_to_config_file_for_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import headjack.cliconfig as cliconfig

    config_path = tmp_path / "config.toml"
    config_path.write_text('token = "tok-from-config"\n')
    monkeypatch.setattr(cliconfig, "CONFIG_PATH", config_path)
    monkeypatch.setenv("HEADJACK_REMOTE", "http://127.0.0.1:8000")

    exit_code = headjack_main(
        [
            "install",
            "--client",
            "claude",
            "--scope",
            "project",
            "--project-dir",
            str(tmp_path),
            "--skills-root",
            str(tmp_path / "skills"),
        ]
    )
    assert exit_code == 0


def test_cli_remote_env_var_overrides_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import headjack.cliconfig as cliconfig

    config_path = tmp_path / "config.toml"
    config_path.write_text('token = "tok"\n')
    monkeypatch.setattr(cliconfig, "CONFIG_PATH", config_path)
    monkeypatch.setenv("HEADJACK_REMOTE", "http://local-dev-server")

    exit_code = headjack_main(
        [
            "install",
            "--client",
            "claude",
            "--scope",
            "project",
            "--project-dir",
            str(tmp_path),
            "--skills-root",
            str(tmp_path / "skills"),
        ]
    )
    assert exit_code == 0
    mcp_config = (tmp_path / ".mcp.json").read_text()
    assert "http://local-dev-server" in mcp_config


def test_cli_errors_clearly_with_no_token_and_no_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import headjack.cliconfig as cliconfig

    monkeypatch.setattr(cliconfig, "CONFIG_PATH", tmp_path / "nonexistent.toml")
    with pytest.raises(SystemExit) as exc_info:
        headjack_main(
            [
                "install",
                "--client",
                "claude",
                "--project-dir",
                str(tmp_path),
                "--skills-root",
                str(tmp_path / "skills"),
            ]
        )
    assert exc_info.value.code == 2
    assert "no token configured" in capsys.readouterr().err
