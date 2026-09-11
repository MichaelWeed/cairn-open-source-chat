"""Private, request-scoped provider-attempt accounting.

This module intentionally has no I/O, clock reads, request identifiers, or public
serialization seam.  A lexical request owner creates one session and receives its
one terminal summary after response-stream cleanup.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from types import BuiltinFunctionType, CoroutineType, FunctionType
from typing import Annotated, Any, Literal, Protocol, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetCoreSchemaHandler,
    StrictBool,
    StrictInt,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic_core import CoreSchema, core_schema

from app.providers.accounting import (
    ProviderAttemptAccountingInput,
    ProviderAttemptCostRecord,
    ProviderPriceSnapshot,
    price_provider_attempt,
)
from app.providers.contracts import (
    ProviderUsage,
    ProviderUsageChunk,
    ProviderUsageValidationError,
    merge_cumulative_usage,
    validate_model_identifier,
    validate_provider_identifier,
    validate_service_tier,
)

MAX_REQUEST_PRICE_SNAPSHOTS = 16

AttemptCompletion = Literal["completed", "error", "cancelled", "uncertain"]
RequestCompletion = Literal["completed", "refused", "limit", "error", "cancelled", "abandoned"]
UncertaintyReason = Literal[
    "finish_missing",
    "observer_failure",
    "identity_mismatch",
    "usage_invalid",
    "cleanup_uncertain",
]
_REQUEST_COMPLETIONS = {
    "completed",
    "refused",
    "limit",
    "error",
    "cancelled",
    "abandoned",
}
_ATTEMPT_COMPLETIONS = {"completed", "error", "cancelled"}
_UNCERTAINTY_REASONS = {
    "finish_missing",
    "observer_failure",
    "identity_mismatch",
    "usage_invalid",
    "cleanup_uncertain",
}
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_MISSING = object()
_APPLICATION_BINDING_AUTHORITY = object()
_PYTHON_DUMP_AUTHORITY = object()


class RequestAccountingError(Exception):
    """Fixed, content-free failure for the private accounting boundary."""

    def __init__(self) -> None:
        super().__init__("Request accounting input is invalid.")

    def __repr__(self) -> str:
        return "RequestAccountingError()"

    def __getattribute__(self, name: str) -> Any:
        if name in {"__traceback__", "__cause__", "__context__"}:
            return None
        return super().__getattribute__(name)

    def errors(
        self,
        *,
        include_url: bool = True,
        include_context: bool = True,
        include_input: bool = True,
    ) -> list[dict[str, object]]:
        del include_url, include_context, include_input
        return [
            {
                "type": "request_accounting_invalid",
                "loc": (),
                "msg": "Request accounting input is invalid.",
            }
        ]

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


class _AccountingPythonDump:
    __slots__ = ("state", "_brand")

    def __init__(self, state: dict[str, Any], *, authority: object = None) -> None:
        if authority is not _PYTHON_DUMP_AUTHORITY:
            raise RequestAccountingError from None
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "_brand", _PYTHON_DUMP_AUTHORITY)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise RequestAccountingError from None


def _content_free(operation: Callable[[], Any]) -> Any:
    result: object = _MISSING
    failed = False
    try:
        result = operation()
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        raise
    except BaseException:
        failed = True
    if failed or result is _MISSING:
        del operation
        raise RequestAccountingError from None
    return result


class _ContentFreeValidator:
    def __init__(self, validator: Any) -> None:
        self._validator = validator

    def _validate(self, method_name: str, value: Any, *args: Any, **kwargs: Any) -> Any:
        kwargs["extra"] = "forbid"
        result: object = _MISSING
        try:
            from_attributes = kwargs.get("from_attributes")
            if from_attributes is not None and (
                type(from_attributes) is not bool or from_attributes
            ):
                raise RequestAccountingError
            result = _content_free(
                lambda: getattr(self._validator, method_name)(value, *args, **kwargs)
            )
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            pass
        if result is _MISSING:
            del self, value, args, kwargs
            raise RequestAccountingError from None
        return result

    def validate_python(self, value: Any, *args: Any, **kwargs: Any) -> Any:
        result: object = _MISSING
        try:
            result = self._validate("validate_python", value, *args, **kwargs)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            pass
        if result is _MISSING:
            del self, value, args, kwargs
            raise RequestAccountingError from None
        return result

    def validate_json(self, value: Any, *args: Any, **kwargs: Any) -> Any:
        result: object = _MISSING
        try:
            result = self._validate("validate_json", value, *args, **kwargs)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            pass
        if result is _MISSING:
            del self, value, args, kwargs
            raise RequestAccountingError from None
        return result

    def validate_strings(self, value: Any, *args: Any, **kwargs: Any) -> Any:
        result: object = _MISSING
        try:
            result = self._validate("validate_strings", value, *args, **kwargs)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            pass
        if result is _MISSING:
            del self, value, args, kwargs
            raise RequestAccountingError from None
        return result

    def validate_assignment(self, *args: Any, **kwargs: Any) -> Any:
        return _content_free(lambda: self._validator.validate_assignment(*args, **kwargs))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._validator, name)


class RequestAccountingModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        hide_input_in_errors=True,
        revalidate_instances="always",
    )

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        validator = cls.__pydantic_validator__
        if not isinstance(validator, _ContentFreeValidator):
            cls.__pydantic_validator__ = _ContentFreeValidator(validator)  # type: ignore[assignment]

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.with_info_wrap_validator_function(
            lambda value, validator, info: _preflight_model_input(cls, value, validator, info.mode),
            handler(source_type),
        )

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise RequestAccountingError from None

    def __delattr__(self, name: str) -> None:
        del name
        raise RequestAccountingError from None

    @classmethod
    def model_validate(
        cls,
        obj: Any,
        *,
        strict: bool | None = None,
        extra: Any = None,
        from_attributes: bool | None = None,
        context: Any | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        result: object = _MISSING
        try:
            if from_attributes is not None and (
                type(from_attributes) is not bool or from_attributes
            ):
                raise RequestAccountingError
            result = super().model_validate(
                obj,
                strict=strict,
                extra=extra,
                from_attributes=from_attributes,
                context=context,
                by_alias=by_alias,
                by_name=by_name,
            )
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            pass
        if result is _MISSING:
            del obj, context
            raise RequestAccountingError from None
        return cast(Self, result)

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Callable[[Any], Any], info: Any) -> Any:
        safe_state = _source_free_python_dump(self)
        if info.mode == "python":
            return _AccountingPythonDump(safe_state, authority=_PYTHON_DUMP_AUTHORITY)
        return handler(self)

    def model_dump(
        self,
        *,
        mode: Literal["json", "python"] | str = "python",
        include: Any = None,
        exclude: Any = None,
        context: Any | None = None,
        by_alias: bool | None = None,
        exclude_unset: bool = False,
        exclude_defaults: bool = False,
        exclude_none: bool = False,
        exclude_computed_fields: bool = False,
        round_trip: bool = False,
        warnings: bool | Literal["none", "warn", "error"] = True,
        fallback: Callable[[Any], Any] | None = None,
        serialize_as_any: bool = False,
        polymorphic_serialization: bool | None = None,
    ) -> dict[str, Any]:
        if (
            mode == "python"
            and include is None
            and exclude is None
            and context is None
            and by_alias in {None, False}
            and not exclude_unset
            and not exclude_defaults
            and not exclude_none
            and not exclude_computed_fields
            and fallback is None
            and not serialize_as_any
            and polymorphic_serialization is None
        ):
            del round_trip, warnings
            return _source_free_python_dump(self)
        if mode == "python":
            raise RequestAccountingError from None
        return super().model_dump(
            mode=mode,
            include=include,
            exclude=exclude,
            context=context,
            by_alias=by_alias,
            exclude_unset=exclude_unset,
            exclude_defaults=exclude_defaults,
            exclude_none=exclude_none,
            exclude_computed_fields=exclude_computed_fields,
            round_trip=round_trip,
            warnings=warnings,
            fallback=fallback,
            serialize_as_any=serialize_as_any,
            polymorphic_serialization=polymorphic_serialization,
        )

    def model_dump_json(self, **kwargs: Any) -> str:
        _source_free_python_dump(self)
        return super().model_dump_json(**kwargs)

    @classmethod
    def _validated(cls, values: Any) -> Self:
        return cast(
            Self,
            _content_free(lambda: cls.model_validate(values, extra="forbid")),
        )

    @classmethod
    def model_construct(cls, _fields_set: set[str] | None = None, **values: Any) -> Self:
        try:
            if _fields_set is not None:
                if type(_fields_set) is not set or len(_fields_set) > len(cls.model_fields):
                    raise RequestAccountingError
                if any(type(item) is not str for item in set.__iter__(_fields_set)):
                    raise RequestAccountingError
                if not _fields_set.issubset(cls.model_fields):
                    raise RequestAccountingError
            result = cls._validated(values)
            if _fields_set is not None:
                object.__setattr__(result, "__pydantic_fields_set__", set(_fields_set))
            return result
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            raise RequestAccountingError from None

    @classmethod
    def construct(cls, _fields_set: set[str] | None = None, **values: Any) -> Self:
        return cls.model_construct(_fields_set=_fields_set, **values)

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        try:
            model_type = type(self)
            if model_type.__base__ is not RequestAccountingModel:
                raise RequestAccountingError
            fields = tuple(model_type.model_fields)
            values = _exact_model_state(self, model_type, fields)
            if update is not None:
                if type(update) is not dict or len(update) > len(fields):
                    raise RequestAccountingError
                for key, value in dict.items(update):
                    if type(key) is not str or key not in model_type.model_fields:
                        raise RequestAccountingError
                    values[key] = value
            del deep
            return type(self)._validated(values)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            raise RequestAccountingError from None

    def copy(
        self,
        *,
        include: Any = None,
        exclude: Any = None,
        update: dict[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if include is not None or exclude is not None:
            raise RequestAccountingError from None
        return self.model_copy(update=update, deep=deep)

    def __replace__(self, **changes: Any) -> Self:
        return self.model_copy(update=changes)


def _preflight_model_input(
    model: type[RequestAccountingModel],
    value: Any,
    handler: Callable[[Any], Any],
    mode: str,
) -> Any:
    def validate() -> Any:
        if type(value) is _AccountingPythonDump:
            if object.__getattribute__(value, "_brand") is not _PYTHON_DUMP_AUTHORITY:
                raise ValueError
            state = object.__getattribute__(value, "state")
            if type(state) is not dict or len(state) > len(model.model_fields):
                raise ValueError
            controlled_dump: dict[str, Any] = {}
            for key, item in dict.items(state):
                if type(key) is not str or key not in model.model_fields:
                    raise ValueError
                controlled_dump[key] = item
            _preflight_model_fields(model, controlled_dump, "python")
            return handler(controlled_dump)
        if type(value) is dict:
            if len(value) > len(model.model_fields):
                raise ValueError
            controlled: dict[str, Any] = {}
            for key, item in dict.items(value):
                if type(key) is not str or key not in model.model_fields:
                    raise ValueError
                controlled[key] = item
            _preflight_model_fields(model, controlled, mode)
            if mode == "json":
                if model is SettledAttempt:
                    if type(controlled.get("identity")) is dict:
                        controlled["identity"] = _copy_identity(
                            controlled["identity"], allow_dict=True
                        )
                    if type(controlled.get("cost_record")) is dict:
                        controlled["cost_record"] = _copy_cost_record(
                            controlled["cost_record"], allow_dict=True
                        )
                elif model is UncertainAttempt:
                    if type(controlled.get("identity")) is dict:
                        controlled["identity"] = _copy_identity(
                            controlled["identity"], allow_dict=True
                        )
                    if type(controlled.get("last_usage")) is dict:
                        controlled["last_usage"] = _copy_usage(
                            controlled["last_usage"], allow_dict=True
                        )
                elif model is RequestAccountingSummary and type(controlled.get("attempts")) is list:
                    attempts = controlled["attempts"]
                    if len(attempts) > 2:
                        raise ValueError
                    controlled["attempts"] = tuple(
                        _copy_attempt_record(item, allow_dict=True) for item in attempts
                    )
            return handler(controlled)
        if type(value) is model:
            raw_model = _exact_model_state(value, model, tuple(model.model_fields))
            _preflight_model_fields(model, raw_model, "python")
            return handler(value)
        if isinstance(value, (BaseModel, Mapping)):
            raise ValueError
        return handler(value)

    result: object = _MISSING
    try:
        result = _content_free(validate)
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        raise
    except BaseException:
        pass
    if result is _MISSING:
        del value, handler, validate
        raise RequestAccountingError from None
    return result


def _preflight_model_fields(
    model: type[RequestAccountingModel], values: dict[str, Any], mode: str
) -> None:
    def exact_string(name: str, *, optional: bool = False) -> None:
        value = dict.get(values, name, _MISSING)
        if value is _MISSING or (optional and value is None):
            return
        if type(value) is not str:
            raise ValueError

    def exact_integer(name: str) -> None:
        value = dict.get(values, name, _MISSING)
        if value is not _MISSING and type(value) is not int:
            raise ValueError

    if model is AttemptIdentity:
        exact_string("contract_version")
        exact_string("provider")
        exact_string("model")
        exact_integer("provider_attempt")
    elif model is ProviderAttemptPolicy:
        exact_string("contract_version")
        exact_string("provider")
        exact_string("model")
        exact_integer("max_attempts")
    elif model is SettledAttempt:
        exact_string("kind")
        exact_string("service_tier", optional=True)
        for name, expected in (
            ("identity", AttemptIdentity),
            ("cost_record", ProviderAttemptCostRecord),
        ):
            value = dict.get(values, name, _MISSING)
            valid = type(value) is expected or (mode == "json" and type(value) is dict)
            if value is not _MISSING and not valid:
                raise ValueError
    elif model is UncertainAttempt:
        exact_string("kind")
        exact_string("service_tier", optional=True)
        exact_string("completion")
        exact_string("reason")
        identity = dict.get(values, "identity", _MISSING)
        identity_valid = type(identity) is AttemptIdentity or (
            mode == "json" and type(identity) is dict
        )
        if identity is not _MISSING and not identity_valid:
            raise ValueError
        usage = dict.get(values, "last_usage", _MISSING)
        if (
            usage is not _MISSING
            and usage is not None
            and type(usage) is not ProviderUsage
            and not (mode == "json" and type(usage) is dict)
        ):
            raise ValueError
    elif model is RequestAccountingSummary:
        exact_string("contract_version")
        exact_string("request_completion")
        started = dict.get(values, "provider_work_started", _MISSING)
        if started is not _MISSING and type(started) is not bool:
            raise ValueError
        attempts = dict.get(values, "attempts", _MISSING)
        if (
            attempts is not _MISSING
            and type(attempts) is not tuple
            and not (mode == "json" and type(attempts) is list)
        ):
            raise ValueError


def _exact_model_state(
    value: object,
    expected_type: type[Any],
    fields: tuple[str, ...],
) -> dict[str, Any]:
    if type(value) is not expected_type:
        raise RequestAccountingError from None
    try:
        raw = object.__getattribute__(value, "__dict__")
        extra = object.__getattribute__(value, "__pydantic_extra__")
        private = object.__getattribute__(value, "__pydantic_private__")
        fields_set = object.__getattribute__(value, "__pydantic_fields_set__")
    except BaseException:
        raise RequestAccountingError from None
    if (
        type(raw) is not dict
        or extra is not None
        or private is not None
        or type(fields_set) is not set
        or len(fields_set) > len(fields)
        or len(raw) != len(fields)
    ):
        raise RequestAccountingError from None
    if any(type(item) is not str for item in set.__iter__(fields_set)):
        raise RequestAccountingError from None
    if not fields_set.issubset(set(fields)):
        raise RequestAccountingError from None
    items = tuple(dict.items(raw))
    if any(type(key) is not str for key, _ in items):
        raise RequestAccountingError from None
    if {key for key, _ in items} != set(fields):
        raise RequestAccountingError from None
    return {key: item for key, item in items}


def _exact_input_state(
    value: object,
    expected_type: type[Any],
    fields: tuple[str, ...],
    *,
    allow_dict: bool = False,
) -> dict[str, Any]:
    if type(value) is expected_type:
        return _exact_model_state(value, expected_type, fields)
    if not allow_dict or type(value) is not dict or len(value) != len(fields):
        raise RequestAccountingError from None
    items = tuple(dict.items(value))
    if any(type(key) is not str for key, _ in items):
        raise RequestAccountingError from None
    if {key for key, _ in items} != set(fields):
        raise RequestAccountingError from None
    return {key: item for key, item in items}


def _exact_optional_int(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise RequestAccountingError from None
    return value


def _copy_usage(value: object, *, allow_dict: bool = False) -> ProviderUsage:
    fields = tuple(ProviderUsage.model_fields)
    raw = _exact_input_state(value, ProviderUsage, fields, allow_dict=allow_dict)
    values = {name: _exact_optional_int(raw[name]) for name in fields}
    try:
        return ProviderUsage(**values)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        raise RequestAccountingError from None


def _copy_usage_chunk(value: object) -> ProviderUsageChunk:
    fields = tuple(ProviderUsageChunk.model_fields)
    raw = _exact_model_state(value, ProviderUsageChunk, fields)
    if type(raw["kind"]) is not str or raw["kind"] != "usage":
        raise RequestAccountingError from None
    if type(raw["schema_version"]) is not str or raw["schema_version"] != "1.0":
        raise RequestAccountingError from None
    if type(raw["provider"]) is not str or type(raw["model"]) is not str:
        raise RequestAccountingError from None
    if type(raw["provider_attempt"]) is not int:
        raise RequestAccountingError from None
    tier = raw["service_tier"]
    if tier is not None and type(tier) is not str:
        raise RequestAccountingError from None
    usage = _copy_usage(raw["usage"])
    try:
        return ProviderUsageChunk(
            provider=raw["provider"],
            model=raw["model"],
            provider_attempt=raw["provider_attempt"],
            service_tier=tier,
            usage=usage,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        raise RequestAccountingError from None


def _copy_snapshot(value: object) -> ProviderPriceSnapshot:
    fields = tuple(ProviderPriceSnapshot.model_fields)
    raw = _exact_model_state(value, ProviderPriceSnapshot, fields)
    string_fields = ("schema_version", "snapshot_id", "provider", "model", "currency", "source_url")
    if any(type(raw[name]) is not str for name in string_fields):
        raise RequestAccountingError from None
    tier = raw["service_tier"]
    through = raw["effective_through"]
    if tier is not None and type(tier) is not str:
        raise RequestAccountingError from None
    if type(raw["effective_from"]) is not date:
        raise RequestAccountingError from None
    if through is not None and type(through) is not date:
        raise RequestAccountingError from None
    rate_fields = (
        "uncached_input_rate_per_million",
        "cached_input_rate_per_million",
        "output_rate_per_million",
        "thinking_rate_per_million",
    )
    if any(type(raw[name]) is not Decimal for name in rate_fields):
        raise RequestAccountingError from None
    try:
        return ProviderPriceSnapshot(**{name: raw[name] for name in fields})
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        raise RequestAccountingError from None


def _copy_cost_record(value: object, *, allow_dict: bool = False) -> ProviderAttemptCostRecord:
    fields = tuple(ProviderAttemptCostRecord.model_fields)
    raw = _exact_input_state(value, ProviderAttemptCostRecord, fields, allow_dict=allow_dict)
    exact_strings = (
        "schema_version",
        "provider",
        "model",
        "completion_state",
        "answer_outcome",
        "cost_state",
    )
    if any(type(raw[name]) is not str for name in exact_strings):
        raise RequestAccountingError from None
    attempt_date = raw["attempt_date"]
    if type(attempt_date) is str:
        try:
            attempt_date = date.fromisoformat(attempt_date)
        except ValueError:
            raise RequestAccountingError from None
    if type(raw["provider_attempt"]) is not int or type(attempt_date) is not date:
        raise RequestAccountingError from None
    for name in ("service_tier", "snapshot_id", "currency"):
        if raw[name] is not None and type(raw[name]) is not str:
            raise RequestAccountingError from None
    model_cost = raw["model_cost"]
    if type(model_cost) is str:
        try:
            model_cost = Decimal(model_cost)
        except Exception:
            raise RequestAccountingError from None
    if model_cost is not None and type(model_cost) is not Decimal:
        raise RequestAccountingError from None
    usage = None if raw["usage"] is None else _copy_usage(raw["usage"], allow_dict=allow_dict)
    values = {name: raw[name] for name in fields}
    values["usage"] = usage
    values["attempt_date"] = attempt_date
    values["model_cost"] = model_cost
    try:
        return ProviderAttemptCostRecord(**values)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        raise RequestAccountingError from None


class AttemptIdentity(RequestAccountingModel):
    contract_version: Literal["1.0"] = "1.0"
    provider: str
    model: str
    provider_attempt: Annotated[StrictInt, Field(ge=1, le=2)]

    @field_validator("provider")
    @classmethod
    def _provider(cls, value: str) -> str:
        if type(value) is not str:
            raise ValueError
        return validate_provider_identifier(value)

    @field_validator("model")
    @classmethod
    def _model(cls, value: str) -> str:
        if type(value) is not str:
            raise ValueError
        return validate_model_identifier(value)


def _copy_identity(value: object, *, allow_dict: bool = False) -> AttemptIdentity:
    fields = tuple(AttemptIdentity.model_fields)
    raw = _exact_input_state(value, AttemptIdentity, fields, allow_dict=allow_dict)
    if any(type(raw[name]) is not str for name in ("contract_version", "provider", "model")):
        raise RequestAccountingError from None
    if type(raw["provider_attempt"]) is not int:
        raise RequestAccountingError from None
    return AttemptIdentity(**{name: raw[name] for name in fields})


class ProviderAttemptPolicy(RequestAccountingModel):
    contract_version: Literal["1.0"] = "1.0"
    provider: Literal["echo", "ollama", "gemini"]
    model: str
    max_attempts: Annotated[StrictInt, Field(ge=0, le=2)]

    @field_validator("model")
    @classmethod
    def _model(cls, value: str) -> str:
        if type(value) is not str:
            raise ValueError
        if value == "":
            return value
        return validate_model_identifier(value)

    @model_validator(mode="after")
    def _provider_attempts(self) -> Self:
        expected = 0 if self.provider == "echo" else 1 if self.provider == "ollama" else None
        if expected is not None and self.max_attempts != expected:
            raise ValueError
        if self.provider == "echo" and self.model != "":
            raise ValueError
        if self.provider != "echo" and self.model == "":
            raise ValueError
        if self.provider == "gemini" and self.max_attempts not in {1, 2}:
            raise ValueError
        return self


def _copy_policy(value: object) -> ProviderAttemptPolicy:
    fields = tuple(ProviderAttemptPolicy.model_fields)
    raw = _exact_model_state(value, ProviderAttemptPolicy, fields)
    if any(type(raw[name]) is not str for name in ("contract_version", "provider", "model")):
        raise RequestAccountingError from None
    if type(raw["max_attempts"]) is not int:
        raise RequestAccountingError from None
    return ProviderAttemptPolicy(**{name: raw[name] for name in fields})


class SettledAttempt(RequestAccountingModel):
    kind: Literal["settled"] = "settled"
    identity: AttemptIdentity
    service_tier: str | None = None
    cost_record: ProviderAttemptCostRecord

    @field_validator("identity", mode="before")
    @classmethod
    def _identity(cls, value: object) -> AttemptIdentity:
        return _copy_identity(value)

    @field_validator("service_tier")
    @classmethod
    def _tier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if type(value) is not str:
            raise ValueError
        return validate_service_tier(value)

    @field_validator("cost_record", mode="before")
    @classmethod
    def _cost(cls, value: object) -> ProviderAttemptCostRecord:
        return _copy_cost_record(value)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        record = self.cost_record
        if (
            record.provider != self.identity.provider
            or record.model != self.identity.model
            or record.provider_attempt != self.identity.provider_attempt
            or record.service_tier != self.service_tier
        ):
            raise ValueError
        return self


class UncertainAttempt(RequestAccountingModel):
    kind: Literal["uncertain"] = "uncertain"
    identity: AttemptIdentity
    service_tier: str | None = None
    completion: Literal["uncertain"] = "uncertain"
    last_usage: ProviderUsage | None = None
    reason: UncertaintyReason

    @field_validator("identity", mode="before")
    @classmethod
    def _identity(cls, value: object) -> AttemptIdentity:
        return _copy_identity(value)

    @field_validator("service_tier")
    @classmethod
    def _tier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if type(value) is not str:
            raise ValueError
        return validate_service_tier(value)

    @field_validator("last_usage", mode="before")
    @classmethod
    def _usage(cls, value: object) -> ProviderUsage | None:
        return None if value is None else _copy_usage(value)


AttemptRecord = Annotated[SettledAttempt | UncertainAttempt, Field(discriminator="kind")]


class RequestAccountingSummary(RequestAccountingModel):
    contract_version: Literal["1.0"] = "1.0"
    provider_work_started: StrictBool
    request_completion: RequestCompletion
    attempts: Annotated[tuple[AttemptRecord, ...], Field(max_length=2)] = ()

    @field_validator("attempts", mode="before")
    @classmethod
    def _attempt_tuple(cls, value: object) -> tuple[object, ...]:
        if type(value) is not tuple:
            raise ValueError
        if len(value) > 2:
            raise ValueError
        return tuple(_copy_attempt_record(item) for item in tuple.__iter__(value))

    @model_validator(mode="after")
    def _derived(self) -> Self:
        if self.provider_work_started is not bool(self.attempts):
            raise ValueError
        numbers = tuple(attempt.identity.provider_attempt for attempt in self.attempts)
        if numbers not in {(), (1,), (1, 2)}:
            raise ValueError
        if self.attempts and any(
            attempt.identity.provider != self.attempts[0].identity.provider
            or attempt.identity.model != self.attempts[0].identity.model
            for attempt in self.attempts
        ):
            raise ValueError
        if len(self.attempts) == 2 and (
            type(self.attempts[0]) is not SettledAttempt
            or self.attempts[0].cost_record.completion_state != "error"
        ):
            raise ValueError
        return self


def _source_free_python_dump_inner(value: RequestAccountingModel) -> dict[str, Any]:
    model_type = type(value)
    fields = tuple(model_type.model_fields)
    raw = _exact_model_state(value, model_type, fields)
    _preflight_model_fields(model_type, raw, "python")
    copied: RequestAccountingModel
    if model_type is AttemptIdentity:
        copied = _copy_identity(value)
    elif model_type is ProviderAttemptPolicy:
        copied = _copy_policy(value)
    elif model_type in {SettledAttempt, UncertainAttempt}:
        copied = _copy_attempt_record(value)
    elif model_type is RequestAccountingSummary:
        attempts = raw["attempts"]
        if type(attempts) is not tuple or len(attempts) > 2:
            raise RequestAccountingError from None
        copied = RequestAccountingSummary(
            contract_version=raw["contract_version"],
            provider_work_started=raw["provider_work_started"],
            request_completion=raw["request_completion"],
            attempts=tuple(_copy_attempt_record(item) for item in tuple.__iter__(attempts)),
        )
    else:
        raise RequestAccountingError from None
    safe = _exact_model_state(copied, model_type, fields)
    return {name: safe[name] for name in fields}


def _source_free_python_dump(value: RequestAccountingModel) -> dict[str, Any]:
    result: object = _MISSING
    try:
        result = _source_free_python_dump_inner(value)
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        raise
    except BaseException:
        pass
    if result is _MISSING:
        del value
        raise RequestAccountingError from None
    return cast(dict[str, Any], result)


class ProviderAttemptObserver(Protocol):
    def attempt_started(self, identity: AttemptIdentity) -> None: ...
    def usage_observed(self, usage: ProviderUsageChunk) -> None: ...
    def attempt_finished(
        self,
        identity: AttemptIdentity,
        completion: Literal["completed", "error", "cancelled"],
    ) -> None: ...
    def attempt_uncertain(self, identity: AttemptIdentity, reason: UncertaintyReason) -> None: ...


@dataclass
class _AttemptState:
    identity: AttemptIdentity
    service_tier: str | None = None
    usage: ProviderUsageChunk | None = None
    completion: Literal["completed", "error", "cancelled"] | None = None
    uncertainty: UncertaintyReason | None = None


def _copy_attempt_record(
    value: object, *, allow_dict: bool = False
) -> SettledAttempt | UncertainAttempt:
    if allow_dict and type(value) is dict:
        if len(value) not in {4, 6}:
            raise RequestAccountingError from None
        items = tuple(dict.items(value))
        if any(type(key) is not str for key, _ in items):
            raise RequestAccountingError from None
        controlled = {key: item for key, item in items}
        kind = dict.get(controlled, "kind", _MISSING)
        if type(kind) is not str:
            raise RequestAccountingError from None
        if kind == "settled":
            expected = {"kind", "identity", "service_tier", "cost_record"}
            if set(controlled) != expected:
                raise RequestAccountingError from None
            return SettledAttempt(
                identity=_copy_identity(dict.__getitem__(controlled, "identity"), allow_dict=True),
                service_tier=dict.__getitem__(controlled, "service_tier"),
                cost_record=_copy_cost_record(
                    dict.__getitem__(controlled, "cost_record"), allow_dict=True
                ),
            )
        if kind == "uncertain":
            expected = {
                "kind",
                "identity",
                "service_tier",
                "completion",
                "last_usage",
                "reason",
            }
            if set(controlled) != expected:
                raise RequestAccountingError from None
            return UncertainAttempt(
                identity=_copy_identity(dict.__getitem__(controlled, "identity"), allow_dict=True),
                service_tier=dict.__getitem__(controlled, "service_tier"),
                last_usage=(
                    None
                    if dict.__getitem__(controlled, "last_usage") is None
                    else _copy_usage(dict.__getitem__(controlled, "last_usage"), allow_dict=True)
                ),
                reason=cast(UncertaintyReason, dict.__getitem__(controlled, "reason")),
            )
        raise RequestAccountingError from None
    if type(value) is SettledAttempt:
        fields = tuple(SettledAttempt.model_fields)
        raw = _exact_model_state(value, SettledAttempt, fields)
        if type(raw["kind"]) is not str or raw["kind"] != "settled":
            raise RequestAccountingError from None
        tier = raw["service_tier"]
        if tier is not None and type(tier) is not str:
            raise RequestAccountingError from None
        return SettledAttempt(
            identity=_copy_identity(raw["identity"]),
            service_tier=tier,
            cost_record=_copy_cost_record(raw["cost_record"]),
        )
    if type(value) is UncertainAttempt:
        fields = tuple(UncertainAttempt.model_fields)
        raw = _exact_model_state(value, UncertainAttempt, fields)
        if (
            type(raw["kind"]) is not str
            or raw["kind"] != "uncertain"
            or type(raw["completion"]) is not str
            or raw["completion"] != "uncertain"
            or type(raw["reason"]) is not str
            or raw["reason"] not in _UNCERTAINTY_REASONS
        ):
            raise RequestAccountingError from None
        tier = raw["service_tier"]
        if tier is not None and type(tier) is not str:
            raise RequestAccountingError from None
        usage = None if raw["last_usage"] is None else _copy_usage(raw["last_usage"])
        return UncertainAttempt(
            identity=_copy_identity(raw["identity"]),
            service_tier=tier,
            last_usage=usage,
            reason=cast(UncertaintyReason, raw["reason"]),
        )
    raise RequestAccountingError from None


class RequestAccountingSession:
    """Mutable one-request state machine; only ``finalize`` emits a value."""

    __slots__ = (
        "_attempt_date",
        "_snapshots",
        "_currency",
        "_max_attempts",
        "_provider",
        "_model",
        "_attempts",
        "_summary",
        "_final_completion",
    )

    def __init__(
        self,
        *,
        attempt_date: date,
        price_snapshots: tuple[ProviderPriceSnapshot, ...] = (),
        currency: str | None = None,
        max_attempts: int | None = None,
        policy: ProviderAttemptPolicy | None = None,
    ) -> None:
        prepared: object = _MISSING
        failed = False
        try:
            if type(attempt_date) is not date:
                raise RequestAccountingError
            if (
                type(price_snapshots) is not tuple
                or len(price_snapshots) > MAX_REQUEST_PRICE_SNAPSHOTS
            ):
                raise RequestAccountingError
            copied_snapshots = tuple(
                _copy_snapshot(item) for item in tuple.__iter__(price_snapshots)
            )
            if currency is not None and (
                type(currency) is not str or _CURRENCY_PATTERN.fullmatch(currency) is None
            ):
                raise RequestAccountingError
            copied_policy = None if policy is None else _copy_policy(policy)
            if copied_policy is None:
                if type(max_attempts) is not int or not 0 <= max_attempts <= 2:
                    raise RequestAccountingError
                provider: str | None = None
                model: str | None = None
                maximum = max_attempts
            else:
                if max_attempts is not None and max_attempts != copied_policy.max_attempts:
                    raise RequestAccountingError
                provider = copied_policy.provider
                model = copied_policy.model
                maximum = copied_policy.max_attempts
            prepared = (
                attempt_date,
                copied_snapshots,
                currency,
                maximum,
                provider,
                model,
            )
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            failed = True
        if failed or prepared is _MISSING:
            del attempt_date, price_snapshots, currency, max_attempts, policy
            del prepared
            raise RequestAccountingError from None
        (
            copied_date,
            copied_snapshots,
            copied_currency,
            maximum,
            provider,
            model,
        ) = cast(
            tuple[
                date,
                tuple[ProviderPriceSnapshot, ...],
                str | None,
                int,
                str | None,
                str | None,
            ],
            prepared,
        )
        self._attempt_date = copied_date
        self._snapshots = copied_snapshots
        self._currency = copied_currency
        self._max_attempts = maximum
        self._provider = provider
        self._model = model
        self._attempts: list[_AttemptState] = []
        self._summary: RequestAccountingSummary | None = None
        self._final_completion: RequestCompletion | None = None

    @property
    def attempt_date(self) -> date:
        return self._attempt_date

    @property
    def observer(self) -> ProviderAttemptObserver:
        return self

    def _ensure_open(self) -> None:
        if self._summary is not None:
            raise RequestAccountingError from None

    def _active(self, identity: object) -> _AttemptState:
        copied = _copy_identity(identity)
        if not self._attempts:
            raise RequestAccountingError from None
        state = self._attempts[-1]
        current = state.identity
        if (
            copied.provider != current.provider
            or copied.model != current.model
            or copied.provider_attempt != current.provider_attempt
        ):
            if state.completion is None:
                state.uncertainty = "identity_mismatch"
            raise RequestAccountingError from None
        if state.completion is not None or state.uncertainty is not None:
            raise RequestAccountingError from None
        return state

    def attempt_started(self, identity: AttemptIdentity) -> None:
        failed = False
        try:
            self._attempt_started(identity)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            failed = True
        del identity
        if failed:
            raise RequestAccountingError from None

    def _attempt_started(self, identity: AttemptIdentity) -> None:
        self._ensure_open()
        copied = _copy_identity(identity)
        expected = len(self._attempts) + 1
        if (
            len(self._attempts) >= self._max_attempts
            or copied.provider_attempt != expected
            or (
                self._attempts
                and self._attempts[-1].completion is None
                and self._attempts[-1].uncertainty is None
            )
            or (self._provider is not None and copied.provider != self._provider)
            or (self._model is not None and copied.model != self._model)
            or (copied.provider_attempt == 2 and self._attempts[-1].completion != "error")
        ):
            raise RequestAccountingError from None
        if not self._attempts and self._provider is None:
            self._provider = copied.provider
            self._model = copied.model
        self._attempts.append(_AttemptState(identity=copied))

    def usage_observed(self, usage: ProviderUsageChunk) -> None:
        failed = False
        try:
            self._usage_observed(usage)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            failed = True
        del usage
        if failed:
            raise RequestAccountingError from None

    def _usage_observed(self, usage: ProviderUsageChunk) -> None:
        self._ensure_open()
        try:
            copied = _copy_usage_chunk(usage)
        except RequestAccountingError:
            if self._attempts and self._attempts[-1].completion is None:
                self._attempts[-1].uncertainty = "usage_invalid"
            raise
        if not self._attempts:
            raise RequestAccountingError from None
        identity = AttemptIdentity(
            provider=copied.provider,
            model=copied.model,
            provider_attempt=copied.provider_attempt,
        )
        state = self._active(identity)
        if (
            state.service_tier is not None
            and copied.service_tier is not None
            and state.service_tier != copied.service_tier
        ):
            state.uncertainty = "identity_mismatch"
            raise RequestAccountingError from None
        try:
            if state.usage is not None and all(
                object.__getattribute__(state.usage, name) == object.__getattribute__(copied, name)
                for name in ProviderUsageChunk.model_fields
            ):
                state.uncertainty = "usage_invalid"
                raise RequestAccountingError from None
            state.usage = merge_cumulative_usage(state.usage, copied)
        except ProviderUsageValidationError:
            state.uncertainty = "usage_invalid"
            raise RequestAccountingError from None
        if state.service_tier is None:
            state.service_tier = copied.service_tier

    def attempt_finished(
        self,
        identity: AttemptIdentity,
        completion: Literal["completed", "error", "cancelled"],
    ) -> None:
        failed = False
        try:
            self._attempt_finished(identity, completion)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            failed = True
        del identity, completion
        if failed:
            raise RequestAccountingError from None

    def _attempt_finished(
        self,
        identity: AttemptIdentity,
        completion: Literal["completed", "error", "cancelled"],
    ) -> None:
        self._ensure_open()
        if type(completion) is not str or completion not in _ATTEMPT_COMPLETIONS:
            if self._attempts and self._attempts[-1].completion is None:
                self._attempts[-1].uncertainty = "observer_failure"
            raise RequestAccountingError from None
        try:
            state = self._active(identity)
        except RequestAccountingError:
            if (
                self._attempts
                and self._attempts[-1].completion is None
                and self._attempts[-1].uncertainty is None
            ):
                self._attempts[-1].uncertainty = "observer_failure"
            raise
        state.completion = completion

    def attempt_uncertain(self, identity: AttemptIdentity, reason: UncertaintyReason) -> None:
        failed = False
        try:
            self._attempt_uncertain(identity, reason)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            failed = True
        del identity, reason
        if failed:
            raise RequestAccountingError from None

    def _attempt_uncertain(self, identity: AttemptIdentity, reason: UncertaintyReason) -> None:
        self._ensure_open()
        if type(reason) is not str or reason not in _UNCERTAINTY_REASONS:
            if self._attempts and self._attempts[-1].completion is None:
                self._attempts[-1].uncertainty = "observer_failure"
            raise RequestAccountingError from None
        try:
            state = self._active(identity)
        except RequestAccountingError:
            if (
                self._attempts
                and self._attempts[-1].completion is None
                and self._attempts[-1].uncertainty is None
            ):
                self._attempts[-1].uncertainty = "observer_failure"
            raise
        state.uncertainty = reason

    def active_cleanup_uncertain(self) -> None:
        """Conservatively mark an unfinished attempt after outer cleanup failure."""

        self._ensure_open()
        if self._attempts:
            state = self._attempts[-1]
            if state.completion is None:
                state.uncertainty = "cleanup_uncertain"

    def _snapshot_for(self, state: _AttemptState) -> ProviderPriceSnapshot | None:
        matches = tuple(
            snapshot
            for snapshot in self._snapshots
            if snapshot.provider == state.identity.provider
            and snapshot.model == state.identity.model
            and snapshot.service_tier == state.service_tier
            and (self._currency is None or snapshot.currency == self._currency)
            and self._attempt_date >= snapshot.effective_from
            and (
                snapshot.effective_through is None
                or self._attempt_date <= snapshot.effective_through
            )
        )
        if len(matches) > 1:
            state.uncertainty = "observer_failure"
            return None
        return None if not matches else matches[0]

    def finalize(self, request_completion: RequestCompletion) -> RequestAccountingSummary:
        if type(request_completion) is not str or request_completion not in _REQUEST_COMPLETIONS:
            raise RequestAccountingError from None
        if self._summary is not None:
            if request_completion == self._final_completion:
                return self._summary
            raise RequestAccountingError from None

        records: list[SettledAttempt | UncertainAttempt] = []
        try:
            for state in self._attempts:
                if state.completion is None and state.uncertainty is None:
                    state.uncertainty = "finish_missing"
                if state.uncertainty is not None:
                    records.append(
                        UncertainAttempt(
                            identity=state.identity,
                            service_tier=state.service_tier,
                            last_usage=None if state.usage is None else state.usage.usage,
                            reason=state.uncertainty,
                        )
                    )
                    continue
                assert state.completion is not None
                snapshot = self._snapshot_for(state)
                if state.uncertainty is not None:
                    records.append(
                        UncertainAttempt(
                            identity=state.identity,
                            service_tier=state.service_tier,
                            last_usage=None if state.usage is None else state.usage.usage,
                            reason=state.uncertainty,
                        )
                    )
                    continue
                answer_outcome: Literal["grounded", "refused", "unverified"] = (
                    "grounded"
                    if request_completion == "completed" and state.completion == "completed"
                    else "refused"
                    if request_completion == "refused" and state.completion == "completed"
                    else "unverified"
                )
                accounting_input = ProviderAttemptAccountingInput(
                    provider=state.identity.provider,
                    model=state.identity.model,
                    service_tier=state.service_tier,
                    provider_attempt=state.identity.provider_attempt,
                    attempt_date=self._attempt_date,
                    completion_state=state.completion,
                    answer_outcome=answer_outcome,
                    usage=None if state.usage is None else _copy_usage(state.usage.usage),
                    price_snapshot=snapshot,
                )
                cost_record = price_provider_attempt(accounting_input)
                records.append(
                    SettledAttempt(
                        identity=state.identity,
                        service_tier=state.service_tier,
                        cost_record=cost_record,
                    )
                )
            summary = RequestAccountingSummary(
                provider_work_started=bool(records),
                request_completion=request_completion,
                attempts=tuple(records),
            )
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            raise RequestAccountingError from None
        self._summary = summary
        self._final_completion = request_completion
        return summary


def _utc_date() -> date:
    return datetime.now(UTC).date()


class RequestAccountingSessionFactory:
    __slots__ = ("_policy", "_snapshots", "_currency", "_date_source")

    def __init__(
        self,
        *,
        policy: ProviderAttemptPolicy,
        price_snapshots: tuple[ProviderPriceSnapshot, ...] = (),
        currency: str | None = None,
        date_source: Callable[[], date] = _utc_date,
    ) -> None:
        prepared: object = _MISSING
        failed = False
        try:
            copied_policy = _copy_policy(policy)
            if (
                type(price_snapshots) is not tuple
                or len(price_snapshots) > MAX_REQUEST_PRICE_SNAPSHOTS
            ):
                raise RequestAccountingError
            snapshots = tuple(_copy_snapshot(item) for item in tuple.__iter__(price_snapshots))
            if currency is not None and (
                type(currency) is not str or _CURRENCY_PATTERN.fullmatch(currency) is None
            ):
                raise RequestAccountingError
            if type(date_source) not in {
                FunctionType,
                BuiltinFunctionType,
            } or inspect.iscoroutinefunction(date_source):
                raise RequestAccountingError
            prepared = (copied_policy, snapshots, currency, date_source)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            failed = True
        if failed or prepared is _MISSING:
            del policy, price_snapshots, currency, date_source, prepared
            raise RequestAccountingError from None
        copied_policy, snapshots, copied_currency, copied_date_source = cast(
            tuple[
                ProviderAttemptPolicy,
                tuple[ProviderPriceSnapshot, ...],
                str | None,
                Callable[[], date],
            ],
            prepared,
        )
        object.__setattr__(self, "_policy", copied_policy)
        object.__setattr__(self, "_snapshots", snapshots)
        object.__setattr__(self, "_currency", copied_currency)
        object.__setattr__(self, "_date_source", copied_date_source)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise RequestAccountingError from None

    def _validated_state(
        self,
    ) -> tuple[
        ProviderAttemptPolicy,
        tuple[ProviderPriceSnapshot, ...],
        str | None,
        Callable[[], date],
    ]:
        if type(self) is not RequestAccountingSessionFactory:
            raise RequestAccountingError from None
        policy = _copy_policy(object.__getattribute__(self, "_policy"))
        raw_snapshots = object.__getattribute__(self, "_snapshots")
        if type(raw_snapshots) is not tuple or len(raw_snapshots) > MAX_REQUEST_PRICE_SNAPSHOTS:
            raise RequestAccountingError from None
        snapshots = tuple(_copy_snapshot(item) for item in tuple.__iter__(raw_snapshots))
        currency = object.__getattribute__(self, "_currency")
        if currency is not None and (
            type(currency) is not str or _CURRENCY_PATTERN.fullmatch(currency) is None
        ):
            raise RequestAccountingError from None
        date_source = object.__getattribute__(self, "_date_source")
        if type(date_source) not in {
            FunctionType,
            BuiltinFunctionType,
        } or inspect.iscoroutinefunction(date_source):
            raise RequestAccountingError from None
        return policy, snapshots, currency, date_source

    @property
    def policy(self) -> ProviderAttemptPolicy:
        result: object = _MISSING
        try:
            result = self._validated_state()[0]
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            pass
        if result is _MISSING:
            del self
            raise RequestAccountingError from None
        return cast(ProviderAttemptPolicy, result)

    def matches_policy(self, policy: ProviderAttemptPolicy) -> bool:
        result: object = _MISSING
        try:
            other = _copy_policy(policy)
            captured = self._validated_state()[0]
            result = (
                captured.provider == other.provider
                and captured.model == other.model
                and captured.max_attempts == other.max_attempts
            )
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            pass
        if result is _MISSING:
            del self, policy
            raise RequestAccountingError from None
        return cast(bool, result)

    def create(self, *, attempt_date: date | None = None) -> RequestAccountingSession:
        result: object = _MISSING
        policy: object = _MISSING
        snapshots: object = _MISSING
        currency: object = _MISSING
        date_source: object = _MISSING
        selected_date: object = _MISSING
        try:
            policy, snapshots, currency, date_source = self._validated_state()
            selected_date = (
                cast(Callable[[], object], date_source)()
                if attempt_date is None
                else cast(object, attempt_date)
            )
            if type(selected_date) is CoroutineType:
                selected_date.close()
                selected_date = _MISSING
                raise RequestAccountingError
            if type(selected_date) is not date:
                raise RequestAccountingError
            result = RequestAccountingSession(
                attempt_date=selected_date,
                price_snapshots=snapshots,
                currency=currency,
                policy=policy,
            )
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            pass
        if result is _MISSING:
            del self, attempt_date, policy, snapshots, currency, date_source, selected_date
            raise RequestAccountingError from None
        return cast(RequestAccountingSession, result)


class ControlledProviderAccountingBinding:
    """Private proof that one app-created built-in matches captured settings."""

    __slots__ = ("provider", "policy", "_provider_state", "_brand")
    provider: object
    policy: ProviderAttemptPolicy
    _provider_state: tuple[object, ...]
    _brand: object

    def __init__(
        self,
        provider: object,
        policy: ProviderAttemptPolicy,
        *,
        provider_state: tuple[object, ...] = (),
        authority: object = None,
    ) -> None:
        if authority is not _APPLICATION_BINDING_AUTHORITY:
            raise RequestAccountingError from None
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "policy", _copy_policy(policy))
        object.__setattr__(self, "_provider_state", provider_state)
        object.__setattr__(self, "_brand", _APPLICATION_BINDING_AUTHORITY)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise RequestAccountingError from None


class _ControlledProviderAttemptObserver:
    """Revalidate app authority in the provider's immediate pre-I/O window."""

    __slots__ = ("_settings", "_provider", "_binding", "_session")

    def __init__(
        self,
        settings: Any,
        provider: object,
        binding: ControlledProviderAccountingBinding,
        session: RequestAccountingSession,
    ) -> None:
        object.__setattr__(self, "_settings", _accounting_settings_snapshot(settings))
        object.__setattr__(self, "_provider", provider)
        object.__setattr__(self, "_binding", binding)
        object.__setattr__(self, "_session", session)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise RequestAccountingError from None

    def attempt_started(self, identity: AttemptIdentity) -> None:
        validate_controlled_provider_binding(
            object.__getattribute__(self, "_settings"),
            object.__getattribute__(self, "_provider"),
            object.__getattribute__(self, "_binding"),
        )
        object.__getattribute__(self, "_session").attempt_started(identity)

    def usage_observed(self, usage: ProviderUsageChunk) -> None:
        object.__getattribute__(self, "_session").usage_observed(usage)

    def attempt_finished(
        self,
        identity: AttemptIdentity,
        completion: Literal["completed", "error", "cancelled"],
    ) -> None:
        object.__getattribute__(self, "_session").attempt_finished(identity, completion)

    def attempt_uncertain(self, identity: AttemptIdentity, reason: UncertaintyReason) -> None:
        object.__getattribute__(self, "_session").attempt_uncertain(identity, reason)


