from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.embeddings import default_embedding_function
from app.embeddings.fake import FakeEmbeddingFunction
from app.embeddings.ollama import OllamaEmbeddingFunction
from app.ingest.startup import CorpusStartupError
from app.main import _default_provider, create_app
from app.providers.echo import EchoProvider
from app.providers.gemini import GeminiProvider
from app.providers.ollama import OllamaProvider


@pytest.mark.parametrize("deployment_mode", ["development", "test", "DEVELOPMENT", "TEST"])
def test_local_modes_default_to_echo_and_fake(
    deployment_mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    settings = Settings.model_validate({"deployment_mode": deployment_mode})
    assert settings.provider == "echo"
    assert settings.embedding_provider == "fake"
    assert isinstance(_default_provider(settings), EchoProvider)
    assert isinstance(default_embedding_function(settings), FakeEmbeddingFunction)


@pytest.mark.parametrize("provider", ["ollama", "OLLAMA", "OlLaMa"])
@pytest.mark.parametrize("embedding_provider", ["fake", "FAKE", "ollama", "OLLAMA"])
def test_known_local_provider_pairs_are_case_insensitive(
    provider: str, embedding_provider: str
) -> None:
    settings = Settings.model_validate(
        {"provider": provider, "embedding_provider": embedding_provider}
    )
    assert settings.provider == "ollama"
    assert isinstance(_default_provider(settings), OllamaProvider)
    expected = (
        OllamaEmbeddingFunction
        if settings.embedding_provider == "ollama"
        else FakeEmbeddingFunction
    )
    assert isinstance(default_embedding_function(settings), expected)


@pytest.mark.parametrize("deployment_mode", ["development", "test"])
def test_gemini_requires_nonblank_key_in_every_mode(deployment_mode: str) -> None:
    for key in (None, "", "   "):
        values: dict[str, object] = {
            "deployment_mode": deployment_mode,
            "provider": "gemini",
        }
        if key is not None:
            values["gemini_api_key"] = key
        with pytest.raises(ValidationError, match="GEMINI_API_KEY"):
            Settings.model_validate(values)


@pytest.mark.parametrize("provider", [None, "echo", "fake", "", "unknown"])
def test_production_rejects_non_hosted_generation(provider: str | None) -> None:
    values: dict[str, object] = {
        "deployment_mode": "production",
        "embedding_provider": "ollama",
    }
    if provider is not None:
        values["provider"] = provider
    with pytest.raises(ValidationError):
        Settings.model_validate(values)


@pytest.mark.parametrize("embedding_provider", [None, "fake", "", "unknown"])
def test_production_rejects_non_real_embeddings(
    embedding_provider: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    values: dict[str, object] = {
        "deployment_mode": "production",
        "provider": "ollama",
    }
    if embedding_provider is not None:
        values["embedding_provider"] = embedding_provider
    with pytest.raises(ValidationError):
        Settings.model_validate(values)


def test_production_ollama_pair_needs_no_gemini_inputs() -> None:
    settings = Settings.model_validate(
        {
            "deployment_mode": "PRODUCTION",
            "provider": "OLLAMA",
            "embedding_provider": "OLLAMA",
        }
    )
    assert settings.deployment_mode == "production"
    assert settings.gemini_api_key is None


def test_missing_optional_extra_has_content_free_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings.model_validate(
        {"provider": "gemini", "gemini_api_key": "sentinel-secret"}
    )

    def missing_module(name: str) -> object:
        assert name == "app.providers.gemini"
        raise ModuleNotFoundError

    monkeypatch.setattr("app.main.importlib.import_module", missing_module)
    with pytest.raises(RuntimeError) as caught:
        _default_provider(settings)
    assert "gemini" in str(caught.value).lower()
    assert "sentinel-secret" not in str(caught.value)


class _ClosingProvider(EchoProvider):
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _OwnedAsyncClient:
    def __init__(self) -> None:
        self.models: Any = object()
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1


class _OwnedRootClient:
    def __init__(self) -> None:
        self.aio = _OwnedAsyncClient()
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


def _root_owned_provider(root: _OwnedRootClient) -> GeminiProvider:
    return GeminiProvider(
        client=root.aio,
        root_client=root,
        types_module=object(),
        model="gemini-3.8-flash",
        timeout_seconds=1,
        max_retries=0,
    )


@pytest.fixture
def app_paths(tmp_path: Path) -> dict[str, object]:
    return {"database_path": tmp_path / "test.db", "chroma_path": tmp_path / "chroma"}


def test_application_owned_provider_is_closed_on_shutdown(
    monkeypatch: pytest.MonkeyPatch, app_paths: dict[str, object]
) -> None:
    provider = _ClosingProvider()
    monkeypatch.setattr("app.main._default_provider", lambda settings: provider)
    app = create_app(Settings.model_validate(app_paths))
    with TestClient(app):
        assert provider.closed is False
    assert provider.closed is True


def test_application_owned_gemini_closes_both_root_transports_once(
    monkeypatch: pytest.MonkeyPatch, app_paths: dict[str, object]
) -> None:
    root = _OwnedRootClient()
    provider = _root_owned_provider(root)
    monkeypatch.setattr("app.main._default_provider", lambda settings: provider)
    app = create_app(Settings.model_validate(app_paths))

    with TestClient(app):
        assert root.aio.close_calls == 0
        assert root.close_calls == 0

    assert root.aio.close_calls == 1
    assert root.close_calls == 1


def test_injected_provider_remains_caller_owned(app_paths: dict[str, object]) -> None:
    provider = _ClosingProvider()
    app = create_app(Settings.model_validate(app_paths), provider=provider)
    with TestClient(app):
        pass
    assert provider.closed is False


def test_application_owned_provider_is_closed_after_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
    app_paths: dict[str, object],
    tmp_path: Path,
) -> None:
    provider = _ClosingProvider()
    corpus = tmp_path / "empty-corpus"
    corpus.mkdir()
    monkeypatch.setattr("app.main._default_provider", lambda settings: provider)
    settings = Settings.model_validate({**app_paths, "corpus_path": corpus})

    with pytest.raises(CorpusStartupError, match="no Markdown or PDF files"):
        with TestClient(create_app(settings)):
            pass
    assert provider.closed is True


def test_application_owned_gemini_closes_both_transports_after_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
    app_paths: dict[str, object],
    tmp_path: Path,
) -> None:
    root = _OwnedRootClient()
    provider = _root_owned_provider(root)
    corpus = tmp_path / "empty-gemini-corpus"
    corpus.mkdir()
    monkeypatch.setattr("app.main._default_provider", lambda settings: provider)
    settings = Settings.model_validate({**app_paths, "corpus_path": corpus})

    with pytest.raises(CorpusStartupError, match="no Markdown or PDF files"):
        with TestClient(create_app(settings)):
            pass

    assert root.aio.close_calls == 1
    assert root.close_calls == 1
