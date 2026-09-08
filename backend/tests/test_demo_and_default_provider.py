from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import _default_provider, create_app
from app.providers.echo import EchoProvider
from app.providers.ollama import OllamaProvider


def test_default_provider_is_echo_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROVIDER", raising=False)
    assert isinstance(_default_provider(Settings()), EchoProvider)


def test_default_provider_is_ollama_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROVIDER", "ollama")
    assert isinstance(_default_provider(Settings()), OllamaProvider)


def test_default_provider_env_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROVIDER", "OLLAMA")
    assert isinstance(_default_provider(Settings()), OllamaProvider)


def test_demo_page_served(tmp_path: Path) -> None:
    settings = Settings(database_path=tmp_path / "test.db", chroma_path=tmp_path / "chroma")
    app = create_app(settings, provider=EchoProvider())
    with TestClient(app) as client:
        resp = client.get("/demo")
    assert resp.status_code == 200
    assert "Cairn" in resp.text
    assert "text/html" in resp.headers["content-type"]


def test_widget_bundle_served_from_stable_url(tmp_path: Path) -> None:
    settings = Settings(database_path=tmp_path / "test.db", chroma_path=tmp_path / "chroma")
    app = create_app(settings, provider=EchoProvider())
    with TestClient(app) as client:
        resp = client.get("/widget/widget.js")
        directory_resp = client.get("/widget/")

    bundle_path = Path(__file__).parents[1] / "app" / "static" / "widget" / "widget.js"
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/javascript")
    assert resp.content == bundle_path.read_bytes()
    assert b'customElements.define("cairn-chat"' in resp.content
    assert directory_resp.status_code == 404
