import json
import math
import os
import re
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.api.contracts import OUTPUT_CHARS_MAX, OUTPUT_TOKENS_MAX, SYSTEM_INSTRUCTION_MAX_CHARS

DEFAULT_DB_PATH = Path("data/cairn.db")
DEFAULT_CHROMA_PATH = Path("data/chroma")
_CONCRETE_PATH_TYPE = type(Path())
_FIRESTORE_PROJECT_PATTERN = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_SETTINGS_VALIDATION_DEPTH: ContextVar[int] = ContextVar(
    "settings_validation_depth", default=0
)
_SAFE_SETTING_NAMES = (
    "CAIRN_PORT",
    "CORPUS_PATH",
    "DATABASE_PATH",
    "DEPLOYMENT_MODE",
    "EMBEDDING_PROVIDER",
    "FIRESTORE_CORPUS_ID",
    "FIRESTORE_CORPUS_VERSION",
    "FIRESTORE_DISTANCE_MEASURE",
    "FIRESTORE_EMBEDDING_DIMENSIONS",
    "FIRESTORE_EMBEDDING_IDENTITY",
    "FIRESTORE_MAX_DISTANCE",
    "FIRESTORE_MAX_RETRIES",
    "FIRESTORE_PROJECT_ID",
    "FIRESTORE_QUERY_TIMEOUT_SECONDS",
    "GEMINI_API_KEY",
    "GEMINI_MAX_RETRIES",
    "GEMINI_MODEL",
    "GEMINI_TIMEOUT_SECONDS",
    "GOOGLE_SDK_PYTHON_LOGGING_SCOPE",
    "MAX_OUTPUT_CHARS",
    "MAX_OUTPUT_TOKENS",
    "PROVIDER",
    "PUBLIC_BUDGET_CURRENCY",
    "PUBLIC_BUDGET_DAILY",
    "PUBLIC_BUDGET_HOURLY",
    "PUBLIC_BUDGET_RESERVE_PER_ATTEMPT",
    "PUBLIC_CONTROL_DIGEST_KEY",
    "PUBLIC_CONTROL_MAX_KEYS",
    "PUBLIC_ENDPOINT_CONTROLS_ENABLED",
    "PUBLIC_LEASE_RENEW_SECONDS",
    "PUBLIC_LEASE_TTL_SECONDS",
    "PUBLIC_MAX_ACTIVE_REQUESTS",
    "PUBLIC_MAX_QUEUED_REQUESTS",
    "PUBLIC_QUEUE_WAIT_SECONDS",
    "PUBLIC_RATE_IP_CAPACITY",
    "PUBLIC_RATE_IP_REFILL_PER_MINUTE",
    "PUBLIC_RATE_SESSION_CAPACITY",
    "PUBLIC_RATE_SESSION_REFILL_PER_MINUTE",
    "PUBLIC_RATE_STATE_TTL_SECONDS",
    "RETRIEVAL_BACKEND",
    "RETRIEVAL_MAX_DISTANCE",
    "RETRIEVAL_TOP_K",
    "SYSTEM_INSTRUCTION",
)
_PRODUCTION_CORPUS_GUARD_NAMES = frozenset(("deployment_mode", "corpus_path"))
_INVALID_SNAPSHOT_VALUE = object()


