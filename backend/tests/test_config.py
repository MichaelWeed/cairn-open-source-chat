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
    assert settings.retrieval_top_k == 4
    assert settings.retrieval_max_distance == 1.2
    assert settings.retrieval_backend == "local"
    assert settings.firestore_project_id == ""
    assert settings.firestore_embedding_dimensions is None
    assert settings.firestore_query_timeout_seconds is None
    assert settings.firestore_max_retries is None


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
    assert {key: compose_environment[key] for key in expected} == {
        key: f"${{{key}:-{value}}}" for key, value in expected.items()
    }


def test_retrieval_setting_defaults_match_env_example_and_compose() -> None:
    settings = Settings()
    expected = {
        "RETRIEVAL_BACKEND": settings.retrieval_backend,
        "RETRIEVAL_TOP_K": str(settings.retrieval_top_k),
        "RETRIEVAL_MAX_DISTANCE": str(settings.retrieval_max_distance),
        "FIRESTORE_PROJECT_ID": "",
        "FIRESTORE_CORPUS_ID": "",
        "FIRESTORE_CORPUS_VERSION": "",
        "FIRESTORE_EMBEDDING_IDENTITY": "",
        "FIRESTORE_EMBEDDING_DIMENSIONS": "",
        "FIRESTORE_DISTANCE_MEASURE": "",
        "FIRESTORE_MAX_DISTANCE": "",
        "FIRESTORE_QUERY_TIMEOUT_SECONDS": "",
        "FIRESTORE_MAX_RETRIES": "",
    }
    env_values = _dotenv_values(REPO_ROOT / ".env.example")
    compose = yaml.safe_load((REPO_ROOT / "compose.yaml").read_text())
    compose_environment = compose["services"]["backend"]["environment"]

    assert {key: env_values[key] for key in expected} == expected
    assert {key: compose_environment[key] for key in expected} == {
        key: f"${{{key}:-{value}}}" for key, value in expected.items()
    }

    assert env_values["CAIRN_INSTALL_GEMINI"] == "false"
    assert env_values["CAIRN_INSTALL_FIRESTORE"] == "false"
    assert compose["services"]["backend"]["build"]["args"] == {
        "CAIRN_INSTALL_GEMINI": "${CAIRN_INSTALL_GEMINI:-false}",
        "CAIRN_INSTALL_FIRESTORE": "${CAIRN_INSTALL_FIRESTORE:-false}",
    }


def _firestore_settings(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "retrieval_backend": "firestore",
        "firestore_project_id": "cairn1",
        "firestore_corpus_id": "docs",
        "firestore_corpus_version": "v1",
        "firestore_embedding_identity": "ollama:nomic-embed-text",
        "firestore_embedding_dimensions": 768,
        "firestore_distance_measure": "cosine",
        "firestore_max_distance": 0.8,
        "firestore_query_timeout_seconds": 5,
        "firestore_max_retries": 1,
    }
    values.update(updates)
    return values


def test_firestore_configuration_accepts_only_complete_development_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOOGLE_SDK_PYTHON_LOGGING_SCOPE", raising=False)
    settings = Settings.model_validate(_firestore_settings())
    assert settings.retrieval_backend == "firestore"
    assert settings.firestore_embedding_dimensions == 768


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("deployment_mode", "production"),
        ("retrieval_backend", "FIRESTORE"),
        ("corpus_path", Path("corpus")),
        ("firestore_project_id", "Bad_Project"),
        ("firestore_corpus_id", "latest"),
        ("firestore_corpus_version", "latest"),
        ("firestore_embedding_identity", " padded "),
        ("firestore_embedding_dimensions", True),
        ("firestore_embedding_dimensions", 0),
        ("firestore_embedding_dimensions", 2049),
        ("firestore_distance_measure", "squared_l2"),
        ("firestore_max_distance", True),
        ("firestore_max_distance", math.inf),
        ("firestore_query_timeout_seconds", 0),
        ("firestore_query_timeout_seconds", 31),
        ("firestore_max_retries", 2),
    ],
)
def test_firestore_configuration_rejects_unsafe_values(field: str, value: object) -> None:
    expected = "RETRIEVAL_BACKEND" if field == "deployment_mode" else field
    with pytest.raises(ValidationError, match=f"(?i){expected}"):
        Settings.model_validate(_firestore_settings(**{field: value}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("firestore_project_id", "cairn1"),
        ("firestore_corpus_id", "docs"),
        ("firestore_corpus_version", "v1"),
        ("firestore_embedding_identity", "embed-v1"),
        ("firestore_embedding_dimensions", 2),
        ("firestore_distance_measure", "cosine"),
        ("firestore_max_distance", 0.8),
        ("firestore_query_timeout_seconds", 7),
        ("firestore_max_retries", 0),
    ],
)
def test_local_backend_rejects_every_stale_firestore_value(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError, match=field.upper()):
        Settings.model_validate({field: value})


@pytest.mark.parametrize(
    "field",
    [
        "firestore_project_id",
        "firestore_corpus_id",
        "firestore_corpus_version",
        "firestore_embedding_identity",
        "firestore_embedding_dimensions",
        "firestore_distance_measure",
        "firestore_max_distance",
        "firestore_query_timeout_seconds",
        "firestore_max_retries",
    ],
)
def test_firestore_requires_every_hosted_setting(field: str) -> None:
    values = _firestore_settings()
    values[field] = (
        None
        if field
        in {
            "firestore_embedding_dimensions",
            "firestore_max_distance",
            "firestore_query_timeout_seconds",
            "firestore_max_retries",
        }
        else ""
    )
    with pytest.raises(ValidationError, match=field.upper()):
        Settings.model_validate(values)


def test_firestore_rejects_sdk_debug_logging_without_echoing_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "private-debug-scope"
    monkeypatch.setenv("GOOGLE_SDK_PYTHON_LOGGING_SCOPE", secret)
    with pytest.raises(ValidationError) as caught:
        Settings.model_validate(_firestore_settings())
    assert "GOOGLE_SDK_PYTHON_LOGGING_SCOPE" in str(caught.value)
    assert secret not in str(caught.value)


def test_firestore_validation_string_hides_rejected_values() -> None:
    secret = "Bad_Project-secret"
    with pytest.raises(ValidationError) as caught:
        Settings.model_validate(_firestore_settings(firestore_project_id=secret))
    assert "FIRESTORE_PROJECT_ID" in str(caught.value)
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)


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
