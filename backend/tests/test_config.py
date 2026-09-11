import json
import logging
import math
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Literal, cast

import pytest
import yaml
from pydantic import SecretStr, TypeAdapter, ValidationError

from app.config import (
    Settings,
    SettingsValidationError,
    get_settings,
    validated_settings_snapshot,
)

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


ValidationExtra = Literal["allow", "ignore", "forbid"] | None


def _settings_surface(
    mode: str,
    payload: dict[str, object],
    extra: ValidationExtra,
) -> Settings:
    if mode.endswith("strings"):
        value: object = {key: str(item) for key, item in payload.items()}
    elif mode.endswith("json"):
        value = json.dumps(payload)
    else:
        value = payload
    if mode.startswith("model_"):
        method = getattr(Settings, mode)
    else:
        method = getattr(TypeAdapter(Settings), mode.removeprefix("adapter_"))
    return cast(Settings, method(value, extra=extra))


def _assert_content_free_settings_error(
    error: ValidationError, canaries: tuple[str, ...]
) -> None:
    rendered = (
        str(error)
        + repr(error)
        + repr(error.errors(include_input=True, include_context=True))
        + error.json(include_input=True, include_context=True)
        + repr(error.__cause__)
        + repr(error.__context__)
        + repr(error.__dict__)
    )
    assert "FIRESTORE_PROJECT_ID" in rendered
    assert all(canary not in rendered for canary in canaries)


def test_firestore_constructor_structured_error_never_retains_values() -> None:
    canaries = (
        "Bad_Project-secret",
        "corpus-secret",
        "v1-secret",
        "embedding-secret",
        "retained-extra-secret",
    )
    payload = _firestore_settings(
        firestore_project_id=canaries[0],
        firestore_corpus_id=canaries[1],
        firestore_corpus_version=canaries[2],
        firestore_embedding_identity=canaries[3],
    )
    payload["unknown_content"] = canaries[4]

    with pytest.raises(ValidationError) as caught:
        Settings(**payload)  # type: ignore[arg-type]
    _assert_content_free_settings_error(caught.value, canaries)


@pytest.mark.parametrize(
    "mode",
    [
        "model_validate",
        "model_validate_json",
        "model_validate_strings",
        "adapter_validate_python",
        "adapter_validate_json",
        "adapter_validate_strings",
    ],
)
@pytest.mark.parametrize("extra", [None, "allow", "ignore", "forbid"])
def test_firestore_structured_validation_errors_never_retain_values(
    mode: str,
    extra: ValidationExtra,
) -> None:
    canaries = (
        "Bad_Project-secret",
        "corpus-secret",
        "v1-secret",
        "embedding-secret",
        "retained-extra-secret",
    )
    payload = _firestore_settings(
        firestore_project_id=canaries[0],
        firestore_corpus_id=canaries[1],
        firestore_corpus_version=canaries[2],
        firestore_embedding_identity=canaries[3],
    )
    payload["unknown_content"] = canaries[4]

    with pytest.raises(ValidationError) as caught:
        _settings_surface(mode, payload, extra)
    _assert_content_free_settings_error(caught.value, canaries)


@pytest.mark.parametrize(
    "mode",
    [
        "model_validate",
        "model_validate_json",
        "model_validate_strings",
        "adapter_validate_python",
        "adapter_validate_json",
        "adapter_validate_strings",
    ],
)
@pytest.mark.parametrize("extra", [None, "allow", "ignore", "forbid"])
def test_firestore_valid_surfaces_ignore_unknown_content_without_retention(
    mode: str,
    extra: ValidationExtra,
) -> None:
    payload = _firestore_settings()
    payload["unknown_content"] = "retained-extra-secret"
    settings = _settings_surface(mode, payload, extra)
    assert settings.firestore_project_id == "cairn1"
    assert "unknown_content" not in settings.__dict__
    assert settings.model_extra is None


