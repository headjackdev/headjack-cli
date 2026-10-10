"""Tests for `headjack.cliconfig` — the `--db`/`--token` fallback file, plus
`resolve_remote()`'s flag > env var > public-default precedence.

`CONFIG_PATH` is a module-level constant computed from `Path.home()` at import
time, so tests point `cliconfig.CONFIG_PATH` itself at a tmp_path file rather than
monkeypatching `Path.home()` (which would run too late to matter).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from headjack import cliconfig


@pytest.fixture(autouse=True)
def _fake_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.toml"
    monkeypatch.setattr(cliconfig, "CONFIG_PATH", path)
    return path


def test_missing_file_returns_empty_dict() -> None:
    assert cliconfig.load() == {}


def test_reads_known_keys(_fake_config_path: Path) -> None:
    _fake_config_path.write_text('db = "/x/index.db"\ntoken = "tok"\n')
    assert cliconfig.load() == {"db": "/x/index.db", "token": "tok"}


def test_unknown_keys_are_ignored(_fake_config_path: Path) -> None:
    _fake_config_path.write_text('db = "/x/index.db"\nscope = "team"\n')
    assert cliconfig.load() == {"db": "/x/index.db"}


def test_remote_key_in_the_file_is_ignored(_fake_config_path: Path) -> None:
    # `remote` was dropped from the config schema in favour of resolve_remote()'s
    # flag/env/default precedence - a stray leftover entry must not resurrect it.
    _fake_config_path.write_text('remote = "http://stale"\ntoken = "tok"\n')
    assert cliconfig.load() == {"token": "tok"}


def test_partial_file_omits_absent_keys(_fake_config_path: Path) -> None:
    _fake_config_path.write_text('token = "tok"\n')
    assert cliconfig.load() == {"token": "tok"}


def test_invalid_toml_raises_clearly(_fake_config_path: Path) -> None:
    _fake_config_path.write_text("this is not [valid toml")
    with pytest.raises(cliconfig.HeadjackConfigError, match="not valid TOML"):
        cliconfig.load()


def test_non_string_value_raises_clearly(_fake_config_path: Path) -> None:
    _fake_config_path.write_text("db = 5\n")
    with pytest.raises(cliconfig.HeadjackConfigError, match="must be a string"):
        cliconfig.load()


# --------------------------------------------------------------------------- write


def test_write_then_load_round_trips(_fake_config_path: Path) -> None:
    cliconfig.write({"token": "tok", "db": "/x/index.db"})
    assert cliconfig.load() == {"token": "tok", "db": "/x/index.db"}


def test_write_rejects_unknown_keys(_fake_config_path: Path) -> None:
    with pytest.raises(cliconfig.HeadjackConfigError, match="remote"):
        cliconfig.write({"remote": "http://h"})


def test_write_creates_parent_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    nested = tmp_path / "nested" / "config.toml"
    monkeypatch.setattr(cliconfig, "CONFIG_PATH", nested)
    cliconfig.write({"token": "tok"})
    assert nested.exists()


def test_write_sets_restrictive_permissions(_fake_config_path: Path) -> None:
    cliconfig.write({"token": "tok"})
    assert oct(_fake_config_path.stat().st_mode)[-3:] == "600"
    assert oct(_fake_config_path.parent.stat().st_mode)[-3:] == "700"


def test_write_escapes_special_characters_in_values(_fake_config_path: Path) -> None:
    tricky = 'tok"with\\quotes\nand newline'
    cliconfig.write({"token": tricky})
    assert cliconfig.load() == {"token": tricky}


# ----------------------------------------------------------------------- resolve_remote


def test_resolve_remote_cli_flag_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEADJACK_REMOTE", "http://from-env")
    assert cliconfig.resolve_remote("http://from-flag") == "http://from-flag"


def test_resolve_remote_falls_back_to_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEADJACK_REMOTE", "http://from-env")
    assert cliconfig.resolve_remote(None) == "http://from-env"


def test_resolve_remote_falls_back_to_public_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HEADJACK_REMOTE", raising=False)
    assert cliconfig.resolve_remote(None) == cliconfig.DEFAULT_REMOTE
