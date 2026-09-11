"""Strict, bounded, in-process telemetry derived from authoritative state."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Mapping
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal, Protocol, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetCoreSchemaHandler,
    StrictBool,
    StrictInt,
    WrapSerializer,
    WrapValidator,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic._internal._model_construction import ModelMetaclass
from pydantic_core import CoreSchema, core_schema

from app.logging_config import (
    TelemetryInputRejectedLog,
    TelemetrySinkFailedLog,
    emit_app_log,
)
from app.providers.accounting import ProviderAttemptCostRecord
from app.providers.contracts import (
    MAX_SAFE_TOKEN_COUNT,
    ProviderUsage,
    validate_model_identifier,
)
from app.readiness import ReadinessCheck, ReadinessReport
from app.request_accounting import (
    ControlledProviderAccountingBinding,
    RequestAccountingSummary,
    SettledAttempt,
    UncertainAttempt,
    validate_controlled_provider_binding,
)

TELEMETRY_CONTRACT_VERSION: Literal["1.0"] = "1.0"
MAX_TELEMETRY_DURATION_NS = 86_400_000_000_000
_MAX_DURATION_SECONDS = Decimal(MAX_TELEMETRY_DURATION_NS) / Decimal(1_000_000_000)
_PROJECTOR_AUTHORITY = object()
_FAILED = object()

ChatOutcome = Literal["grounded", "refused", "limit", "error", "cancelled"]
TokenKind = Literal["input", "cached_input", "output", "thinking", "total"]
UsageState = Literal["complete", "incomplete", "missing", "uncertain"]

_CHAT_OUTCOMES = {"grounded", "refused", "limit", "error", "cancelled"}
_TOKEN_KINDS = {"input", "cached_input", "output", "thinking", "total"}
_USAGE_STATES = {"complete", "incomplete", "missing", "uncertain"}
_READINESS_DIMENSIONS = {
    "database",
    "vector_store",
    "corpus",
    "provider",
    "model",
    "embedding",
    "exact_corpus",
    "budget",
}
_READINESS_STATES = {"ready", "not_ready", "not_required", "unknown"}
_READINESS_REASONS = {
    "ready",
    "not_required",
    "unreachable",
    "unavailable",
    "misconfigured",
    "model_missing",
    "store_unready",
    "exact_corpus_unready",
    "budget_exhausted",
    "accounting_uncertain",
    "budget_overrun",
}
_DURATION_JSON_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]{0,4})(?:\.[0-9]{1,9})?(?:[Ee][+-]?[0-9]{1,2})?\Z"
)


class TelemetryError(Exception):
    """Fixed failure that never renders rejected telemetry input."""

    def __init__(self) -> None:
        super().__init__("Telemetry input is invalid.")

    def __repr__(self) -> str:
        return "TelemetryError()"

    def __getattribute__(self, name: str) -> Any:
        if name in {"__traceback__", "__cause__", "__context__"}:
            return None
        return super().__getattribute__(name)

    def errors(self, **kwargs: Any) -> list[dict[str, object]]:
        del kwargs
        return [{"type": "telemetry_error", "loc": (), "msg": "Telemetry input is invalid."}]

    def json(self, **kwargs: Any) -> str:
        del kwargs
        return '[{"type":"telemetry_error","loc":[],"msg":"Telemetry input is invalid."}]'


def _release_exception(error: BaseException) -> None:
    try:
        BaseException.__setattr__(error, "__traceback__", None)
        BaseException.__setattr__(error, "__context__", None)
        BaseException.__setattr__(error, "__cause__", None)
    except BaseException:
        return


def _dict_items(value: object, *, maximum: int) -> tuple[tuple[str, object], ...]:
    if type(value) is not dict or len(value) > maximum:
        raise TelemetryError from None
    items = tuple(dict.items(cast(dict[object, object], value)))
    if any(type(key) is not str for key, _ in items):
        raise TelemetryError from None
    return cast(tuple[tuple[str, object], ...], items)


def _raw_model(value: object, model: type[BaseModel]) -> dict[str, object]:
    if type(value) is not model:
        raise TelemetryError from None
    try:
        raw = object.__getattribute__(value, "__dict__")
        extra = object.__getattribute__(value, "__pydantic_extra__")
        private = object.__getattribute__(value, "__pydantic_private__")
        fields_set = object.__getattribute__(value, "__pydantic_fields_set__")
    except BaseException as error:
        _release_exception(error)
        raise TelemetryError from None
    fields = model.model_fields
    if extra is not None or private is not None or type(fields_set) is not set:
        raise TelemetryError from None
    if len(fields_set) > len(fields) or any(
        type(item) is not str for item in set.__iter__(fields_set)
    ):
        raise TelemetryError from None
    if not fields_set.issubset(fields):
        raise TelemetryError from None
    items = _dict_items(raw, maximum=len(fields))
    if len(items) != len(fields) or {key for key, _ in items} != set(fields):
        raise TelemetryError from None
    return {key: item for key, item in items}


def _event_input(model: type[TelemetryModel], value: object, mode: str) -> dict[str, object]:
    fields = model.model_fields
    if type(value) is model:
        controlled = _raw_model(value, model)
    else:
        items = _dict_items(value, maximum=len(fields))
        if any(key not in fields for key, _ in items):
            raise TelemetryError from None
        controlled = {key: item for key, item in items}
    _preflight_event_fields(model, controlled, mode)
    return controlled


def _preflight_event_fields(
    model: type[TelemetryModel], values: dict[str, object], mode: str
) -> None:
    for name, value in dict.items(values):
        if (
            name
            in {
                "contract_version",
                "kind",
                "metric",
                "chat_outcome",
                "provider",
                "model",
                "token_kind",
                "usage_state",
                "readiness_dimension",
                "readiness_state",
                "readiness_reason",
            }
            and type(value) is not str
        ):
            raise TelemetryError from None
        if name in {"delta", "provider_attempt"} and type(value) is not int:
            raise TelemetryError from None
        if name == "required" and type(value) is not bool:
            raise TelemetryError from None
        if name == "value":
            if model is ProviderReportedTotalTokens:
                if type(value) is not int:
                    raise TelemetryError from None
            elif type(value) is not Decimal and not (mode == "json" and type(value) is str):
                raise TelemetryError from None
            elif mode == "json" and type(value) is str and (
                len(value) > 32 or _DURATION_JSON_PATTERN.fullmatch(value) is None
            ):
                raise TelemetryError from None
    contract_version = dict.get(values, "contract_version", "1.0")
    if contract_version != "1.0":
        raise TelemetryError from None
    metric = dict.get(values, "metric", _METRIC_BY_TYPE[model])
    if metric != _METRIC_BY_TYPE[model]:
        raise TelemetryError from None
    expected_kind = (
        "histogram"
        if model in {ChatDurationSeconds, ProviderReportedTotalTokens, ReadinessDurationSeconds}
        else "counter"
    )
    if dict.get(values, "kind", expected_kind) != expected_kind:
        raise TelemetryError from None
    if "delta" in values:
        delta = values["delta"]
        if type(delta) is not int or not 1 <= delta <= MAX_SAFE_TOKEN_COUNT:
            raise TelemetryError from None
        if model is not ProviderUsageTokensTotal and delta != 1:
            raise TelemetryError from None
    if "provider" in values and values["provider"] not in {"ollama", "gemini"}:
        raise TelemetryError from None
    if "provider_attempt" in values and values["provider_attempt"] not in {1, 2}:
        raise TelemetryError from None
    if "model" in values:
        try:
            validate_model_identifier(cast(str, values["model"]))
        except Exception:
            raise TelemetryError from None
    if "chat_outcome" in values and values["chat_outcome"] not in _CHAT_OUTCOMES:
        raise TelemetryError from None
    if "token_kind" in values and values["token_kind"] not in _TOKEN_KINDS:
        raise TelemetryError from None
    if "usage_state" in values and values["usage_state"] not in _USAGE_STATES:
        raise TelemetryError from None
    if (
        "readiness_dimension" in values
        and values["readiness_dimension"] not in _READINESS_DIMENSIONS
    ):
        raise TelemetryError from None
    if "readiness_state" in values and values["readiness_state"] not in _READINESS_STATES:
        raise TelemetryError from None
    if "readiness_reason" in values and values["readiness_reason"] not in _READINESS_REASONS:
        raise TelemetryError from None
    if "value" in values:
        value = values["value"]
        if model is ProviderReportedTotalTokens:
            if type(value) is not int or not 0 <= value <= MAX_SAFE_TOKEN_COUNT:
                raise TelemetryError from None
        elif type(value) is Decimal:
            if not _valid_duration(value):
                raise TelemetryError from None


def _valid_duration(value: object) -> bool:
    if (
        type(value) is not Decimal
        or not value.is_finite()
        or not 0 <= value <= _MAX_DURATION_SECONDS
    ):
        return False
    if value and value.adjusted() < -9:
        return False
    nanoseconds = value * Decimal(1_000_000_000)
    return nanoseconds == nanoseconds.to_integral_value()


def _canonical_duration(value: object) -> Decimal:
    if not _valid_duration(value):
        raise TelemetryError from None
    nanoseconds = int(cast(Decimal, value) * Decimal(1_000_000_000))
    return Decimal(nanoseconds) / Decimal(1_000_000_000)


class _TelemetryModelMeta(ModelMetaclass):
    def __call__(cls, *args: object, **kwargs: object) -> Any:
        result: object = _FAILED
        try:
            result = super().__call__(*args, **kwargs)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException as error:
            _release_exception(error)
            pass
        if result is _FAILED:
            del args, kwargs, result
            raise TelemetryError from None
        return result


class TelemetryModel(BaseModel, metaclass=_TelemetryModelMeta):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        hide_input_in_errors=True,
        validate_default=True,
        revalidate_instances="always",
    )

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        schema = handler(source_type)

        def validate(value: object, inner: Any, info: Any) -> object:
            controlled: object = _FAILED
            try:
                controlled = _event_input(cls, value, info.mode)
                return inner(controlled)
            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                raise
            except BaseException as error:
                _release_exception(error)
                del value, controlled, inner, info
                raise TelemetryError from None

        return core_schema.with_info_wrap_validator_function(validate, schema)

    @classmethod
    def model_validate(cls, obj: Any, **kwargs: Any) -> Self:
        kwargs["strict"] = True
        kwargs["extra"] = "forbid"
        kwargs["from_attributes"] = False
        result: object = _FAILED
        try:
            result = super().model_validate(obj, **kwargs)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException as error:
            _release_exception(error)
            pass
        if result is _FAILED:
            del obj, kwargs, result
            raise TelemetryError from None
        return cast(Self, result)

    @classmethod
    def model_construct(cls, _fields_set: set[str] | None = None, **values: Any) -> Self:
        result: object = _FAILED
        try:
            if _fields_set is not None:
                if type(_fields_set) is not set or len(_fields_set) > len(cls.model_fields):
                    raise TelemetryError
                if any(type(item) is not str for item in set.__iter__(_fields_set)):
                    raise TelemetryError
                if not _fields_set.issubset(cls.model_fields):
                    raise TelemetryError
            result = cls(**values)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException as error:
            _release_exception(error)
            pass
        if result is _FAILED:
            del _fields_set, values, result
            raise TelemetryError from None
        return cast(Self, result)

    @classmethod
    def construct(cls, _fields_set: set[str] | None = None, **values: Any) -> Self:
        result: object = _FAILED
        try:
            result = cls.model_construct(_fields_set=_fields_set, **values)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException as error:
            _release_exception(error)
            pass
        if result is _FAILED:
            del _fields_set, values, result
            raise TelemetryError from None
        return cast(Self, result)

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        result: object = _FAILED
        values: object = _FAILED
        items: object = _FAILED
        try:
            if type(deep) is not bool or (update is not None and type(update) is not dict):
                raise TelemetryError
            values = _event_input(type(self), self, "python")
            if update is not None:
                items = _dict_items(update, maximum=len(type(self).model_fields))
                if any(key not in type(self).model_fields for key, _ in items):
                    raise TelemetryError
                values.update(items)
            result = type(self)(**values)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException as error:
            _release_exception(error)
            pass
        if result is _FAILED:
            del self, update, deep, values, items, result
            raise TelemetryError from None
        return cast(Self, result)

    def copy(
        self,
        *,
        include: Any = None,
        exclude: Any = None,
        update: dict[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        result: object = _FAILED
        try:
            if include is not None or exclude is not None:
                raise TelemetryError
            result = self.model_copy(update=update, deep=deep)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException as error:
            _release_exception(error)
            pass
        if result is _FAILED:
            del self, include, exclude, update, deep, result
            raise TelemetryError from None
        return cast(Self, result)

    def __replace__(self, **changes: Any) -> Self:
        result: object = _FAILED
        try:
            result = self.model_copy(update=changes)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException as error:
            _release_exception(error)
            pass
        if result is _FAILED:
            del self, changes, result
            raise TelemetryError from None
        return cast(Self, result)

    @model_serializer(mode="wrap")
    def _safe_serialize(self, handler: Any) -> Any:
        result: object = _FAILED
        try:
            _event_input(type(self), self, "python")
            result = handler(self)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException as error:
            _release_exception(error)
            pass
        if result is _FAILED:
            del self, handler, result
            raise TelemetryError from None
        return result

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise TelemetryError from None

    def __delattr__(self, name: str) -> None:
        del name
        raise TelemetryError from None


class ChatRequestsTotal(TelemetryModel):
    contract_version: Literal["1.0"] = "1.0"
    kind: Literal["counter"] = "counter"
    metric: Literal["cairn_chat_requests_total"] = "cairn_chat_requests_total"
    delta: Annotated[StrictInt, Field(ge=1, le=MAX_SAFE_TOKEN_COUNT)] = 1

    @field_validator("delta")
    @classmethod
    def _one(cls, value: int) -> int:
        if type(value) is not int or value != 1:
            raise TelemetryError from None
        return value


class ChatOutcomeTotal(TelemetryModel):
    contract_version: Literal["1.0"] = "1.0"
    kind: Literal["counter"] = "counter"
    metric: Literal["cairn_chat_outcomes_total"] = "cairn_chat_outcomes_total"
    chat_outcome: ChatOutcome
    delta: Annotated[StrictInt, Field(ge=1, le=MAX_SAFE_TOKEN_COUNT)] = 1

    @field_validator("chat_outcome")
    @classmethod
    def _outcome(cls, value: str) -> str:
        if type(value) is not str or value not in _CHAT_OUTCOMES:
            raise TelemetryError from None
        return value

    @field_validator("delta")
    @classmethod
    def _one(cls, value: int) -> int:
        if type(value) is not int or value != 1:
            raise TelemetryError from None
        return value


class ChatDurationSeconds(TelemetryModel):
    contract_version: Literal["1.0"] = "1.0"
    kind: Literal["histogram"] = "histogram"
    metric: Literal["cairn_chat_duration_seconds"] = "cairn_chat_duration_seconds"
    chat_outcome: ChatOutcome
    value: Decimal

    @field_validator("chat_outcome")
    @classmethod
    def _outcome(cls, value: str) -> str:
        if type(value) is not str or value not in _CHAT_OUTCOMES:
            raise TelemetryError from None
        return value

    @field_validator("value", mode="before")
    @classmethod
    def _json_decimal(cls, value: object, info: Any) -> object:
        if info.mode == "json" and type(value) is str:
            try:
                return Decimal(value)
            except Exception:
                raise TelemetryError from None
        return value

    @field_validator("value")
    @classmethod
    def _duration(cls, value: Decimal) -> Decimal:
        return _canonical_duration(value)


class ProviderUsageRecordsTotal(TelemetryModel):
    contract_version: Literal["1.0"] = "1.0"
    kind: Literal["counter"] = "counter"
    metric: Literal["cairn_provider_usage_records_total"] = "cairn_provider_usage_records_total"
    provider: Literal["ollama", "gemini"]
    model: str
    provider_attempt: Annotated[StrictInt, Field(ge=1, le=2)]
    usage_state: UsageState
    delta: Annotated[StrictInt, Field(ge=1, le=MAX_SAFE_TOKEN_COUNT)] = 1

    @field_validator("provider", "model", "usage_state")
    @classmethod
    def _strings(cls, value: str, info: Any) -> str:
        if type(value) is not str or not value:
            raise TelemetryError from None
        if info.field_name == "usage_state" and value not in _USAGE_STATES:
            raise TelemetryError from None
        if info.field_name == "model":
            return validate_model_identifier(value)
        return value

    @field_validator("delta")
    @classmethod
    def _one(cls, value: int) -> int:
        if type(value) is not int or value != 1:
            raise TelemetryError from None
        return value


class ProviderUsageTokensTotal(TelemetryModel):
    contract_version: Literal["1.0"] = "1.0"
    kind: Literal["counter"] = "counter"
    metric: Literal["cairn_provider_usage_tokens_total"] = "cairn_provider_usage_tokens_total"
    provider: Literal["ollama", "gemini"]
    model: str
    provider_attempt: Annotated[StrictInt, Field(ge=1, le=2)]
    token_kind: TokenKind
    delta: Annotated[StrictInt, Field(ge=1, le=MAX_SAFE_TOKEN_COUNT)]

    @field_validator("provider", "model", "token_kind")
    @classmethod
    def _strings(cls, value: str, info: Any) -> str:
        if type(value) is not str or not value:
            raise TelemetryError from None
        if info.field_name == "token_kind" and value not in _TOKEN_KINDS:
            raise TelemetryError from None
        if info.field_name == "model":
            return validate_model_identifier(value)
        return value


class ProviderReportedTotalTokens(TelemetryModel):
    contract_version: Literal["1.0"] = "1.0"
    kind: Literal["histogram"] = "histogram"
    metric: Literal["cairn_provider_reported_total_tokens"] = "cairn_provider_reported_total_tokens"
    provider: Literal["ollama", "gemini"]
    model: str
    provider_attempt: Annotated[StrictInt, Field(ge=1, le=2)]
    value: Annotated[StrictInt, Field(ge=0, le=MAX_SAFE_TOKEN_COUNT)]

    @field_validator("provider", "model")
    @classmethod
    def _strings(cls, value: str) -> str:
        if type(value) is not str or not value:
            raise TelemetryError from None
        return validate_model_identifier(value)


class ReadinessChecksTotal(TelemetryModel):
    contract_version: Literal["1.0"] = "1.0"
    kind: Literal["counter"] = "counter"
    metric: Literal["cairn_readiness_checks_total"] = "cairn_readiness_checks_total"
    readiness_dimension: str
    readiness_state: str
    readiness_reason: str
    required: StrictBool
    delta: Annotated[StrictInt, Field(ge=1, le=MAX_SAFE_TOKEN_COUNT)] = 1

    @field_validator("readiness_dimension", "readiness_state", "readiness_reason")
    @classmethod
    def _labels(cls, value: str, info: Any) -> str:
        allowed = {
            "readiness_dimension": _READINESS_DIMENSIONS,
            "readiness_state": _READINESS_STATES,
            "readiness_reason": _READINESS_REASONS,
        }[info.field_name]
        if type(value) is not str or value not in allowed:
            raise TelemetryError from None
        return value

    @field_validator("delta")
    @classmethod
    def _one(cls, value: int) -> int:
        if type(value) is not int or value != 1:
            raise TelemetryError from None
        return value

    @model_validator(mode="after")
    def _relation(self) -> Self:
        try:
            ReadinessCheck(
                dimension=cast(Any, self.readiness_dimension),
                state=cast(Any, self.readiness_state),
                reason=cast(Any, self.readiness_reason),
                required=self.required,
            )
        except BaseException:
            raise TelemetryError from None
        return self


class ReadinessDurationSeconds(TelemetryModel):
    contract_version: Literal["1.0"] = "1.0"
    kind: Literal["histogram"] = "histogram"
    metric: Literal["cairn_readiness_duration_seconds"] = "cairn_readiness_duration_seconds"
    value: Decimal

    @field_validator("value", mode="before")
    @classmethod
    def _json_decimal(cls, value: object, info: Any) -> object:
        if info.mode == "json" and type(value) is str:
            try:
                return Decimal(value)
            except Exception:
                raise TelemetryError from None
        return value

    @field_validator("value")
    @classmethod
    def _duration(cls, value: Decimal) -> Decimal:
        return _canonical_duration(value)


_EventUnion = (
    ChatRequestsTotal
    | ChatOutcomeTotal
    | ChatDurationSeconds
    | ProviderUsageRecordsTotal
    | ProviderUsageTokensTotal
    | ProviderReportedTotalTokens
    | ReadinessChecksTotal
    | ReadinessDurationSeconds
)
_EVENT_TYPES = {
    ChatRequestsTotal,
    ChatOutcomeTotal,
    ChatDurationSeconds,
    ProviderUsageRecordsTotal,
    ProviderUsageTokensTotal,
    ProviderReportedTotalTokens,
    ReadinessChecksTotal,
    ReadinessDurationSeconds,
}
_METRIC_BY_TYPE: dict[type[TelemetryModel], str] = {
    ChatRequestsTotal: "cairn_chat_requests_total",
    ChatOutcomeTotal: "cairn_chat_outcomes_total",
    ChatDurationSeconds: "cairn_chat_duration_seconds",
    ProviderUsageRecordsTotal: "cairn_provider_usage_records_total",
    ProviderUsageTokensTotal: "cairn_provider_usage_tokens_total",
    ProviderReportedTotalTokens: "cairn_provider_reported_total_tokens",
    ReadinessChecksTotal: "cairn_readiness_checks_total",
    ReadinessDurationSeconds: "cairn_readiness_duration_seconds",
}


def _validate_event_union(value: object, handler: Any, info: Any) -> object:
    controlled: object = _FAILED
    items: object = _FAILED
    try:
        if type(value) in _EVENT_TYPES:
            controlled = value
        else:
            items = _dict_items(value, maximum=9)
            controlled = {key: item for key, item in items}
            metric = dict.get(controlled, "metric")
            if type(metric) is not str:
                raise TelemetryError
        return handler(controlled)
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        raise
    except BaseException as error:
        _release_exception(error)
        del value, handler, info, controlled, items
        raise TelemetryError from None


def _serialize_event_union(value: object, handler: Any, info: Any) -> object:
    copied: object = _FAILED
    try:
        if type(value) not in _EVENT_TYPES:
            raise TelemetryError
        event_type = cast(type[TelemetryModel], type(value))
        copied = event_type.model_validate(value)
        return handler(copied)
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        raise
    except BaseException as error:
        _release_exception(error)
        del value, handler, info, copied
        raise TelemetryError from None


ExactTelemetryEvent = Annotated[
    _EventUnion,
    Field(discriminator="metric"),
    WrapValidator(_validate_event_union),
    WrapSerializer(_serialize_event_union),
]


def _copy_usage(value: object) -> ProviderUsage:
    raw = _raw_model(value, ProviderUsage)
    fields = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "thinking_tokens",
        "total_tokens",
    )
    if any(raw[name] is not None and type(raw[name]) is not int for name in fields):
        raise TelemetryError from None
    return ProviderUsage.model_validate({name: raw[name] for name in fields})


def _copy_cost(value: object) -> ProviderAttemptCostRecord:
    raw = _raw_model(value, ProviderAttemptCostRecord)
    string_fields = (
        "schema_version",
        "provider",
        "model",
        "completion_state",
        "answer_outcome",
        "cost_state",
    )
    if any(type(raw[name]) is not str for name in string_fields):
        raise TelemetryError from None
    if type(raw["provider_attempt"]) is not int or type(raw["attempt_date"]) is not date:
        raise TelemetryError from None
    for name in ("service_tier", "snapshot_id", "currency"):
        if raw[name] is not None and type(raw[name]) is not str:
            raise TelemetryError from None
    if raw["model_cost"] is not None and type(raw["model_cost"]) is not Decimal:
        raise TelemetryError from None
    usage = None if raw["usage"] is None else _copy_usage(raw["usage"])
    controlled = dict(raw)
    controlled["usage"] = usage
    return ProviderAttemptCostRecord.model_validate(controlled)


def _copy_summary(value: object) -> RequestAccountingSummary:
    try:
        raw = _raw_model(value, RequestAccountingSummary)
        if (
            type(raw["contract_version"]) is not str
            or type(raw["provider_work_started"]) is not bool
            or type(raw["request_completion"]) is not str
            or type(raw["attempts"]) is not tuple
            or len(cast(tuple[object, ...], raw["attempts"])) > 2
        ):
            raise TelemetryError
        attempts: list[SettledAttempt | UncertainAttempt] = []
        for attempt in tuple.__iter__(cast(tuple[object, ...], raw["attempts"])):
            if type(attempt) is SettledAttempt:
                state = _raw_model(attempt, SettledAttempt)
                if type(state["kind"]) is not str or state["kind"] != "settled":
                    raise TelemetryError
                from app.request_accounting import AttemptIdentity

                identity_state = _raw_model(state["identity"], AttemptIdentity)
                if (
                    any(
                        type(identity_state[name]) is not str
                        for name in ("contract_version", "provider", "model")
                    )
                    or type(identity_state["provider_attempt"]) is not int
                ):
                    raise TelemetryError
                identity = AttemptIdentity.model_validate(identity_state)
                tier = state["service_tier"]
                if tier is not None and type(tier) is not str:
                    raise TelemetryError
                attempts.append(
                    SettledAttempt(
                        identity=identity,
                        service_tier=tier,
                        cost_record=_copy_cost(state["cost_record"]),
                    )
                )
            elif type(attempt) is UncertainAttempt:
                state = _raw_model(attempt, UncertainAttempt)
                from app.request_accounting import AttemptIdentity

                identity_state = _raw_model(state["identity"], AttemptIdentity)
                if (
                    any(
                        type(identity_state[name]) is not str
                        for name in ("contract_version", "provider", "model")
                    )
                    or type(identity_state["provider_attempt"]) is not int
                ):
                    raise TelemetryError
                tier = state["service_tier"]
                if tier is not None and type(tier) is not str:
                    raise TelemetryError
                if (
                    type(state["kind"]) is not str
                    or state["kind"] != "uncertain"
                    or type(state["completion"]) is not str
                    or state["completion"] != "uncertain"
                    or type(state["reason"]) is not str
                ):
                    raise TelemetryError
                attempts.append(
                    UncertainAttempt(
                        identity=AttemptIdentity.model_validate(identity_state),
                        service_tier=tier,
                        last_usage=None
                        if state["last_usage"] is None
                        else _copy_usage(state["last_usage"]),
                        reason=cast(Any, state["reason"]),
                    )
                )
            else:
                raise TelemetryError
        return RequestAccountingSummary(
            contract_version=cast(Any, raw["contract_version"]),
            provider_work_started=raw["provider_work_started"],
            request_completion=cast(Any, raw["request_completion"]),
            attempts=tuple(attempts),
        )
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        raise
    except BaseException:
        del value
        raise TelemetryError from None


def _copy_readiness(value: object) -> ReadinessReport:
    try:
        raw = _raw_model(value, ReadinessReport)
        if (
            type(raw["contract_version"]) is not str
            or type(raw["ready"]) is not bool
            or type(raw["checks"]) is not tuple
            or len(cast(tuple[object, ...], raw["checks"])) != 8
        ):
            raise TelemetryError
        copied: list[ReadinessCheck] = []
        for check in tuple.__iter__(cast(tuple[object, ...], raw["checks"])):
            state = _raw_model(check, ReadinessCheck)
            if (
                any(
                    type(state[name]) is not str
                    for name in ("contract_version", "dimension", "state", "reason")
                )
                or type(state["required"]) is not bool
            ):
                raise TelemetryError
            copied.append(ReadinessCheck.model_validate(state))
        return ReadinessReport(
            contract_version=cast(Any, raw["contract_version"]),
            checks=tuple(copied),
            ready=raw["ready"],
        )
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        raise
    except BaseException:
        del value
        raise TelemetryError from None


def _duration_seconds(elapsed_ns: object) -> Decimal | None:
    if type(elapsed_ns) is not int or not 0 <= elapsed_ns <= MAX_TELEMETRY_DURATION_NS:
        return None
    return Decimal(elapsed_ns) / Decimal(1_000_000_000)


class TelemetryProjector:
    """Pure projection bound to one application-owned provider identity."""

    __slots__ = (
        "_provider",
        "_model",
        "_expected_provider",
        "_expected_model",
        "_settings",
        "_bound_provider",
        "_binding",
        "_brand",
    )

    def __init__(
        self,
        provider: Literal["ollama", "gemini"] | None,
        model: str | None,
        *,
        settings: object = None,
        bound_provider: object = None,
        binding: ControlledProviderAccountingBinding | None = None,
        authority: object = None,
    ) -> None:
        if authority is not _PROJECTOR_AUTHORITY:
            raise TelemetryError from None
        if (provider is None) is not (model is None):
            raise TelemetryError from None
        if provider is not None and (
            type(provider) is not str
            or provider not in {"ollama", "gemini"}
            or type(model) is not str
            or not model
        ):
            raise TelemetryError from None
        object.__setattr__(self, "_provider", provider)
        object.__setattr__(self, "_model", model)
        object.__setattr__(self, "_expected_provider", provider)
        object.__setattr__(self, "_expected_model", model)
        object.__setattr__(self, "_settings", settings)
        object.__setattr__(self, "_bound_provider", bound_provider)
        object.__setattr__(self, "_binding", binding)
        object.__setattr__(self, "_brand", _PROJECTOR_AUTHORITY)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise TelemetryError from None

    def _identity(self) -> tuple[str | None, str | None]:
        if (
            type(self) is not TelemetryProjector
            or object.__getattribute__(self, "_brand") is not _PROJECTOR_AUTHORITY
        ):
            raise TelemetryError from None
        provider = object.__getattribute__(self, "_provider")
        model = object.__getattribute__(self, "_model")
        expected_stored_provider = object.__getattribute__(self, "_expected_provider")
        expected_stored_model = object.__getattribute__(self, "_expected_model")
        if provider is None:
            if model is not None:
                raise TelemetryError from None
        elif (
            type(provider) is not str
            or provider not in {"ollama", "gemini"}
            or type(model) is not str
            or not model
        ):
            raise TelemetryError from None
        if expected_stored_provider is None:
            if expected_stored_model is not None:
                raise TelemetryError from None
        elif (
            type(expected_stored_provider) is not str
            or expected_stored_provider not in {"ollama", "gemini"}
            or type(expected_stored_model) is not str
            or not expected_stored_model
        ):
            raise TelemetryError from None
        if provider != expected_stored_provider or model != expected_stored_model:
            raise TelemetryError from None
        binding = object.__getattribute__(self, "_binding")
        if binding is not None:
            try:
                policy = validate_controlled_provider_binding(
                    object.__getattribute__(self, "_settings"),
                    object.__getattribute__(self, "_bound_provider"),
                    binding,
                )
            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                raise
            except BaseException:
                raise TelemetryError from None
            expected_provider = (
                policy.provider if policy.provider in {"ollama", "gemini"} else None
            )
            expected_model = policy.model if expected_provider is not None else None
            if provider != expected_provider or model != expected_model:
                raise TelemetryError from None
        elif (
            object.__getattribute__(self, "_settings") is not None
            or object.__getattribute__(self, "_bound_provider") is not None
        ):
            raise TelemetryError from None
        return provider, model

    def project_chat_start(self) -> tuple[ExactTelemetryEvent, ...]:
        result: object = _FAILED
        try:
            self._identity()
            result = (ChatRequestsTotal(),)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            pass
        if result is _FAILED:
            del self, result
            raise TelemetryError from None
        return cast(tuple[ExactTelemetryEvent, ...], result)

    def _project_chat_completion(
        self, summary: RequestAccountingSummary, elapsed_ns: object
    ) -> tuple[ExactTelemetryEvent, ...]:
        provider, model = self._identity()
        copied = _copy_summary(summary)
        if any(
            provider is None
            or attempt.identity.provider != provider
            or attempt.identity.model != model
            for attempt in copied.attempts
        ):
            raise TelemetryError from None
        outcome = {
            "completed": "grounded",
            "refused": "refused",
            "limit": "limit",
            "error": "error",
            "abandoned": "error",
            "cancelled": "cancelled",
        }[copied.request_completion]
        events: list[ExactTelemetryEvent] = [
            ChatOutcomeTotal(chat_outcome=cast(ChatOutcome, outcome))
        ]
        duration = _duration_seconds(elapsed_ns)
        if duration is not None:
            events.append(
                ChatDurationSeconds(chat_outcome=cast(ChatOutcome, outcome), value=duration)
            )
        for attempt in copied.attempts:
            identity = attempt.identity
            assert provider is not None and model is not None
            provider_label = cast(Literal["ollama", "gemini"], provider)
            model_label = model
            if type(attempt) is UncertainAttempt:
                events.append(
                    ProviderUsageRecordsTotal(
                        provider=provider_label,
                        model=model_label,
                        provider_attempt=identity.provider_attempt,
                        usage_state="uncertain",
                    )
                )
                continue
            record = cast(SettledAttempt, attempt).cost_record
            usage_state = {
                "priced": "complete",
                "usage_incomplete": "incomplete",
                "usage_missing": "missing",
                "snapshot_missing": "complete",
            }[record.cost_state]
            events.append(
                ProviderUsageRecordsTotal(
                    provider=provider_label,
                    model=model_label,
                    provider_attempt=identity.provider_attempt,
                    usage_state=cast(UsageState, usage_state),
                )
            )
            usage = record.usage
            if usage is None:
                continue
            token_values = (
                ("input", usage.input_tokens),
                ("cached_input", usage.cached_input_tokens),
                ("output", usage.output_tokens),
                ("thinking", usage.thinking_tokens),
                ("total", usage.total_tokens),
            )
            for token_kind, count in token_values:
                if count is not None and count > 0:
                    events.append(
                        ProviderUsageTokensTotal(
                            provider=provider_label,
                            model=model_label,
                            provider_attempt=identity.provider_attempt,
                            token_kind=cast(TokenKind, token_kind),
                            delta=count,
                        )
                    )
            if usage.total_tokens is not None:
                events.append(
                    ProviderReportedTotalTokens(
                        provider=provider_label,
                        model=model_label,
                        provider_attempt=identity.provider_attempt,
                        value=usage.total_tokens,
                    )
                )
        if len(events) > 16:
            raise TelemetryError from None
        return tuple(events)

    def project_chat_completion(
        self, summary: RequestAccountingSummary, elapsed_ns: object
    ) -> tuple[ExactTelemetryEvent, ...]:
        result: object = _FAILED
        try:
            result = self._project_chat_completion(summary, elapsed_ns)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            pass
        if result is _FAILED:
            del self, summary, elapsed_ns, result
            raise TelemetryError from None
        return cast(tuple[ExactTelemetryEvent, ...], result)

    def _project_readiness(
        self, report: ReadinessReport, elapsed_ns: object
    ) -> tuple[ExactTelemetryEvent, ...]:
        self._identity()
        copied = _copy_readiness(report)
        events: list[ExactTelemetryEvent] = [
            ReadinessChecksTotal(
                readiness_dimension=check.dimension,
                readiness_state=check.state,
                readiness_reason=check.reason,
                required=check.required,
            )
            for check in copied.checks
        ]
        duration = _duration_seconds(elapsed_ns)
        if duration is not None:
            events.append(ReadinessDurationSeconds(value=duration))
        if len(events) > 9:
            raise TelemetryError from None
        return tuple(events)

    def project_readiness(
        self, report: ReadinessReport, elapsed_ns: object
    ) -> tuple[ExactTelemetryEvent, ...]:
        result: object = _FAILED
        try:
            result = self._project_readiness(report, elapsed_ns)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            pass
        if result is _FAILED:
            del self, report, elapsed_ns, result
            raise TelemetryError from None
        return cast(tuple[ExactTelemetryEvent, ...], result)


def build_telemetry_projector(
    settings: object,
    provider: object,
    binding: ControlledProviderAccountingBinding | None,
) -> TelemetryProjector:
    result: object = _FAILED
    try:
        if binding is None:
            result = TelemetryProjector(None, None, authority=_PROJECTOR_AUTHORITY)
        else:
            policy = validate_controlled_provider_binding(settings, provider, binding)
            selected_provider = (
                policy.provider if policy.provider in {"ollama", "gemini"} else None
            )
            selected_model = policy.model if selected_provider is not None else None
            result = TelemetryProjector(
                cast(Literal["ollama", "gemini"] | None, selected_provider),
                selected_model,
                settings=settings,
                bound_provider=provider,
                binding=binding,
                authority=_PROJECTOR_AUTHORITY,
            )
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        raise
    except BaseException:
        pass
    if result is _FAILED:
        del settings, provider, binding, result
        raise TelemetryError from None
    return cast(TelemetryProjector, result)


class TelemetrySink(Protocol):
    def emit(self, event: ExactTelemetryEvent) -> None: ...


class NullTelemetrySink:
    __slots__ = ()

    def emit(self, event: ExactTelemetryEvent) -> None:
        _copy_event(event)


def _copy_event(event: ExactTelemetryEvent) -> ExactTelemetryEvent:
    if type(event) not in _EVENT_TYPES:
        raise TelemetryError from None
    return type(event).model_validate(event)


def deliver_telemetry(
    sink: TelemetrySink,
    events: tuple[ExactTelemetryEvent, ...],
) -> bool:
    if type(events) is not tuple or len(events) > 17:
        raise TelemetryError from None
    copied = tuple(_copy_event(event) for event in tuple.__iter__(events))
    try:
        emit = sink.emit
        for event in copied:
            emit(event)
    except Exception:
        emit_app_log(TelemetrySinkFailedLog())
        return False
    return True


def _read_clock(clock: Callable[[], int]) -> int | None:
    try:
        value = clock()
    except Exception:
        emit_app_log(TelemetryInputRejectedLog())
        return None
    if type(value) is not int:
        emit_app_log(TelemetryInputRejectedLog())
        return None
    return value


def _elapsed(start: int | None, end: int | None) -> int | None:
    if start is None or end is None:
        return None
    value = end - start
    if not 0 <= value <= MAX_TELEMETRY_DURATION_NS:
        emit_app_log(TelemetryInputRejectedLog())
        return None
    return value


class ChatTelemetryUnit:
    """One captured request sink, clock, projector, and failure fuse."""

    __slots__ = ("_projector", "_sink", "_clock", "_start", "_fused", "_finished")

    def __init__(
        self,
        projector: object,
        sink: TelemetrySink,
        clock: Callable[[], int],
    ) -> None:
        self._projector = projector if type(projector) is TelemetryProjector else None
        self._sink = sink
        self._clock = clock
        self._fused = self._projector is None
        self._finished = False
        if self._projector is None:
            self._start = None
            emit_app_log(TelemetryInputRejectedLog())
            return
        self._start = _read_clock(clock)
        try:
            self._fused = not deliver_telemetry(sink, self._projector.project_chat_start())
        except TelemetryError:
            self._fused = True
            emit_app_log(TelemetryInputRejectedLog())

    def complete(self, summary: RequestAccountingSummary) -> None:
        if self._finished:
            raise TelemetryError from None
        self._finished = True
        if self._projector is None:
            return
        end = _read_clock(self._clock)
        elapsed = _elapsed(self._start, end)
        try:
            events = self._projector.project_chat_completion(summary, elapsed)
        except TelemetryError:
            emit_app_log(TelemetryInputRejectedLog())
            return
        if not self._fused:
            self._fused = not deliver_telemetry(self._sink, events)

    def summary_missing(self) -> None:
        if self._finished:
            raise TelemetryError from None
        self._finished = True
        if self._projector is None:
            return
        _read_clock(self._clock)
        emit_app_log(TelemetryInputRejectedLog())


class ReadinessTelemetryUnit:
    __slots__ = (
        "_projector",
        "_sink",
        "_clock",
        "_start",
        "_started",
        "_finished",
    )

    def __init__(
        self,
        projector: object,
        sink: TelemetrySink,
        clock: Callable[[], int],
    ) -> None:
        self._projector = projector if type(projector) is TelemetryProjector else None
        self._sink = sink
        self._clock = clock
        self._start: int | None = None
        self._started = False
        self._finished = False
        if self._projector is None:
            emit_app_log(TelemetryInputRejectedLog())

    def start(self) -> None:
        if self._started or self._finished:
            raise TelemetryError from None
        self._started = True
        if self._projector is None:
            return
        self._start = _read_clock(self._clock)

    def complete(self, report: ReadinessReport) -> None:
        if self._finished or not self._started:
            raise TelemetryError from None
        self._finished = True
        if self._projector is None:
            return
        end = _read_clock(self._clock)
        elapsed = _elapsed(self._start, end)
        try:
            events = self._projector.project_readiness(report, elapsed)
        except TelemetryError:
            emit_app_log(TelemetryInputRejectedLog())
            return
        deliver_telemetry(self._sink, events)


def monotonic_ns() -> int:
    return time.monotonic_ns()
