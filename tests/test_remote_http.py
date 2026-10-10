"""HTTPS CLI commands against httpx.MockTransport — no server/app in this package."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

import httpx
import numpy as np
import pytest

from headjack import cliconfig
from headjack.init import _token_works
from headjack.init import main as init_main
from headjack.listing import _list_remote
from headjack.publish import PublishAuthError, publish
from headjack.search import _search_remote
from headjack.update import _check


@pytest.fixture(autouse=True)
def _fake_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "headjack-config.toml"
    monkeypatch.setattr(cliconfig, "CONFIG_PATH", path)
    return path


def _client(handler: httpx.MockTransport | object) -> httpx.Client:
    transport = (
        handler if isinstance(handler, httpx.MockTransport) else httpx.MockTransport(handler)
    )
    return httpx.Client(transport=transport, base_url="https://api.example.test")


class _FakeEmbedder:
    dim = 8

    def encode(self, texts: list[str]) -> np.ndarray:
        return np.zeros((len(texts), self.dim), dtype=np.float32)

    def encode_one(self, text: str) -> np.ndarray:
        return np.zeros(self.dim, dtype=np.float32)


def test_token_works_accepts_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/artifacts"
        assert request.headers["Authorization"] == "Bearer good"
        return httpx.Response(200, json=[])

    assert _token_works("https://api.example.test", "good", client=_client(handler)) is True


def test_token_works_rejects_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "unauthorized"})

    assert _token_works("https://api.example.test", "bad", client=_client(handler)) is False


def test_init_writes_config_after_mock_validation(
    _fake_config_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import headjack.init as init_module

    monkeypatch.setenv("HEADJACK_REMOTE", "https://api.example.test")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    monkeypatch.setattr(
        init_module,
        "_token_works",
        lambda remote, token, **kw: _token_works(remote, token, client=_client(handler)),
    )
    assert init_main(["--token", "alice-token"]) == 0
    assert cliconfig.load() == {"token": "alice-token"}
    assert "wrote" in capsys.readouterr().out


def test_list_remote_renders_table(capsys: pytest.CaptureFixture[str]) -> None:
    payload = [
        {
            "kind": "skill",
            "name": "pdf-fill",
            "version": "1.0",
            "description": "Fill PDFs",
            "systems": ["acrobat"],
            "updated_at": "2026-01-01T00:00:00Z",
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/artifacts"
        return httpx.Response(200, json=payload)

    args = argparse.Namespace(kind="skill", json=False)
    assert _list_remote("https://api.example.test", "tok", args, client=_client(handler)) == 0
    out = capsys.readouterr().out
    assert "pdf-fill" in out
    assert "Fill PDFs" in out


def test_list_remote_json_mode(capsys: pytest.CaptureFixture[str]) -> None:
    payload = [{"kind": "skill", "name": "a", "version": None, "updated_at": "t", "systems": []}]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    args = argparse.Namespace(kind="all", json=True)
    assert _list_remote("https://api.example.test", "tok", args, client=_client(handler)) == 0
    assert json.loads(capsys.readouterr().out) == payload


def test_search_remote_posts_query(capsys: pytest.CaptureFixture[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/search"
        body = json.loads(request.content)
        assert body["query"] == "pdf forms"
        assert body["query_vector"] is None  # --no-dense
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "name": "pdf-fill",
                        "kind": "skill",
                        "score": 0.9,
                        "snippet": "fill a form",
                        "matched_in": "Usage",
                        "source": "skills/pdf/SKILL.md",
                    }
                ]
            },
        )

    args = argparse.Namespace(query="pdf forms", k=5, json=False, scope=None, no_dense=True)
    assert _search_remote("https://api.example.test", "tok", args, client=_client(handler)) == 0
    out = capsys.readouterr().out
    assert "pdf-fill" in out
    assert "fill a form" in out


def test_publish_puts_new_skill(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "hello"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: hello\ndescription: say hi\n---\n\n# Hello\n\nSay hello.\n"
    )

    puts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/artifacts":
            return httpx.Response(200, json=[])
        if request.method == "PUT":
            puts.append(request.url.path)
            payload = json.loads(request.content)
            assert payload["body"]
            assert payload["chunks"]
            assert payload["files"]
            # files content is base64
            assert base64.b64decode(payload["files"][0]["content_b64"])
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    stats = publish(
        tmp_path / "skills",
        "https://api.example.test",
        "tok",
        embedder=_FakeEmbedder(),  # type: ignore[arg-type]
        client=_client(handler),
    )
    assert stats.added == 1
    assert stats.failed == 0
    assert puts == ["/v1/artifacts/skill/hello"]


def test_publish_auth_error_on_401(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "hello"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: hello\n---\n\n# Hi\n")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "no"})

    with pytest.raises(PublishAuthError):
        publish(
            tmp_path / "skills",
            "https://api.example.test",
            "bad",
            embedder=_FakeEmbedder(),  # type: ignore[arg-type]
            client=_client(handler),
        )


def test_update_check_up_to_date(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("headjack.update.__version__", "0.1.0")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/cli/version"
        return httpx.Response(
            200,
            json={
                "latest": "0.1.0",
                "minimum": "0.1.0",
                "install_url": "uv tool install headjack-cli",
            },
        )

    assert _check("https://api.example.test", client=_client(handler)) == 0
    assert "up to date" in capsys.readouterr().out


def test_update_check_newer_available(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("headjack.update.__version__", "0.1.0")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "latest": "0.2.0",
                "minimum": "0.1.0",
                "install_url": "uv tool install headjack-cli",
            },
        )

    assert _check("https://api.example.test", client=_client(handler)) == 0
    assert "0.2.0 is available" in capsys.readouterr().out
