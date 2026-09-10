import math
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


def test_defaults() -> None:
    settings = Settings()
    assert settings.ollama_base_url == "http://localhost:11434"
    assert settings.chat_message_max_chars == 500
    assert settings.cairn_port == 8080
    assert settings.system_instruction == ""
    assert settings.max_output_tokens == 1500
    assert settings.max_output_chars == 6000
    assert settings.retrieval_top_k == 4
    assert settings.retrieval_max_distance == 1.2


@pytest.mark.parametrize("value", [0, 7, True, 1.5, "1.5", "four"])
def test_retrieval_top_k_is_a_bounded_integer(value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(retrieval_top_k=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [-0.1, True, math.nan, math.inf, 10**1000, "nan", "inf"])
def test_retrieval_max_distance_is_finite_nonnegative(value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(retrieval_max_distance=value)  # type: ignore[arg-type]


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


def test_generation_setting_defaults_match_env_example_and_compose() -> None:
    settings = Settings()
    expected = {
        "SYSTEM_INSTRUCTION": settings.system_instruction,
        "MAX_OUTPUT_TOKENS": str(settings.max_output_tokens),
        "MAX_OUTPUT_CHARS": str(settings.max_output_chars),
    }
    env_values = _dotenv_values(REPO_ROOT / ".env.example")
    compose = yaml.safe_load((REPO_ROOT / "compose.yaml").read_text())
    compose_environment = compose["services"]["backend"]["environment"]

    assert {key: env_values[key] for key in expected} == expected
    assert {key: compose_environment[key] for key in expected} == {
        key: f"${{{key}:-{value}}}" for key, value in expected.items()
    }


def test_retrieval_setting_defaults_match_env_example_and_compose() -> None:
    settings = Settings()
    expected = {
        "RETRIEVAL_TOP_K": str(settings.retrieval_top_k),
        "RETRIEVAL_MAX_DISTANCE": str(settings.retrieval_max_distance),
    }
    env_values = _dotenv_values(REPO_ROOT / ".env.example")
    compose = yaml.safe_load((REPO_ROOT / "compose.yaml").read_text())
    compose_environment = compose["services"]["backend"]["environment"]

    assert {key: env_values[key] for key in expected} == expected
    assert {key: compose_environment[key] for key in expected} == {
        key: f"${{{key}:-{value}}}" for key, value in expected.items()
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