def _production_values(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "deployment_mode": "production",
        "provider": "ollama",
        "embedding_provider": "ollama",
    }
    values.update(updates)
    return values


def _assert_corpus_path_failure(
    error: BaseException,
    *canaries: str,
    log_text: str = "",
) -> None:
    assert type(error) is SettingsValidationError
    rendered = (
        str(error)
        + repr(error)
        + repr(error.args)
        + repr(vars(error))
        + repr(error.errors(include_input=True))
        + error.json(include_input=True)
        + repr(error.__cause__)
        + repr(error.__context__)
        + log_text
    )
    assert "CORPUS_PATH" in rendered
    assert all(canary not in rendered for canary in canaries)
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.parametrize(
    "corpus_path",
    [
        "/private/absolute-corpus",
        "relative-corpus",
        ".",
        "",
        "missing/nonexistent-corpus",
        "symlink-shaped-corpus",
    ],
)
def test_production_rejects_every_non_null_corpus_path_content_free(
    corpus_path: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(SettingsValidationError) as caught:
        Settings(**_production_values(corpus_path=corpus_path))  # type: ignore[arg-type]
    canaries = (corpus_path,) if len(corpus_path) > 1 else ()
    _assert_corpus_path_failure(caught.value, *canaries, log_text=caplog.text)


def test_production_before_guard_performs_no_path_protocol_or_filesystem_query() -> None:
    hooks = 0

    class HostilePathValue:
        def __fspath__(self) -> str:
            nonlocal hooks
            hooks += 1
            raise AssertionError

        def __repr__(self) -> str:
            nonlocal hooks
            hooks += 1
            raise AssertionError

    values = _production_values(corpus_path=HostilePathValue())
    guard = cast(Callable[[object], object], Settings.reject_production_corpus_input)
    with pytest.raises(ValueError, match="CORPUS_PATH"):
        guard(values)
    assert hooks == 0


@pytest.mark.parametrize(
    "mode",
    [
        "model_validate",
        "model_validate_json",
        "model_validate_strings",
        "adapter_validate_python",
        "adapter_validate_json",
        "adapter_validate_strings",
    ],
)
@pytest.mark.parametrize("extra", [None, "allow", "ignore", "forbid"])
def test_production_corpus_guard_covers_every_validation_surface(
    mode: str,
    extra: ValidationExtra,
    caplog: pytest.LogCaptureFixture,
) -> None:
    canary = "surface-private-corpus"
    caplog.set_level(logging.DEBUG)
    with pytest.raises(SettingsValidationError) as caught:
        _settings_surface(mode, _production_values(corpus_path=canary), extra)
    _assert_corpus_path_failure(caught.value, canary, log_text=caplog.text)


@pytest.mark.parametrize("deployment_mode", ["development", "test"])
@pytest.mark.parametrize(
    "mode",
    [
        "model_validate",
        "model_validate_json",
        "model_validate_strings",
        "adapter_validate_python",
        "adapter_validate_json",
        "adapter_validate_strings",
    ],
)
def test_nonproduction_corpus_paths_remain_valid_on_every_surface(
    deployment_mode: str,
    mode: str,
) -> None:
    result = _settings_surface(
        mode,
        {"deployment_mode": deployment_mode, "corpus_path": "safe-corpus"},
        None,
    )
    assert result.deployment_mode == deployment_mode
    assert result.corpus_path == Path("safe-corpus")


@pytest.mark.parametrize("corpus_value", [None])
def test_production_omitted_and_explicit_null_corpus_remain_valid(
    corpus_value: None,
) -> None:
    omitted = Settings.model_validate(_production_values())
    explicit = Settings.model_validate(_production_values(corpus_path=corpus_value))
    assert omitted.corpus_path is None
    assert explicit.corpus_path is None


@pytest.mark.parametrize(
    "conflict",
    [
        {"provider": "echo"},
        {"embedding_provider": "fake"},
        {"retrieval_backend": "firestore"},
        {"retrieval_top_k": "not-an-integer"},
        {"provider": "gemini", "gemini_api_key": None},
        {"unknown_field": "unknown-secret"},
    ],
)
def test_production_corpus_failure_precedes_every_conflicting_taxonomy(
    conflict: dict[str, object],
) -> None:
    canary = "priority-private-corpus"
    with pytest.raises(SettingsValidationError) as caught:
        Settings.model_validate(_production_values(corpus_path=canary) | conflict)
    _assert_corpus_path_failure(caught.value, canary, "unknown-secret")
    assert len(caught.value.errors()) == 1


def test_production_corpus_failure_precedes_debug_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_SDK_PYTHON_LOGGING_SCOPE", "debug-scope-secret")
    with pytest.raises(SettingsValidationError) as caught:
        Settings.model_validate(
            _production_values(
                corpus_path="debug-private-corpus",
                retrieval_backend="firestore",
            )
        )
    _assert_corpus_path_failure(
        caught.value,
        "debug-private-corpus",
        "debug-scope-secret",
    )


def test_field_parse_taxonomy_is_unchanged_without_production_corpus_pair() -> None:
    with pytest.raises(SettingsValidationError) as caught:
        Settings.model_validate({"retrieval_top_k": "not-an-integer"})
    assert "RETRIEVAL_TOP_K" in str(caught.value)
    assert "CORPUS_PATH" not in str(caught.value)


def test_uppercase_environment_and_cached_get_settings_reject_production_corpus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("DEPLOYMENT_MODE", "PRODUCTION")
    monkeypatch.setenv("PROVIDER", "OLLAMA")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "OLLAMA")
    monkeypatch.setenv("CORPUS_PATH", "environment-private-corpus")
    try:
        with pytest.raises(SettingsValidationError) as direct:
            Settings()
        with pytest.raises(SettingsValidationError) as cached:
            get_settings()
    finally:
        get_settings.cache_clear()
    _assert_corpus_path_failure(direct.value, "environment-private-corpus")
    _assert_corpus_path_failure(cached.value, "environment-private-corpus")