def controlled_provider_observer(
    settings: Any,
    provider: object,
    binding: ControlledProviderAccountingBinding,
    session: RequestAccountingSession,
) -> ProviderAttemptObserver:
    validate_controlled_provider_binding(settings, provider, binding)
    return _ControlledProviderAttemptObserver(settings, provider, binding, session)


def observer_matches_session(
    observer: ProviderAttemptObserver,
    session: RequestAccountingSession,
) -> bool:
    return type(observer) is _ControlledProviderAttemptObserver and (
        object.__getattribute__(observer, "_session") is session
    )


def _accounting_settings_snapshot(settings: Any) -> Any:
    from app.config import Settings, validated_settings_snapshot

    if type(settings) is not Settings:
        raise RequestAccountingError from None
    try:
        extra = object.__getattribute__(settings, "__pydantic_extra__")
        private = object.__getattribute__(settings, "__pydantic_private__")
        fields_set = object.__getattribute__(settings, "__pydantic_fields_set__")
    except BaseException:
        raise RequestAccountingError from None
    if extra is not None or private is not None or type(fields_set) is not set:
        raise RequestAccountingError from None
    if len(fields_set) > len(Settings.model_fields):
        raise RequestAccountingError from None
    if any(type(item) is not str for item in set.__iter__(fields_set)):
        raise RequestAccountingError from None
    if not fields_set.issubset(Settings.model_fields):
        raise RequestAccountingError from None
    return cast(
        Settings,
        _content_free(lambda: validated_settings_snapshot(settings)),
    )


