from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.config import Settings

REPO_ROOT = Path(__file__).parents[2]


def _dotenv_values(path: Path) -> dict[str, str]:
    return {
        key: value
        for line in path.read_text().splitlines()
        if line and not line.startswith("#")
        for key, value in [line.split("=", maxsplit=1)]
    }


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    settings = Settings()
    assert settings.deployment_mode == "development"
    assert settings.provider == "echo"
    assert settings.embedding_provider == "fake"
    assert settings.gemini_api_key is None
    assert settings.gemini_model == "gemini-3.8-flash"
    assert settings.gemini_timeout_seconds == 30.0
    assert settings.gemini_max_retries == 1
    assert settings.ollama_base_url == "http://localhost:11434"
    assert settings.chat_message_max_chars == 500
    assert settings.cairn_port == 8080
    assert settings.system_instruction == ""
    assert settings.max_output_tokens == 1500
    assert settings.max_output_chars == 6000


@pytest.mark.parametrize(
    "field,value",
    [
        ("system_instruction", "x" * 4001),
        ("max_output_tokens", 1501),
        ("max_output_chars", 6001),
    ],
)
def test_generation_settings_respect_contract_maxima(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("gemini_model", "latest"),
        ("gemini_model", "gemini-3.8-flash "),
        ("gemini_model", ""),
        ("gemini_timeout_seconds", 0),
        ("gemini_timeout_seconds", 181),
        ("gemini_timeout_seconds", float("inf")),
        ("gemini_timeout_seconds", float("nan")),
        ("gemini_max_retries", -1),
        ("gemini_max_retries", 2),
    ],
)
def test_gemini_settings_reject_values_outside_frozen_bounds(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({field: value})


def test_generation_setting_defaults_match_env_example_and_compose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    settings = Settings()
    expected = {
        "DEPLOYMENT_MODE": settings.deployment_mode,
        "PROVIDER": settings.provider,
        "EMBEDDING_PROVIDER": settings.embedding_provider,
        "GEMINI_API_KEY": "",
        "GEMINI_MODEL": settings.gemini_model,
        "GEMINI_TIMEOUT_SECONDS": str(settings.gemini_timeout_seconds),
        "GEMINI_MAX_RETRIES": str(settings.gemini_max_retries),
        "SYSTEM_INSTRUCTION": settings.system_instruction,
        "MAX_OUTPUT_TOKENS": str(settings.max_output_tokens),
        "MAX_OUTPUT_CHARS": str(settings.max_output_chars),
    }
    env_values = _dotenv_values(REPO_ROOT / ".env.example")
    compose = yaml.safe_load((REPO_ROOT / "compose.yaml").read_text())
    compose_environment = compose["services"]["backend"]["environment"]

    assert {key: env_values[key] for key in expected} == expected
    assert {
        key: compose_environment[key] for key in expected
    } == {
        key: f"${{{key}:-{value}}}" for key, value in expected.items()
    }

    assert env_values["CAIRN_INSTALL_GEMINI"] == "false"
    assert compose["services"]["backend"]["build"]["args"] == {
        "CAIRN_INSTALL_GEMINI": "${CAIRN_INSTALL_GEMINI:-false}"
    }


def test_origins_splits_and_strips() -> None:
    settings = Settings(origin_allowlist=" http://a.example , http://b.example")
    assert settings.origins == ["http://a.example", "http://b.example"]


def test_origins_empty_allowlist_derives_from_cairn_port() -> None:
    settings = Settings(origin_allowlist="", cairn_port=8081)
    assert settings.origins == ["http://localhost:8081"]


def test_origins_default_is_own_origin() -> None:
    settings = Settings()
    assert settings.origins == ["http://localhost:8080"]


def test_origins_explicit_allowlist_wins_over_port() -> None:
    settings = Settings(origin_allowlist="http://widget.example", cairn_port=8081)
    assert settings.origins == ["http://widget.example"]
