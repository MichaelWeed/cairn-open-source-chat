import pytest
from pydantic import ValidationError

from app.config import Settings


def test_defaults() -> None:
    settings = Settings()
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