def _provider_attempt_policy(settings: Any) -> ProviderAttemptPolicy:
    snapshot = _accounting_settings_snapshot(settings)
    provider = snapshot.provider
    if provider == "echo":
        return ProviderAttemptPolicy(provider="echo", model="", max_attempts=0)
    if provider == "ollama":
        return ProviderAttemptPolicy(provider="ollama", model=snapshot.ollama_model, max_attempts=1)
    if provider == "gemini":
        return ProviderAttemptPolicy(
            provider="gemini",
            model=snapshot.gemini_model,
            max_attempts=snapshot.gemini_max_retries + 1,
        )
    raise RequestAccountingError from None


def provider_attempt_policy(settings: Any) -> ProviderAttemptPolicy:
    """Derive the sole provider attempt policy from a validated settings snapshot."""

    result: object = _MISSING
    try:
        result = _provider_attempt_policy(settings)
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        raise
    except BaseException:
        pass
    if result is _MISSING:
        del settings
        raise RequestAccountingError from None
    return cast(ProviderAttemptPolicy, result)


def _bind_application_owned_provider(
    settings: Any,
    provider: object,
    *,
    authority: object,
) -> ControlledProviderAccountingBinding:
    """Issue controlled authority only for an exact app-created built-in state."""

    if authority is not _APPLICATION_BINDING_AUTHORITY:
        raise RequestAccountingError from None
    snapshot = _accounting_settings_snapshot(settings)
    policy = provider_attempt_policy(snapshot)
    try:
        raw = object.__getattribute__(provider, "__dict__")
    except BaseException:
        raise RequestAccountingError from None
    if type(raw) is not dict:
        raise RequestAccountingError from None
    if len(raw) > 9:
        raise RequestAccountingError from None
    items = tuple(dict.items(raw))
    if any(type(key) is not str for key, _ in items):
        raise RequestAccountingError from None
    state = {key: value for key, value in items}

    # Imports are local so provider base classes can import only the observer protocol.
    from app.providers.echo import EchoProvider
    from app.providers.gemini import GeminiProvider
    from app.providers.ollama import OllamaProvider

    valid = False
    provider_state: tuple[object, ...]
    if policy.provider == "echo":
        valid = (
            type(provider) is EchoProvider
            and set(state) == {"_delay_seconds"}
            and type(state.get("_delay_seconds")) is float
            and state.get("_delay_seconds") == 0.0
        )
    elif policy.provider == "ollama":
        valid = (
            type(provider) is OllamaProvider
            and set(state) == {"_base_url", "_model", "_client"}
            and type(state.get("_base_url")) is str
            and state.get("_base_url") == snapshot.ollama_base_url.rstrip("/")
            and type(state.get("_model")) is str
            and state.get("_model") == snapshot.ollama_model
        )
    elif policy.provider == "gemini":
        expected_keys = {
            "_client",
            "_root_client",
            "_types",
            "_model",
            "_timeout_seconds",
            "_timeout_milliseconds",
            "_max_retries",
            "_sleep",
            "_closed",
        }
        timeout = state.get("_timeout_seconds")
        retries = state.get("_max_retries")
        valid = (
            type(provider) is GeminiProvider
            and set(state) == expected_keys
            and type(state.get("_model")) is str
            and state.get("_model") == snapshot.gemini_model
            and type(timeout) is float
            and timeout == snapshot.gemini_timeout_seconds
            and type(state.get("_timeout_milliseconds")) is int
            and state.get("_timeout_milliseconds") == math.ceil(timeout * 1000)
            and type(retries) is int
            and retries == snapshot.gemini_max_retries
            and state.get("_sleep") is asyncio.sleep
            and state.get("_closed") is False
        )
    if not valid:
        raise RequestAccountingError from None
    if policy.provider == "echo":
        provider_state = ("echo", state["_delay_seconds"])
    elif policy.provider == "ollama":
        provider_state = (
            "ollama",
            state["_base_url"],
            state["_model"],
            state["_client"],
        )
    else:
        provider_state = (
            "gemini",
            state["_client"],
            state["_root_client"],
            state["_types"],
            state["_model"],
            state["_timeout_seconds"],
            state["_timeout_milliseconds"],
            state["_max_retries"],
            state["_sleep"],
            state["_closed"],
        )
    return ControlledProviderAccountingBinding(
        provider=provider,
        policy=policy,
        provider_state=provider_state,
        authority=_APPLICATION_BINDING_AUTHORITY,
    )