def test_validated_snapshot_revalidates_cached_get_settings_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _invalid_snapshot_settings()
    calls = 0

    def cached_settings() -> Settings:
        nonlocal calls
        calls += 1
        return source

    monkeypatch.setattr("app.config.get_settings", cached_settings)
    with pytest.raises(SettingsValidationError) as caught:
        validated_settings_snapshot()
    _assert_corpus_path_failure(caught.value, "snapshot-private-corpus")
    assert calls == 1


def _invalid_snapshot_settings() -> Settings:
    values = Settings().model_dump()
    values.update(
        deployment_mode="production",
        provider="ollama",
        embedding_provider="ollama",
        corpus_path=Path("snapshot-private-corpus"),
    )
    return Settings.model_construct(**values)


def test_validated_snapshot_rejects_every_validation_bypass_content_free() -> None:
    base = Settings(corpus_path=Path("development-corpus"))
    forged = []
    values = base.model_dump()
    invalid_update = {
        "deployment_mode": "production",
        "provider": "ollama",
        "embedding_provider": "ollama",
        "corpus_path": Path("snapshot-private-corpus"),
    }
    forged.append(Settings.model_construct(**(values | invalid_update)))
    with pytest.warns(DeprecationWarning):
        forged.append(Settings.construct(**(values | invalid_update)))
    forged.append(base.model_copy(update=invalid_update))
    with pytest.warns(DeprecationWarning):
        forged.append(base.copy(update=invalid_update))
    assigned = Settings()
    assigned.deployment_mode = "production"
    assigned.provider = "ollama"
    assigned.embedding_provider = "ollama"
    assigned.corpus_path = Path("snapshot-private-corpus")
    forged.append(assigned)
    raw_mutated = Settings()
    raw_mutated.__dict__.update(invalid_update)
    forged.append(raw_mutated)
    setattr_mutated = Settings()
    for name, value in invalid_update.items():
        object.__setattr__(setattr_mutated, name, value)
    forged.append(setattr_mutated)

    for item in forged:
        with pytest.raises(SettingsValidationError) as caught:
            validated_settings_snapshot(item)
        _assert_corpus_path_failure(caught.value, "snapshot-private-corpus")


