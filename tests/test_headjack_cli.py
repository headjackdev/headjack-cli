"""Tests for headjack.cli's top-level dispatch and help text."""

from __future__ import annotations

import pytest

from headjack.cli import main


def test_no_args_prints_help_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "headjack" in out
    assert "index" in out and "search" in out and "publish" in out
    assert "list" in out and "init" in out and "migrate" in out


def test_list_subcommand_dispatches_to_listing_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import headjack.listing as listing_module

    called: list[list[str]] = []
    monkeypatch.setattr(listing_module, "main", lambda argv=None: called.append(argv) or 0)
    assert main(["list", "--remote", "http://h", "--token", "t"]) == 0
    assert called == [["--remote", "http://h", "--token", "t"]]


def test_init_subcommand_dispatches_to_init_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import headjack.init as init_module

    called: list[list[str]] = []
    monkeypatch.setattr(init_module, "main", lambda argv=None: called.append(argv) or 0)
    assert main(["init", "--token", "t"]) == 0
    assert called == [["--token", "t"]]


def test_migrate_subcommand_dispatches_to_migrate_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import headjack.migrate as migrate_module

    called: list[list[str]] = []
    monkeypatch.setattr(migrate_module, "main", lambda argv=None: called.append(argv) or 0)
    assert main(["migrate", "pdf", "--token", "t"]) == 0
    assert called == [["pdf", "--token", "t"]]


def test_help_subcommand_behaves_like_dash_dash_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["help"]) == 0
    out = capsys.readouterr().out
    assert "usage: headjack" in out


def test_help_text_includes_worked_examples(capsys: pytest.CaptureFixture[str]) -> None:
    main(["help"])
    out = capsys.readouterr().out
    assert "examples:" in out
    assert "headjack search" in out


def test_version_flag_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    assert "headjack" in capsys.readouterr().out


def test_unknown_command_is_rejected_by_argparse() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["not-a-real-command"])
    assert exc_info.value.code == 2


def test_tank_serve_subcommand_dispatches_to_tank_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one thing a globally-registered MCP entry actually launches
    (`headjack tank-serve`, via `tank.register._launch_command`) must reach the
    real server loop — there is no separate `tank` binary any more."""
    called = False

    def _fake_serve() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("tank.main.main", _fake_serve)
    exit_code = main(["tank-serve"])
    assert exit_code == 0
    assert called