def bind_application_owned_provider(
    settings: Any,
    provider: object,
    *,
    authority: object,
) -> ControlledProviderAccountingBinding:
    result: object = _MISSING
    try:
        result = _bind_application_owned_provider(settings, provider, authority=authority)
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        raise
    except BaseException:
        pass
    if result is _MISSING:
        del settings, provider, authority
        raise RequestAccountingError from None
    return cast(ControlledProviderAccountingBinding, result)


def _same_provider_state(left: tuple[object, ...], right: tuple[object, ...]) -> bool:
    if type(left) is not tuple or type(right) is not tuple or len(left) != len(right):
        return False
    if not left or type(left[0]) is not str or type(right[0]) is not str:
        return False
    if left[0] != right[0]:
        return False
    if left[0] == "echo":
        return len(left) == 2 and type(left[1]) is float and left[1] == right[1]
    if left[0] == "ollama":
        return (
            len(left) == 4
            and all(type(item) is str for item in left[1:3])
            and left[1] == right[1]
            and left[2] == right[2]
            and left[3] is right[3]
        )
    if left[0] == "gemini":
        return (
            len(left) == 10
            and left[1] is right[1]
            and left[2] is right[2]
            and left[3] is right[3]
            and type(left[4]) is str
            and left[4] == right[4]
            and type(left[5]) is float
            and left[5] == right[5]
            and type(left[6]) is int
            and left[6] == right[6]
            and type(left[7]) is int
            and left[7] == right[7]
            and left[8] is right[8]
            and type(left[9]) is bool
            and left[9] is right[9]
        )
    return False