def test_validated_snapshot_clones_every_field_path_and_secret() -> None:
    source = Settings(
        corpus_path=Path("development-corpus"),
        database_path=Path("custom.db"),
        chroma_path=Path("custom-chroma"),
        gemini_api_key=SecretStr("private-key"),
        system_instruction="custom instruction",
        cairn_port=8081,
    )
    snapshot = validated_settings_snapshot(source)

    assert type(snapshot) is Settings
    assert snapshot is not source
    assert snapshot.model_dump() == source.model_dump()
    assert snapshot.corpus_path is not source.corpus_path
    assert snapshot.database_path is not source.database_path
    assert snapshot.chroma_path is not source.chroma_path
    assert snapshot.gemini_api_key is not source.gemini_api_key
    assert snapshot.model_extra is None

    assert source.gemini_api_key is not None
    object.__setattr__(source.gemini_api_key, "_secret_value", "mutated-key")
    assert snapshot.gemini_api_key is not None
    assert snapshot.gemini_api_key.get_secret_value() == "private-key"


def test_explicit_snapshot_bypasses_all_base_settings_sources_and_filesystem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Settings(corpus_path=Path("safe-corpus"))
    calls: list[str] = []

    def tripwire(name: str) -> Callable[..., object]:
        def fail(*args: object, **kwargs: object) -> object:
            del args, kwargs
            calls.append(name)
            raise AssertionError(name)

        return fail

    intercepted = {
        "model_validate": tripwire("model_validate"),
        "settings_constructor": tripwire("settings_constructor"),
        "settings_sources": tripwire("settings_sources"),
        "path_stat": tripwire("path_stat"),
        "path_open": tripwire("path_open"),
        "path_read_text": tripwire("path_read_text"),
        "path_read_bytes": tripwire("path_read_bytes"),
    }
    with monkeypatch.context() as context:
        context.setattr(Settings, "model_validate", intercepted["model_validate"])
        context.setattr(Settings, "__init__", intercepted["settings_constructor"])
        context.setattr(
            Settings,
            "_settings_init_sources",
            intercepted["settings_sources"],
        )
        context.setattr(Path, "stat", intercepted["path_stat"])
        context.setattr(Path, "open", intercepted["path_open"])
        context.setattr(Path, "read_text", intercepted["path_read_text"])
        context.setattr(Path, "read_bytes", intercepted["path_read_bytes"])
        snapshot = validated_settings_snapshot(source)

    assert type(snapshot) is Settings
    assert snapshot is not source
    assert snapshot.corpus_path == Path("safe-corpus")
    assert calls == []
    for name, fail in intercepted.items():
        with pytest.raises(AssertionError, match=name):
            fail()