class SettingsValidationError(ValidationError):
    """ValidationError-compatible settings failure without rejected values."""

    _safe_errors: tuple[dict[str, object], ...]

    @classmethod
    def sanitized(cls, error: ValidationError) -> "SettingsValidationError":
        if isinstance(error, cls):
            return error

        def safe_error(item: Any) -> dict[str, object]:
            location = tuple(item.get("loc", ()))
            message = str(item.get("msg", ""))
            setting_name = next(
                (
                    name
                    for name in _SAFE_SETTING_NAMES
                    if name in message
                    or any(
                        isinstance(part, str) and part.upper() == name
                        for part in location
                    )
                ),
                None,
            )
            safe_message = (
                f"{setting_name} is invalid."
                if setting_name is not None
                else "Settings configuration is invalid."
            )
            return {
                "type": "settings_invalid",
                "loc": location,
                "msg": safe_message,
            }

        safe_errors = tuple(
            safe_error(item)
            for item in error.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            )
        )
        sanitized = cls.from_exception_data(
            "Settings",
            [
                {
                    "type": "value_error",
                    "loc": (),
                    "input": None,
                    "ctx": {"error": ValueError("Settings configuration is invalid.")},
                }
            ],
            hide_input=True,
        )
        sanitized._safe_errors = safe_errors
        return sanitized

    def __str__(self) -> str:
        parts = []
        for item in self._safe_errors:
            location = ".".join(
                str(value) for value in cast(tuple[object, ...], item["loc"])
            )
            prefix = f"{location}: " if location else ""
            parts.append(prefix + str(item["msg"]))
        return "Settings configuration is invalid. " + "; ".join(parts)

    def __repr__(self) -> str:
        return "SettingsValidationError()"

    def errors(
        self,
        *,
        include_url: bool = True,
        include_context: bool = True,
        include_input: bool = True,
    ) -> list[Any]:
        del include_url, include_context, include_input
        return [dict(item) for item in self._safe_errors]

    def json(
        self,
        *,
        indent: int | None = None,
        include_url: bool = True,
        include_context: bool = True,
        include_input: bool = True,
    ) -> str:
        return json.dumps(
            self.errors(
                include_url=include_url,
                include_context=include_context,
                include_input=include_input,
            ),
            indent=indent,
            separators=None if indent is not None else (",", ":"),
        )


class _ContentFreeSettingsValidator:
    def __init__(self, validator: Any) -> None:
        self._validator = validator

    def _validate(
        self,
        method_name: Literal["validate_python", "validate_json", "validate_strings"],
        value: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        kwargs["extra"] = "ignore"
        depth = _SETTINGS_VALIDATION_DEPTH.get()
        token = _SETTINGS_VALIDATION_DEPTH.set(depth + 1)
        failure: SettingsValidationError | None = None
        try:
            return getattr(self._validator, method_name)(value, *args, **kwargs)
        except SettingsValidationError as error:
            if depth:
                raise
            failure = error
        except ValidationError as error:
            if depth:
                raise
            failure = SettingsValidationError.sanitized(error)
        finally:
            _SETTINGS_VALIDATION_DEPTH.reset(token)
        assert failure is not None
        raise failure from None

    def validate_python(self, value: Any, *args: Any, **kwargs: Any) -> Any:
        return self._validate("validate_python", value, *args, **kwargs)

    def validate_json(self, value: Any, *args: Any, **kwargs: Any) -> Any:
        return self._validate("validate_json", value, *args, **kwargs)

    def validate_strings(self, value: Any, *args: Any, **kwargs: Any) -> Any:
        return self._validate("validate_strings", value, *args, **kwargs)

    def validate_assignment(self, *args: Any, **kwargs: Any) -> Any:
        failure: SettingsValidationError | None = None
        try:
            return self._validator.validate_assignment(*args, **kwargs)
        except SettingsValidationError as error:
            failure = error
        except ValidationError as error:
            failure = SettingsValidationError.sanitized(error)
        assert failure is not None
        raise failure from None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._validator, name)


class ContentFreeBaseSettings(BaseSettings):
    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        validator = cls.__pydantic_validator__
        if not isinstance(validator, _ContentFreeSettingsValidator):
            cls.__pydantic_validator__ = _ContentFreeSettingsValidator(validator)  # type: ignore[assignment]