def _validate_controlled_provider_binding(
    settings: Any,
    provider: object,
    binding: ControlledProviderAccountingBinding,
) -> ProviderAttemptPolicy:
    if type(binding) is not ControlledProviderAccountingBinding:
        raise RequestAccountingError from None
    if object.__getattribute__(binding, "_brand") is not _APPLICATION_BINDING_AUTHORITY:
        raise RequestAccountingError from None
    bound_provider = object.__getattribute__(binding, "provider")
    bound_policy = _copy_policy(object.__getattribute__(binding, "policy"))
    bound_state = object.__getattribute__(binding, "_provider_state")
    if bound_provider is not provider:
        raise RequestAccountingError from None
    candidate = bind_application_owned_provider(
        settings,
        provider,
        authority=_APPLICATION_BINDING_AUTHORITY,
    )
    if (
        bound_policy.provider != candidate.policy.provider
        or bound_policy.model != candidate.policy.model
        or bound_policy.max_attempts != candidate.policy.max_attempts
        or not _same_provider_state(
            bound_state,
            object.__getattribute__(candidate, "_provider_state"),
        )
    ):
        raise RequestAccountingError from None
    return candidate.policy


def validate_controlled_provider_binding(
    settings: Any,
    provider: object,
    binding: ControlledProviderAccountingBinding,
) -> ProviderAttemptPolicy:
    result: object = _MISSING
    try:
        result = _validate_controlled_provider_binding(settings, provider, binding)
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        raise
    except BaseException:
        pass
    if result is _MISSING:
        del settings, provider, binding
        raise RequestAccountingError from None
    return cast(ProviderAttemptPolicy, result)


async def discard_accounting_summary(summary: RequestAccountingSummary) -> None:
    """Explicit default lexical owner for the pre-enforcement milestone."""

    del summary
