import importlib.util
import os
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from app.config import Settings


@pytest.fixture
def dev_demo(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "dev_demo.py"
    spec = importlib.util.spec_from_file_location("dev_demo_for_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.delenv("PROVIDER", raising=False)
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    spec.loader.exec_module(module)
    return module


def test_corpus_directory_must_exist(
    dev_demo: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(SystemExit) as exc_info:
        dev_demo._corpus_files(missing)

    assert exc_info.value.code == 1
    output = capsys.readouterr().out
    assert str(missing) in output
    assert "does not exist" in output


def test_corpus_directory_must_contain_markdown(
    dev_demo: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "ignored.txt").write_text("not a supported corpus file")

    with pytest.raises(SystemExit) as exc_info:
        dev_demo._corpus_files(tmp_path)

    assert exc_info.value.code == 1
    output = capsys.readouterr().out
    assert str(tmp_path) in output
    assert ".md" in output


def test_corpus_markdown_must_not_be_empty(
    dev_demo: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "empty.md").write_text("\n")

    with pytest.raises(SystemExit) as exc_info:
        dev_demo._corpus_files(tmp_path)

    assert exc_info.value.code == 1
    output = capsys.readouterr().out
    assert str(tmp_path) in output
    assert "non-empty .md" in output


def test_demo_forces_real_provider_and_embeddings(
    dev_demo: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROVIDER", "echo")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")

    dev_demo._configure_real_providers()

    assert os.environ["PROVIDER"] == "ollama"
    assert os.environ["EMBEDDING_PROVIDER"] == "ollama"


@pytest.mark.asyncio
async def test_ollama_must_be_reachable_before_startup(
    dev_demo: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    settings = Settings(ollama_base_url="http://localhost:11434")
    async with httpx.AsyncClient(transport=httpx.MockTransport(unreachable)) as client:
        with pytest.raises(SystemExit) as exc_info:
            await dev_demo._check_ollama_models(settings, client=client)

    assert exc_info.value.code == 1
    output = capsys.readouterr().out
    assert "http://localhost:11434" in output
    assert "ollama serve" in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("available", "missing_model"),
    [
        (["nomic-embed-text:latest"], "llama3.1:8b-instruct"),
        (["llama3.1:8b-instruct"], "nomic-embed-text"),
    ],
)
async def test_each_configured_ollama_model_must_be_available_before_startup(
    dev_demo: ModuleType,
    capsys: pytest.CaptureFixture[str],
    available: list[str],
    missing_model: str,
) -> None:
    def tags(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": name} for name in available]})

    settings = Settings(
        ollama_base_url="http://localhost:11434",
        ollama_model="llama3.1:8b-instruct",
        embedding_model="nomic-embed-text",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(tags)) as client:
        with pytest.raises(SystemExit) as exc_info:
            await dev_demo._check_ollama_models(settings, client=client)

    assert exc_info.value.code == 1
    output = capsys.readouterr().out
    assert missing_model in output
    assert f"ollama pull {missing_model}" in output


@pytest.mark.asyncio
async def test_preflight_accepts_both_configured_models(
    dev_demo: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    def tags(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "llama3.1:8b-instruct"},
                    {"name": "nomic-embed-text:latest"},
                ]
            },
        )

    settings = Settings(
        ollama_base_url="http://localhost:11434",
        ollama_model="llama3.1:8b-instruct",
        embedding_model="nomic-embed-text",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(tags)) as client:
        await dev_demo._check_ollama_models(settings, client=client)

    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"models": None},
        {"models": [{"name": "llama3.1:8b-instruct"}, "not-an-object"]},
    ],
)
async def test_malformed_ollama_model_list_is_actionable(
    dev_demo: ModuleType,
    capsys: pytest.CaptureFixture[str],
    payload: object,
) -> None:
    def malformed_tags(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    settings = Settings(ollama_base_url="http://localhost:11434")
    async with httpx.AsyncClient(transport=httpx.MockTransport(malformed_tags)) as client:
        with pytest.raises(SystemExit) as exc_info:
            await dev_demo._check_ollama_models(settings, client=client)

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "did not return a usable model list" in captured.out
    assert "Traceback" not in captured.err