class Settings(ContentFreeBaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b-instruct"
    deployment_mode: Literal["development", "test", "production"] = "development"
    provider: Literal["echo", "ollama", "gemini"] = "echo"
    embedding_provider: Literal["fake", "ollama"] = "fake"
    gemini_api_key: SecretStr | None = None
    gemini_model: Literal["gemini-3.8-flash"] = "gemini-3.8-flash"
    gemini_timeout_seconds: float = Field(default=30.0, gt=0, le=180, allow_inf_nan=False)
    gemini_max_retries: Literal[0, 1] = 1
    admin_bootstrap_password: str = ""
    # Host port the stack is published on (compose maps it to the
    # container's 8000). Only used to derive the default origin allowlist.
    cairn_port: int = 8080
    origin_allowlist: str = ""
    chat_message_max_chars: int = 500
    system_instruction: str = Field(default="", max_length=SYSTEM_INSTRUCTION_MAX_CHARS)
    max_output_tokens: int = Field(default=OUTPUT_TOKENS_MAX, ge=1, le=OUTPUT_TOKENS_MAX)
    max_output_chars: int = Field(default=OUTPUT_CHARS_MAX, ge=1, le=OUTPUT_CHARS_MAX)
    database_path: Path = DEFAULT_DB_PATH
    chroma_path: Path = DEFAULT_CHROMA_PATH
    # None preserves the deterministic local/test startup path. The live
    # Compose override sets this to its read-only operator corpus mount.
    corpus_path: Path | None = None
    embedding_model: str = "nomic-embed-text"
    retrieval_top_k: Annotated[int, Field(ge=1, le=6)] = 4
    retrieval_max_distance: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 1.2
    retrieval_backend: Literal["local", "firestore"] = "local"
    firestore_project_id: str = ""
    firestore_corpus_id: str = ""
    firestore_corpus_version: str = ""
    firestore_embedding_identity: str = ""
    firestore_embedding_dimensions: int | None = None
    firestore_distance_measure: Literal["", "cosine", "euclidean"] = ""
    firestore_max_distance: float | None = None
    firestore_query_timeout_seconds: int | None = None
    firestore_max_retries: Literal[0, 1] | None = None

    rate_limit_ip_capacity: float = 20
    rate_limit_ip_refill_per_minute: float = 20
    rate_limit_session_capacity: float = 10
    rate_limit_session_refill_per_minute: float = 10

    public_endpoint_controls_enabled: bool = False
    trusted_proxy_cidrs: str = ""
    public_control_digest_key: SecretStr | None = None
    public_rate_ip_capacity: int | None = None
    public_rate_ip_refill_per_minute: str = ""
    public_rate_session_capacity: int | None = None
    public_rate_session_refill_per_minute: str = ""
    public_rate_state_ttl_seconds: int | None = None
    public_control_max_keys: int | None = None
    public_max_active_requests: int | None = None
    public_max_queued_requests: int | None = None
    public_queue_wait_seconds: str = ""
    public_lease_ttl_seconds: str = ""
    public_lease_renew_seconds: str = ""
    public_budget_currency: str = ""
    public_budget_hourly: str = ""
    public_budget_daily: str = ""
    public_budget_reserve_per_attempt: str = ""

    @model_validator(mode="before")
    @classmethod
    def reject_production_corpus_input(cls, value: object) -> object:
        if type(value) is not dict:
            return value
        selected: dict[str, object] = {}
        for item in dict.items(cast(dict[object, object], value)):
            key = tuple.__getitem__(item, 0)
            if type(key) is str and key in _PRODUCTION_CORPUS_GUARD_NAMES:
                selected[key] = tuple.__getitem__(item, 1)
        mode = selected.get("deployment_mode")
        if (
            type(mode) is str
            and str.lower(mode) == "production"
            and "corpus_path" in selected
            and selected["corpus_path"] is not None
        ):
            raise ValueError("CORPUS_PATH is invalid.")
        return value

    @field_validator("retrieval_top_k", mode="before")
    @classmethod
    def validate_retrieval_top_k_type(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("retrieval_top_k must be an integer")
        if isinstance(value, str):
            if not value.isascii() or not value.isdecimal():
                raise ValueError("retrieval_top_k must be an integer")
            return int(value)
        if type(value) is not int:
            raise ValueError("retrieval_top_k must be an integer")
        return value

    @field_validator("retrieval_max_distance", mode="before")
    @classmethod
    def validate_retrieval_max_distance_type(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("retrieval_max_distance must be a finite non-negative number")
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError as error:
                raise ValueError(
                    "retrieval_max_distance must be a finite non-negative number"
                ) from error
        if type(value) not in {int, float}:
            raise ValueError("retrieval_max_distance must be a finite non-negative number")
        try:
            finite = math.isfinite(float(cast(int | float, value)))
        except OverflowError as error:
            raise ValueError(
                "retrieval_max_distance must be a finite non-negative number"
            ) from error
        if not finite:
            raise ValueError("retrieval_max_distance must be a finite non-negative number")
        return value

    @field_validator("deployment_mode", "provider", "embedding_provider", mode="before")
    @classmethod
    def normalize_selection(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value

    @field_validator("gemini_api_key", mode="before")
    @classmethod
    def trim_gemini_api_key(cls, value: object) -> object:
        if isinstance(value, SecretStr):
            return value.get_secret_value().strip()
        return value.strip() if isinstance(value, str) else value

    @field_validator(
        "gemini_max_retries",
        "firestore_embedding_dimensions",
        "firestore_query_timeout_seconds",
        "firestore_max_retries",
        mode="before",
    )
    @classmethod
    def validate_optional_strict_integer(cls, value: object, info: object) -> object:
        field_name = getattr(info, "field_name", "firestore setting")
        if value == "":
            return None
        if isinstance(value, bool):
            raise ValueError(f"{field_name.upper()} must be a strict integer")
        if isinstance(value, str):
            if not value.isascii() or not value.isdecimal():
                raise ValueError(f"{field_name.upper()} must be a strict integer")
            return int(value)
        if type(value) is not int and value is not None:
            raise ValueError(f"{field_name.upper()} must be a strict integer")
        return value

    @field_validator(
        "public_rate_ip_capacity",
        "public_rate_session_capacity",
        "public_rate_state_ttl_seconds",
        "public_control_max_keys",
        "public_max_active_requests",
        "public_max_queued_requests",
        mode="before",
    )
    @classmethod
    def validate_public_control_integer(cls, value: object, info: object) -> object:
        field_name = getattr(info, "field_name", "public control setting")
        if value is None or (type(value) is str and value == ""):
            return None
        if type(value) is int:
            return value
        if (
            type(value) is str
            and 1 <= len(value) <= 10
            and value.isascii()
            and value.isdecimal()
        ):
            return int(value)
        raise ValueError(f"{field_name.upper()} must be a strict integer")

    @field_validator("public_endpoint_controls_enabled", mode="before")
    @classmethod
    def validate_public_controls_switch(cls, value: object) -> object:
        if type(value) is bool:
            return value
        if type(value) is str and len(value) in {4, 5} and value.lower() in {"true", "false"}:
            return value.lower() == "true"
        raise ValueError("PUBLIC_ENDPOINT_CONTROLS_ENABLED must be a strict boolean")

    @field_validator("firestore_max_distance", mode="before")
    @classmethod
    def validate_firestore_distance_type(cls, value: object) -> object:
        if value == "":
            return None
        if isinstance(value, bool):
            raise ValueError("FIRESTORE_MAX_DISTANCE must be a finite non-negative number")
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError as error:
                raise ValueError(
                    "FIRESTORE_MAX_DISTANCE must be a finite non-negative number"
                ) from error
        if type(value) not in {int, float} and value is not None:
            raise ValueError("FIRESTORE_MAX_DISTANCE must be a finite non-negative number")
        if value is not None:
            numeric = cast(int | float, value)
            if not math.isfinite(float(numeric)) or numeric < 0:
                raise ValueError("FIRESTORE_MAX_DISTANCE must be a finite non-negative number")
        return value

    @model_validator(mode="after")
    def validate_provider_pair(self) -> "Settings":
        if self.deployment_mode == "production" and self.corpus_path is not None:
            raise ValueError("CORPUS_PATH is invalid.")
        hosted_values: tuple[tuple[str, object], ...] = (
            ("FIRESTORE_PROJECT_ID", self.firestore_project_id),
            ("FIRESTORE_CORPUS_ID", self.firestore_corpus_id),
            ("FIRESTORE_CORPUS_VERSION", self.firestore_corpus_version),
            ("FIRESTORE_EMBEDDING_IDENTITY", self.firestore_embedding_identity),
            ("FIRESTORE_EMBEDDING_DIMENSIONS", self.firestore_embedding_dimensions),
            ("FIRESTORE_DISTANCE_MEASURE", self.firestore_distance_measure),
            ("FIRESTORE_MAX_DISTANCE", self.firestore_max_distance),
            ("FIRESTORE_QUERY_TIMEOUT_SECONDS", self.firestore_query_timeout_seconds),
            ("FIRESTORE_MAX_RETRIES", self.firestore_max_retries),
        )
        if self.retrieval_backend == "local":
            for name, value in hosted_values:
                if value not in (None, ""):
                    raise ValueError(f"{name} must be blank when RETRIEVAL_BACKEND=local")
        else:
            if self.deployment_mode == "production":
                raise ValueError("RETRIEVAL_BACKEND=firestore is not available in production")
            if self.corpus_path is not None:
                raise ValueError("CORPUS_PATH must be unset when RETRIEVAL_BACKEND=firestore")
            if os.getenv("GOOGLE_SDK_PYTHON_LOGGING_SCOPE", "").strip():
                raise ValueError(
                    "GOOGLE_SDK_PYTHON_LOGGING_SCOPE must be blank when RETRIEVAL_BACKEND=firestore"
                )
            for name, value in hosted_values:
                if value in (None, ""):
                    raise ValueError(f"{name} is required when RETRIEVAL_BACKEND=firestore")
            if _FIRESTORE_PROJECT_PATTERN.fullmatch(self.firestore_project_id) is None:
                raise ValueError("FIRESTORE_PROJECT_ID is invalid")
            from app.retrieval_contracts import ExactCorpusReference
            try:
                ExactCorpusReference(
                    corpus_id=self.firestore_corpus_id,
                    corpus_version="v1",
                )
            except ValidationError:
                raise ValueError("FIRESTORE_CORPUS_ID is invalid") from None
            try:
                ExactCorpusReference(
                    corpus_id="docs",
                    corpus_version=self.firestore_corpus_version,
                )
            except ValidationError:
                raise ValueError("FIRESTORE_CORPUS_VERSION is invalid") from None
            identity = self.firestore_embedding_identity
            if (
                identity != identity.strip()
                or not 1 <= len(identity) <= 256
                or not identity.isascii()
                or any(ord(char) < 32 or ord(char) > 126 for char in identity)
            ):
                raise ValueError("FIRESTORE_EMBEDDING_IDENTITY is invalid")
            dimensions = self.firestore_embedding_dimensions
            if dimensions is None or not 1 <= dimensions <= 2_048:
                raise ValueError("FIRESTORE_EMBEDDING_DIMENSIONS is invalid")
            if self.firestore_distance_measure not in ("cosine", "euclidean"):
                raise ValueError("FIRESTORE_DISTANCE_MEASURE is invalid")
            timeout = self.firestore_query_timeout_seconds
            if timeout is None or not 1 <= timeout <= 30:
                raise ValueError("FIRESTORE_QUERY_TIMEOUT_SECONDS is invalid")
        if self.provider == "gemini" and (
            self.gemini_api_key is None or not self.gemini_api_key.get_secret_value()
        ):
            raise ValueError("GEMINI_API_KEY is required when PROVIDER=gemini")
        if self.deployment_mode == "production":
            if self.provider not in ("ollama", "gemini"):
                raise ValueError("production PROVIDER must be ollama or gemini")
            if self.embedding_provider != "ollama":
                raise ValueError("production EMBEDDING_PROVIDER must be ollama")
        return self

    @property
    def origins(self) -> list[str]:
        # Empty allowlist means "the app's own published origin": the demo
        # and admin pages are served same-origin, and the chat endpoint
        # rejects any Origin outside this list — so the default has to
        # track cairn_port or a port override silently breaks the demo.
        if not self.origin_allowlist.strip():
            return [f"http://localhost:{self.cairn_port}"]
        return [origin.strip() for origin in self.origin_allowlist.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


_SETTINGS_FIELD_NAMES = tuple(Settings.model_fields)
_SETTINGS_FIELD_NAME_SET = frozenset(_SETTINGS_FIELD_NAMES)
_SAFE_SNAPSHOT_SCALAR_TYPES = frozenset((str, int, float, bool, type(None)))


def _fixed_settings_validation_error() -> SettingsValidationError:
    error = SettingsValidationError.from_exception_data(
        "Settings",
        [
            {
                "type": "value_error",
                "loc": (),
                "input": None,
                "ctx": {"error": ValueError("Settings configuration is invalid.")},
            }
        ],
        hide_input=True,
    )
    error._safe_errors = (
        {
            "type": "settings_invalid",
            "loc": (),
            "msg": "Settings configuration is invalid.",
        },
    )
    return error


def _clone_snapshot_value(value: object) -> object:
    if type(value) in _SAFE_SNAPSHOT_SCALAR_TYPES:
        return value
    if type(value) is _CONCRETE_PATH_TYPE:
        try:
            raw_paths = object.__getattribute__(value, "_raw_paths")
        except Exception:
            return _INVALID_SNAPSHOT_VALUE
        if type(raw_paths) is not list:
            return _INVALID_SNAPSHOT_VALUE
        components: list[str] = []
        count = list.__len__(raw_paths)
        for index in range(count):
            component = list.__getitem__(raw_paths, index)
            if type(component) is not str:
                return _INVALID_SNAPSHOT_VALUE
            components.append(component)
        try:
            clone = _CONCRETE_PATH_TYPE(*components)
        except Exception:
            return _INVALID_SNAPSHOT_VALUE
        return clone if type(clone) is _CONCRETE_PATH_TYPE else _INVALID_SNAPSHOT_VALUE
    if type(value) is SecretStr:
        try:
            secret = object.__getattribute__(value, "_secret_value")
        except Exception:
            return _INVALID_SNAPSHOT_VALUE
        if type(secret) is not str:
            return _INVALID_SNAPSHOT_VALUE
        return SecretStr(secret)
    return _INVALID_SNAPSHOT_VALUE


def validated_settings_snapshot(settings: Settings | None = None) -> Settings:
    """Return a fully revalidated, caller-independent settings snapshot."""
    source = get_settings() if settings is None else settings
    if type(source) is not Settings:
        raise _fixed_settings_validation_error() from None
    try:
        raw_state = object.__getattribute__(source, "__dict__")
    except Exception:
        raw_state = None
    if type(raw_state) is not dict:
        raise _fixed_settings_validation_error() from None

    controlled: dict[str, object] = {}
    unsafe = False
    for item in dict.items(cast(dict[object, object], raw_state)):
        key = tuple.__getitem__(item, 0)
        if type(key) is not str or key not in _SETTINGS_FIELD_NAME_SET:
            continue
        clone = _clone_snapshot_value(tuple.__getitem__(item, 1))
        if clone is _INVALID_SNAPSHOT_VALUE:
            unsafe = True
            continue
        controlled[key] = clone
    if unsafe or len(controlled) != len(_SETTINGS_FIELD_NAMES):
        raise _fixed_settings_validation_error() from None

    target = object.__new__(Settings)
    result: object = _INVALID_SNAPSHOT_VALUE
    try:
        result = Settings.__pydantic_validator__.validate_python(
            controlled,
            self_instance=target,
        )
    except SettingsValidationError:
        raise
    except Exception:
        pass
    if result is not target or type(result) is not Settings:
        raise _fixed_settings_validation_error() from None
    try:
        snapshot_state = object.__getattribute__(target, "__dict__")
    except Exception:
        snapshot_state = None
    if type(snapshot_state) is not dict:
        raise _fixed_settings_validation_error() from None
    seen = 0
    for item in dict.items(cast(dict[object, object], snapshot_state)):
        key = tuple.__getitem__(item, 0)
        if type(key) is not str or key not in _SETTINGS_FIELD_NAME_SET:
            raise _fixed_settings_validation_error() from None
        seen += 1
    if seen != len(_SETTINGS_FIELD_NAMES):
        raise _fixed_settings_validation_error() from None
    return target