def test_snapshot_preserves_later_firestore_debug_scope_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Settings.model_validate(_firestore_settings())
    canary = "snapshot-debug-scope-canary"
    monkeypatch.setenv("GOOGLE_SDK_PYTHON_LOGGING_SCOPE", canary)

    with pytest.raises(SettingsValidationError) as caught:
        validated_settings_snapshot(source)

    rendered = str(caught.value) + repr(caught.value) + repr(caught.value.args)
    assert "GOOGLE_SDK_PYTHON_LOGGING_SCOPE" in rendered
    assert canary not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_validated_snapshot_rejects_subclass_facsimile_and_hostile_raw_state_without_hooks(
) -> None:
    hooks = 0

    class HookedSettings(Settings):
        def __getattribute__(self, name: str) -> object:
            nonlocal hooks
            hooks += 1
            raise AssertionError(name)

        def __repr__(self) -> str:
            nonlocal hooks
            hooks += 1
            raise AssertionError

    def forbidden_hook(*args: object, **kwargs: object) -> object:
        nonlocal hooks
        del args, kwargs
        hooks += 1
        raise AssertionError

    for hook_name in ("model_dump", "__iter__"):
        setattr(HookedSettings, hook_name, forbidden_hook)
    hooks = 0

    class Facsimile:
        deployment_mode = "production"
        corpus_path = "facsimile-private-corpus"

    class HostileDict(dict[object, object]):
        def __repr__(self) -> str:
            nonlocal hooks
            hooks += 1
            raise AssertionError

    subclass = object.__new__(HookedSettings)
    for invalid in (subclass, Facsimile()):
        with pytest.raises(SettingsValidationError) as caught:
            validated_settings_snapshot(cast(Settings, invalid))
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None

    hostile_state = Settings()
    object.__setattr__(hostile_state, "__dict__", HostileDict(hostile_state.__dict__))
    with pytest.raises(SettingsValidationError):
        validated_settings_snapshot(hostile_state)
    assert hooks == 0


def test_validated_snapshot_ignores_hostile_extras_without_hash_equality_or_value_hooks() -> None:
    hooks = 0

    class CollidingKey:
        def __hash__(self) -> int:
            nonlocal hooks
            hooks += 1
            return hash("deployment_mode")

        def __eq__(self, other: object) -> bool:
            nonlocal hooks
            hooks += 1
            return False

        def __repr__(self) -> str:
            nonlocal hooks
            hooks += 1
            raise AssertionError

    class HostileValue:
        def __repr__(self) -> str:
            nonlocal hooks
            hooks += 1
            raise AssertionError

    source = Settings()
    raw_state = cast(dict[object, object], source.__dict__)
    colliding = object.__new__(CollidingKey)
    dict.__setitem__(raw_state, colliding, HostileValue())
    hooks = 0

    snapshot = validated_settings_snapshot(source)

    assert snapshot.deployment_mode == "development"
    assert hooks == 0
    assert all(type(key) is str for key in snapshot.__dict__)


def test_production_before_guard_ignores_hash_colliding_extra_without_hooks() -> None:
    hooks = 0

    class CollidingKey:
        def __hash__(self) -> int:
            nonlocal hooks
            hooks += 1
            return hash("deployment_mode")

        def __eq__(self, other: object) -> bool:
            nonlocal hooks
            hooks += 1
            return False

        def __repr__(self) -> str:
            nonlocal hooks
            hooks += 1
            raise AssertionError

    class HostileValue:
        def __repr__(self) -> str:
            nonlocal hooks
            hooks += 1
            raise AssertionError

    values = cast(
        dict[object, object],
        dict(_production_values(corpus_path="guard-private-corpus")),
    )
    dict.__setitem__(values, object.__new__(CollidingKey), HostileValue())
    hooks = 0

    with pytest.raises(ValueError, match="CORPUS_PATH"):
        guard = cast(Callable[[object], object], Settings.reject_production_corpus_input)
        guard(values)
    assert hooks == 0


