"""Tests for tank.register and the `tank register` CLI subcommand."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from tank import verify as tank_verify
from tank.cli import main as tank_main
from tank.register import register


def test_claude_writes_mcp_json_at_project_root(tmp_path: Path) -> None:
    path = register("claude", "http://127.0.0.1:8000", "tok", tmp_path, scope="project")
    assert path == tmp_path / ".mcp.json"
    config = json.loads(path.read_text())
    assert config["mcpServers"]["tank"]["env"] == {
        "TANK_TOKEN": "tok",
        "TANK_HEADJACK_URL": "http://127.0.0.1:8000",
    }
    assert config["mcpServers"]["tank"]["args"] == [
        "run",
        "--extra",
        "client",
        "python3",
        "-m",
        "tank.main",
    ]
    assert config["mcpServers"]["tank"]["alwaysLoad"] is True


def test_cursor_writes_dot_cursor_mcp_json(tmp_path: Path) -> None:
    path = register("cursor", "http://127.0.0.1:8000", "tok", tmp_path, scope="project")
    assert path == tmp_path / ".cursor" / "mcp.json"
    assert path.exists()


def test_unknown_client_raises_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown client"):
        register("vscode", "http://x", "t", tmp_path)


def test_unknown_scope_raises_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown scope"):
        register("claude", "http://x", "t", tmp_path, scope="org")


def test_cursor_global_writes_to_home_cursor_mcp_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    path = register("cursor", "http://127.0.0.1:8000", "tok", tmp_path, scope="global")

    assert path == fake_home / ".cursor" / "mcp.json"
    assert path.exists()
    assert path != tmp_path / ".cursor" / "mcp.json"


def test_cursor_global_uses_headjack_tank_serve_not_uv_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A globally-registered server is launched by the MCP host from whatever
    project the user has open, not this checkout - `uv run --extra client ...`
    fails there with `ModuleNotFoundError: No module named 'tank'` (reproduced
    live against this exact bug). Only the installed `headjack` binary's
    `tank-serve` subcommand works - there is no separate `tank` binary."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    path = register("cursor", "http://127.0.0.1:8000", "tok", tmp_path, scope="global")

    config = json.loads(path.read_text())
    assert config["mcpServers"]["tank"]["command"] == "headjack"
    assert config["mcpServers"]["tank"]["args"] == ["tank-serve"]


def test_cursor_project_scope_still_uses_uv_run_from_source(tmp_path: Path) -> None:
    """Project scope is launched with cwd fixed to the registering directory by
    the MCP host - the dev-loop case where picking up local, uninstalled edits
    to src/tank/*.py matters more than a stable installed binary."""
    path = register("cursor", "http://127.0.0.1:8000", "tok", tmp_path, scope="project")

    config = json.loads(path.read_text())
    assert config["mcpServers"]["tank"]["command"] == "uv"
    assert config["mcpServers"]["tank"]["args"] == [
        "run",
        "--extra",
        "client",
        "python3",
        "-m",
        "tank.main",
    ]


def test_claude_global_delegates_to_claude_cli_and_patches_always_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    claude_json = fake_home / ".claude.json"
    claude_json.write_text(json.dumps({"mcpServers": {"tank": {"command": "uv"}}}))

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("tank.register.subprocess.run", fake_run)

    path = register("claude", "http://127.0.0.1:8000", "tok", tmp_path, scope="global")

    assert path == claude_json
    assert calls == [
        ["claude", "mcp", "remove", "-s", "user", "tank"],
        [
            "claude",
            "mcp",
            "add",
            "-s",
            "user",
            "tank",
            "-e",
            "TANK_TOKEN=tok",
            "-e",
            "TANK_HEADJACK_URL=http://127.0.0.1:8000",
            "--",
            "headjack",
            "tank-serve",
        ],
    ]
    config = json.loads(claude_json.read_text())
    assert config["mcpServers"]["tank"]["alwaysLoad"] is True

    settings = json.loads((fake_home / ".claude" / "settings.json").read_text())
    assert settings["permissions"]["ask"] == ["mcp__tank__skill_install"]


def test_claude_global_preserves_existing_settings_and_ask_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    (fake_home / ".claude.json").write_text(json.dumps({"mcpServers": {"tank": {}}}))
    settings_path = fake_home / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps({"permissions": {"ask": ["Bash(git push:*)"]}, "outputStyle": "concise"})
    )

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("tank.register.subprocess.run", fake_run)

    register("claude", "http://127.0.0.1:8000", "tok", tmp_path, scope="global")

    settings = json.loads(settings_path.read_text())
    assert settings["outputStyle"] == "concise"  # untouched sibling key
    assert set(settings["permissions"]["ask"]) == {"Bash(git push:*)", "mcp__tank__skill_install"}


def test_claude_global_registering_twice_does_not_duplicate_the_ask_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    (fake_home / ".claude.json").write_text(json.dumps({"mcpServers": {"tank": {}}}))

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("tank.register.subprocess.run", fake_run)

    register("claude", "http://127.0.0.1:8000", "tok", tmp_path, scope="global")
    register("claude", "http://127.0.0.1:8000", "tok", tmp_path, scope="global")

    settings = json.loads((fake_home / ".claude" / "settings.json").read_text())
    assert settings["permissions"]["ask"] == ["mcp__tank__skill_install"]


def test_claude_global_is_idempotent_even_when_already_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`claude mcp add` has no overwrite flag and fails outright on a name
    that's already registered - reproduced live against this exact bug.
    register() must still succeed, by removing any existing entry first."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    claude_json = fake_home / ".claude.json"
    claude_json.write_text(json.dumps({"mcpServers": {"tank": {"command": "uv"}}}))

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["claude", "mcp", "add"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        # "remove" always succeeds too, whether or not anything was there to
        # remove - callers never branch on it, matching real `claude mcp remove`
        # exiting nonzero for "not found" without that being this function's
        # problem to handle.
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("tank.register.subprocess.run", fake_run)

    path = register("claude", "http://127.0.0.1:8000", "tok", tmp_path, scope="global")
    assert path == claude_json


def test_claude_global_missing_cli_raises_clear_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("no such file: claude")

    monkeypatch.setattr("tank.register.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match=r"claude.*CLI.*not found"):
        register("claude", "http://x", "t", tmp_path, scope="global")


def test_claude_global_cli_failure_raises_clear_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, cmd, output="", stderr="boom")

    monkeypatch.setattr("tank.register.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="boom"):
        register("claude", "http://x", "t", tmp_path, scope="global")


def test_existing_other_servers_are_left_untouched(tmp_path: Path) -> None:
    config_path = tmp_path / ".mcp.json"
    config_path.write_text(
        json.dumps({"mcpServers": {"other": {"command": "foo"}}, "_comment": "keep me"})
    )

    register("claude", "http://127.0.0.1:8000", "tok", tmp_path, scope="project")

    config = json.loads(config_path.read_text())
    assert config["mcpServers"]["other"] == {"command": "foo"}
    assert config["_comment"] == "keep me"
    assert "tank" in config["mcpServers"]


def test_running_twice_is_idempotent(tmp_path: Path) -> None:
    register("claude", "http://127.0.0.1:8000", "tok", tmp_path, scope="project")
    first = (tmp_path / ".mcp.json").read_text()
    register("claude", "http://127.0.0.1:8000", "tok", tmp_path, scope="project")
    second = (tmp_path / ".mcp.json").read_text()
    assert first == second


def test_cli_writes_the_file_and_prints_the_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = tank_main(
        [
            "register",
            "--client",
            "claude",
            "--remote",
            "http://127.0.0.1:8000",
            "--token",
            "tok",
            "--scope",
            "project",
            "--project-dir",
            str(tmp_path),
        ]
    )
    assert exit_code == 0
    assert (tmp_path / ".mcp.json").exists()
    assert str(tmp_path / ".mcp.json") in capsys.readouterr().out


def test_cli_prints_approval_guidance_for_claude_project_scope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tank_main(
        [
            "register",
            "--client",
            "claude",
            "--remote",
            "http://127.0.0.1:8000",
            "--token",
            "tok",
            "--scope",
            "project",
            "--project-dir",
            str(tmp_path),
        ]
    )
    assert "one-time approval" in capsys.readouterr().out


def test_cli_skips_approval_guidance_for_cursor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tank_main(
        [
            "register",
            "--client",
            "cursor",
            "--remote",
            "http://127.0.0.1:8000",
            "--token",
            "tok",
            "--scope",
            "project",
            "--project-dir",
            str(tmp_path),
        ]
    )
    assert "one-time approval" not in capsys.readouterr().out


def test_cli_rejects_unknown_client_cleanly() -> None:
    with pytest.raises(SystemExit) as exc_info:
        tank_main(["register", "--client", "vscode", "--remote", "x", "--token", "t"])
    assert exc_info.value.code == 2


def test_tank_cli_help_does_not_import_mcp_or_httpx() -> None:
    import subprocess
    import sys
    from pathlib import Path as P

    script = (
        "import tank.cli\n"
        "import sys\n"
        "forbidden = ['mcp', 'httpx', 'headjack']\n"
        "leaked = [m for m in forbidden if m in sys.modules]\n"
        "assert not leaked, f'forbidden modules imported: {leaked}'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=P(__file__).parent.parent,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


# ------------------------------------------------------------- TOFU signing key


class _FakeResponse:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._data


def _fake_signing_key_get(key_id: str | None, public_key: str | None) -> Any:
    return lambda url, timeout: _FakeResponse({"key_id": key_id, "public_key": public_key})


def test_pins_a_new_signing_key_on_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted_path = tmp_path / "trusted_keys.toml"
    monkeypatch.setattr(tank_verify, "TRUSTED_KEYS_PATH", trusted_path)
    monkeypatch.setattr(
        "tank.register.httpx.get", _fake_signing_key_get("ops-2026", "cHVibGljLWtleQ==")
    )

    register("claude", "http://127.0.0.1:8000", "tok", tmp_path, scope="project")

    trusted = tank_verify.load_trusted_keys(trusted_path)
    assert trusted == {"ops-2026": b"public-key"}


def test_tofu_pin_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    trusted_path = tmp_path / "trusted_keys.toml"
    monkeypatch.setattr(tank_verify, "TRUSTED_KEYS_PATH", trusted_path)
    monkeypatch.setattr(
        "tank.register.httpx.get", _fake_signing_key_get("ops-2026", "cHVibGljLWtleQ==")
    )

    register("claude", "http://127.0.0.1:8000", "tok", tmp_path, scope="project")
    register("claude", "http://127.0.0.1:8000", "tok", tmp_path, scope="project")

    assert tank_verify.load_trusted_keys(trusted_path) == {"ops-2026": b"public-key"}


def test_tofu_refuses_to_overwrite_a_different_pinned_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    trusted_path = tmp_path / "trusted_keys.toml"
    trusted_path.write_text('[[key]]\nid = "ops-2026"\npublic_key = "b3JpZ2luYWwta2V5"\n')
    monkeypatch.setattr(tank_verify, "TRUSTED_KEYS_PATH", trusted_path)
    monkeypatch.setattr(
        "tank.register.httpx.get", _fake_signing_key_get("ops-2026", "ZGlmZmVyZW50LWtleQ==")
    )

    register("claude", "http://127.0.0.1:8000", "tok", tmp_path, scope="project")

    # the original pin survives untouched
    assert tank_verify.load_trusted_keys(trusted_path) == {"ops-2026": b"original-key"}
    assert "ops-2026" in capsys.readouterr().err


def test_tofu_pin_skipped_when_deployment_does_not_sign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted_path = tmp_path / "trusted_keys.toml"
    monkeypatch.setattr(tank_verify, "TRUSTED_KEYS_PATH", trusted_path)
    monkeypatch.setattr(
        "tank.register.httpx.get",
        lambda url, timeout: _FakeResponse({"key_id": None, "public_key": None}),
    )

    register("claude", "http://127.0.0.1:8000", "tok", tmp_path, scope="project")

    assert not trusted_path.exists()


def test_tofu_pin_failure_does_not_fail_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    trusted_path = tmp_path / "trusted_keys.toml"
    monkeypatch.setattr(tank_verify, "TRUSTED_KEYS_PATH", trusted_path)

    def _raise(url: str, timeout: float) -> _FakeResponse:
        raise httpx.ConnectError("could not connect")

    monkeypatch.setattr("tank.register.httpx.get", _raise)

    path = register("claude", "http://127.0.0.1:8000", "tok", tmp_path, scope="project")

    assert path == tmp_path / ".mcp.json"  # registration itself still succeeded
    assert not trusted_path.exists()
    assert "signing key" in capsys.readouterr().err