@pytest.mark.parametrize(
    "field",
    [
        "deployment_mode",
        "cairn_port",
        "retrieval_max_distance",
        "corpus_path",
        "gemini_api_key",
    ],
)
def test_validated_snapshot_rejects_hostile_declared_raw_values_without_hooks(
    field: str,
) -> None:
    hooks = 0

    class HostileString(str):
        def __str__(self) -> str:
            nonlocal hooks
            hooks += 1
            raise AssertionError

    class HostileInteger(int):
        def __int__(self) -> int:
            nonlocal hooks
            hooks += 1
            raise AssertionError

    class HostileFloat(float):
        def __float__(self) -> float:
            nonlocal hooks
            hooks += 1
            raise AssertionError

    class HostilePath(Path):
        def __fspath__(self) -> str:
            nonlocal hooks
            hooks += 1
            raise AssertionError

        def __str__(self) -> str:
            nonlocal hooks
            hooks += 1
            raise AssertionError

    class HostileSecret(SecretStr):
        def __repr__(self) -> str:
            nonlocal hooks
            hooks += 1
            raise AssertionError

    hostile: object = {
        "deployment_mode": HostileString("production"),
        "cairn_port": HostileInteger(8080),
        "retrieval_max_distance": HostileFloat(1.0),
        "corpus_path": HostilePath("private-path"),
        "gemini_api_key": HostileSecret("private-key"),
    }[field]
    hooks = 0
    source = Settings()
    dict.__setitem__(source.__dict__, field, hostile)

    with pytest.raises(SettingsValidationError) as caught:
        validated_settings_snapshot(source)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert hooks == 0


def test_invalid_snapshot_error_has_no_recursive_raw_retention_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    canary = "snapshot-unsafe-value-canary"

    class UnsafeString(str):
        pass

    source = Settings()
    dict.__setitem__(source.__dict__, "deployment_mode", UnsafeString(canary))
    caplog.set_level(logging.DEBUG)
    with pytest.raises(SettingsValidationError) as caught:
        validated_settings_snapshot(source)

    error = caught.value
    rendered = (
        str(error)
        + repr(error)
        + repr(error.args)
        + repr(vars(error))
        + repr(error.errors(include_input=True))
        + error.json(include_input=True)
        + repr(error.__cause__)
        + repr(error.__context__)
        + caplog.text
    )
    assert canary not in rendered
    assert error.__cause__ is None
    assert error.__context__ is None


def test_validated_snapshot_rejects_missing_field_and_ignores_hooked_extra() -> None:
    hooks = 0

    class HookedExtra:
        def __repr__(self) -> str:
            nonlocal hooks
            hooks += 1
            raise AssertionError

    source = Settings()
    dict.__delitem__(source.__dict__, "provider")
    dict.__setitem__(source.__dict__, "unknown_extra", HookedExtra())
    with pytest.raises(SettingsValidationError):
        validated_settings_snapshot(source)
    assert hooks == 0


@pytest.mark.parametrize("path_failure", ["list-subclass", "component", "missing"])
def test_validated_snapshot_rejects_hostile_nested_path_state_without_hooks(
    path_failure: str,
) -> None:
    hooks = 0

    class HostileList(list[str]):
        def __iter__(self) -> Iterator[str]:
            nonlocal hooks
            hooks += 1
            raise AssertionError

    class HostileComponent(str):
        def __str__(self) -> str:
            nonlocal hooks
            hooks += 1
            raise AssertionError

    source = Settings(corpus_path=Path("safe-corpus"))
    path = cast(Path, source.corpus_path)
    if path_failure == "list-subclass":
        object.__setattr__(path, "_raw_paths", HostileList(["safe-corpus"]))
    elif path_failure == "component":
        object.__setattr__(path, "_raw_paths", [HostileComponent("safe-corpus")])
    else:
        object.__delattr__(path, "_raw_paths")

    with pytest.raises(SettingsValidationError):
        validated_settings_snapshot(source)
    assert hooks == 0


def test_validated_snapshot_ignores_hostile_path_caches() -> None:
    class HostileCache:
        def __repr__(self) -> str:
            raise AssertionError

    source = Settings(corpus_path=Path("safe-corpus"))
    path = cast(Path, source.corpus_path)
    for cache_name in ("_str", "_tail_cached", "_hash"):
        object.__setattr__(path, cache_name, HostileCache())

    snapshot = validated_settings_snapshot(source)
    assert snapshot.corpus_path == Path("safe-corpus")


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
