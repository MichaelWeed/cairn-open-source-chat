"""Strict, content-free aggregate readiness for the selected application profile."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from threading import RLock
from typing import Annotated, Any, Literal, Protocol, Self, cast

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetCoreSchemaHandler,
    StrictBool,
    model_validator,
)
from pydantic_core import CoreSchema, core_schema

from app.providers.gemini import GeminiReadiness
from app.retrieval_contracts import (
    ExactCorpusReference,
    LocalActiveScope,
    RetrievalProbe,
    RetrievalScope,
)

READINESS_CONTRACT_VERSION: Literal["1.0"] = "1.0"
OLLAMA_CATALOG_MAX_RESPONSE_BYTES = 1_048_576
OLLAMA_CATALOG_MAX_MODELS = 1_024
OLLAMA_MODEL_NAME_MAX_CHARS = 256
OLLAMA_CATALOG_TIMEOUT_SECONDS = 5.0

ReadinessState = Literal["ready", "not_ready", "not_required", "unknown"]
ReadinessDimension = Literal[
    "database",
    "vector_store",
    "corpus",
    "provider",
    "model",
    "embedding",
    "exact_corpus",
    "budget",
]
ReadinessReason = Literal[
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
]
RetrievalReadinessProfile = Literal[
    "local_static",
    "firestore_static",
    "lifecycle_exact",
]

_DIMENSION_ORDER: tuple[ReadinessDimension, ...] = (
    "database",
    "vector_store",
    "corpus",
    "provider",
    "model",
    "embedding",
    "exact_corpus",
    "budget",
)
_ERROR_MESSAGE = "Readiness input is invalid."
_MISSING = object()
_MODEL_REBUILD_LOCK = RLock()
_SUPPRESS_TRANSPORT_LOGS = ContextVar("suppress_readiness_transport_logs", default=False)
_TRANSPORT_LOG_FILTER_LOCK = RLock()
_TRANSPORT_LOG_FILTER_USERS = 0
_TRANSPORT_LOGGER_NAMES = (
    "httpx",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)


class _ReadinessTransportLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        del record
        return not _SUPPRESS_TRANSPORT_LOGS.get()


_TRANSPORT_LOG_FILTER = _ReadinessTransportLogFilter()


@contextmanager
def _without_transport_logs() -> Iterator[None]:
    global _TRANSPORT_LOG_FILTER_USERS

    token = _SUPPRESS_TRANSPORT_LOGS.set(True)
    with _TRANSPORT_LOG_FILTER_LOCK:
        if _TRANSPORT_LOG_FILTER_USERS == 0:
            for name in _TRANSPORT_LOGGER_NAMES:
                logging.getLogger(name).addFilter(_TRANSPORT_LOG_FILTER)
        _TRANSPORT_LOG_FILTER_USERS += 1
    try:
        yield
    finally:
        _SUPPRESS_TRANSPORT_LOGS.reset(token)
        with _TRANSPORT_LOG_FILTER_LOCK:
            _TRANSPORT_LOG_FILTER_USERS -= 1
            if _TRANSPORT_LOG_FILTER_USERS == 0:
                for name in _TRANSPORT_LOGGER_NAMES:
                    logging.getLogger(name).removeFilter(_TRANSPORT_LOG_FILTER)


class ReadinessError(Exception):
    """Fixed error that never retains or renders rejected readiness input."""

    def __init__(self) -> None:
        super().__init__(_ERROR_MESSAGE)

    def __repr__(self) -> str:
        return "ReadinessError()"

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
        return [{"type": "readiness_error", "loc": (), "msg": _ERROR_MESSAGE}]

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


def _content_free[Result](operation: Callable[[], Result]) -> Result:
    result: object = _MISSING
    failed = False
    try:
        result = operation()
    except ReadinessError:
        failed = True
    except Exception:
        failed = True
    if failed or result is _MISSING:
        del operation
        raise ReadinessError() from None
    return cast(Result, result)


def _copy_fields_set(value: object, expected: Mapping[str, Any]) -> set[str]:
    if type(value) is not set or len(value) > len(expected):
        raise ValueError
    copied: set[str] = set()
    for field in set.__iter__(cast(set[object], value)):
        if type(field) is not str or field not in expected:
            raise ValueError
        copied.add(field)
    return copied


def _content_free_validation(
    model: type[ReadinessModel],
    value: Any,
    handler: Callable[[Any], Any],
    mode: str,
) -> Any:
    def validate() -> Any:
        if type(value) is dict:
            raw = value
            if len(raw) > len(model.model_fields):
                raise ValueError
            for key, _ in dict.items(raw):
                if type(key) is not str or key not in model.model_fields:
                    raise ValueError
            if model is ReadinessReport:
                checks = dict.get(raw, "checks", _MISSING)
                if type(checks) in {list, tuple} and len(checks) != len(
                    _DIMENSION_ORDER
                ):
                    raise ValueError
            elif model is OllamaCatalogReadiness:
                names = dict.get(raw, "model_names", _MISSING)
                if (
                    type(names) in {list, tuple}
                    and len(names) > OLLAMA_CATALOG_MAX_MODELS
                ):
                    raise ValueError
            if mode == "json":
                controlled = dict(raw)
                if model is ReadinessReport and type(controlled.get("checks")) is list:
                    if len(controlled["checks"]) != len(_DIMENSION_ORDER):
                        raise ValueError
                    controlled["checks"] = tuple(controlled["checks"])
                elif (
                    model is OllamaCatalogReadiness
                    and type(controlled.get("model_names")) is list
                ):
                    if len(controlled["model_names"]) > OLLAMA_CATALOG_MAX_MODELS:
                        raise ValueError
                    controlled["model_names"] = tuple(controlled["model_names"])
                return handler(controlled)
        elif type(value) is model:
            raw = object.__getattribute__(value, "__dict__")
            extra = object.__getattribute__(value, "__pydantic_extra__")
            private = object.__getattribute__(value, "__pydantic_private__")
            fields_set = object.__getattribute__(value, "__pydantic_fields_set__")
            if (
                type(raw) is not dict
                or extra is not None
                or private is not None
                or len(raw) != len(model.model_fields)
            ):
                raise ValueError
            _copy_fields_set(fields_set, model.model_fields)
            count = 0
            for key, _ in dict.items(cast(dict[object, object], raw)):
                if type(key) is not str or key not in model.model_fields:
                    raise ValueError
                count += 1
            if count != len(model.model_fields):
                raise ValueError
            if model is ReadinessReport:
                checks = dict.get(raw, "checks", _MISSING)
                if type(checks) is tuple and len(checks) != len(_DIMENSION_ORDER):
                    raise ValueError
            elif model is OllamaCatalogReadiness:
                names = dict.get(raw, "model_names", _MISSING)
                if (
                    type(names) is tuple
                    and len(names) > OLLAMA_CATALOG_MAX_MODELS
                ):
                    raise ValueError
        elif isinstance(value, Mapping):
            raise ValueError
        return handler(value)

    return _content_free(validate)


class _ContentFreeValidator:
    __slots__ = ("_validator",)

    def __init__(self, validator: Any) -> None:
        self._validator = validator

    def validate_python(self, *args: Any, **kwargs: Any) -> Any:
        return _content_free(lambda: self._validator.validate_python(*args, **kwargs))

    def validate_json(self, *args: Any, **kwargs: Any) -> Any:
        return _content_free(lambda: self._validator.validate_json(*args, **kwargs))

    def validate_strings(self, *args: Any, **kwargs: Any) -> Any:
        return _content_free(lambda: self._validator.validate_strings(*args, **kwargs))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._validator, name)


class ReadinessModel(BaseModel):
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
            cast(Any, cls).__pydantic_validator__ = _ContentFreeValidator(validator)

    @classmethod
    def model_rebuild(
        cls,
        *,
        force: bool = False,
        raise_errors: bool = True,
        _parent_namespace_depth: int = 2,
        _types_namespace: Any = None,
    ) -> bool | None:
        with _MODEL_REBUILD_LOCK:
            result = super().model_rebuild(
                force=force,
                raise_errors=raise_errors,
                _parent_namespace_depth=_parent_namespace_depth,
                _types_namespace=_types_namespace,
            )
            validator = cls.__pydantic_validator__
            if not isinstance(validator, _ContentFreeValidator):
                cast(Any, cls).__pydantic_validator__ = _ContentFreeValidator(validator)
            return result

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.with_info_wrap_validator_function(
            lambda value, validator, info: _content_free_validation(
                cls, value, validator, info.mode
            ),
            handler(source_type),
        )

    @classmethod
    def _validated(cls, values: Any) -> Self:
        return _content_free(lambda: cls.model_validate(values))

    @classmethod
    def model_construct(cls, _fields_set: set[str] | None = None, **values: Any) -> Self:
        if len(values) > len(cls.model_fields):
            raise ReadinessError() from None
        fields_set: set[str] | None = None
        if _fields_set is not None:
            fields_set = _content_free(
                lambda: _copy_fields_set(_fields_set, cls.model_fields)
            )
        result = cls._validated(values)
        if fields_set is not None:
            object.__setattr__(result, "__pydantic_fields_set__", fields_set)
        return result

    @classmethod
    def construct(cls, _fields_set: set[str] | None = None, **values: Any) -> Self:
        return cls.model_construct(_fields_set=_fields_set, **values)

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        del deep

        def prepare() -> tuple[dict[str, Any], set[str]]:
            raw = object.__getattribute__(self, "__dict__")
            extra = object.__getattribute__(self, "__pydantic_extra__")
            private = object.__getattribute__(self, "__pydantic_private__")
            raw_fields_set = object.__getattribute__(self, "__pydantic_fields_set__")
            if (
                type(raw) is not dict
                or extra is not None
                or private is not None
                or len(raw) != len(type(self).model_fields)
            ):
                raise ValueError
            values: dict[str, Any] = {}
            for key, value in dict.items(cast(dict[object, object], raw)):
                if type(key) is not str or key not in type(self).model_fields:
                    raise ValueError
                values[key] = value
            if len(values) != len(type(self).model_fields):
                raise ValueError
            fields_set = _copy_fields_set(raw_fields_set, type(self).model_fields)
            if update is not None:
                if type(update) is not dict or len(update) > len(type(self).model_fields):
                    raise ValueError
                for key, value in dict.items(cast(dict[object, object], update)):
                    if type(key) is not str or key not in type(self).model_fields:
                        raise ValueError
                    values[key] = value
                    fields_set.add(key)
            return values, fields_set

        values, fields_set = _content_free(prepare)
        result = type(self)._validated(values)
        object.__setattr__(result, "__pydantic_fields_set__", fields_set)
        return result

    def copy(
        self,
        *,
        include: Any = None,
        exclude: Any = None,
        update: dict[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if update is not None and type(update) is not dict:
            raise ReadinessError() from None
        if include is None and exclude is None and update is None:
            return self.model_copy(deep=deep)
        source = self.model_copy(deep=deep)

        def prepare() -> dict[str, Any]:
            values = source.model_dump(
                include=include, exclude=exclude, round_trip=True
            )
            if type(values) is not dict or len(values) > len(type(self).model_fields):
                raise ValueError
            if update is not None:
                if len(update) > len(type(self).model_fields):
                    raise ValueError
                for key, value in dict.items(cast(dict[object, object], update)):
                    if type(key) is not str or key not in type(self).model_fields:
                        raise ValueError
                    values[key] = value
            return values

        return type(self)._validated(_content_free(prepare))

    def __replace__(self, **changes: Any) -> Self:
        return self.model_copy(update=changes)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise ReadinessError() from None

    def __delattr__(self, name: str) -> None:
        del name
        raise ReadinessError() from None


_NEGATIVE_REASONS: dict[ReadinessDimension, frozenset[ReadinessReason]] = {
    "database": frozenset(("unavailable",)),
    "vector_store": frozenset(("unreachable", "store_unready")),
    "corpus": frozenset(("unavailable",)),
    "provider": frozenset(("unreachable",)),
    "model": frozenset(("model_missing",)),
    "embedding": frozenset(("unreachable", "model_missing")),
    "exact_corpus": frozenset(("exact_corpus_unready",)),
    "budget": frozenset(("budget_exhausted", "budget_overrun")),
}


class ReadinessCheck(ReadinessModel):
    contract_version: Literal["1.0"] = READINESS_CONTRACT_VERSION
    dimension: ReadinessDimension
    state: ReadinessState
    required: StrictBool
    reason: ReadinessReason

    @model_validator(mode="after")
    def validate_relation(self) -> ReadinessCheck:
        valid = False
        if self.state == "ready":
            valid = self.reason == "ready"
        elif self.state == "not_required":
            valid = not self.required and self.reason == "not_required"
        elif self.state == "not_ready":
            valid = self.required and self.reason in _NEGATIVE_REASONS[self.dimension]
        elif self.state == "unknown":
            valid = self.required and (
                self.reason in {"unavailable", "misconfigured"}
                or (
                    self.dimension == "budget"
                    and self.reason == "accounting_uncertain"
                )
            )
        if not valid:
            raise ReadinessError() from None
        return self


def _copy_check(value: object) -> ReadinessCheck:
    def copy() -> ReadinessCheck:
        if type(value) is not ReadinessCheck:
            raise TypeError
        raw = object.__getattribute__(value, "__dict__")
        extra = object.__getattribute__(value, "__pydantic_extra__")
        private = object.__getattribute__(value, "__pydantic_private__")
        fields_set = object.__getattribute__(value, "__pydantic_fields_set__")
        if (
            type(raw) is not dict
            or extra is not None
            or private is not None
            or len(raw) != len(ReadinessCheck.model_fields)
        ):
            raise ValueError
        _copy_fields_set(fields_set, ReadinessCheck.model_fields)
        expected = ReadinessCheck.model_fields
        controlled: dict[str, object] = {}
        for key, item in dict.items(cast(dict[object, object], raw)):
            if type(key) is not str or key not in expected:
                raise ValueError
            if key == "required":
                if type(item) is not bool:
                    raise ValueError
            elif type(item) is not str:
                raise ValueError
            controlled[key] = item
        if len(controlled) != len(expected):
            raise ValueError
        return ReadinessCheck.model_validate(controlled)

    return _content_free(copy)


class ReadinessReport(ReadinessModel):
    contract_version: Literal["1.0"] = READINESS_CONTRACT_VERSION
    checks: Annotated[tuple[ReadinessCheck, ...], Field(min_length=8, max_length=8)]
    ready: StrictBool = False

    @model_validator(mode="after")
    def validate_report(self) -> ReadinessReport:
        if type(self.checks) is not tuple or len(self.checks) != len(_DIMENSION_ORDER):
            raise ReadinessError() from None
        copied = tuple(_copy_check(check) for check in self.checks)
        if tuple(check.dimension for check in copied) != _DIMENSION_ORDER:
            raise ReadinessError() from None
        derived = all(
            not check.required or check.state == "ready"
            for check in copied
        )
        if "ready" in self.model_fields_set and self.ready is not derived:
            raise ReadinessError() from None
        object.__setattr__(self, "checks", copied)
        object.__setattr__(self, "ready", derived)
        return self


class OllamaCatalogReadiness(ReadinessModel):
    reachable: StrictBool
    model_names: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=OLLAMA_MODEL_NAME_MAX_CHARS)], ...],
        Field(max_length=OLLAMA_CATALOG_MAX_MODELS),
    ]

    @model_validator(mode="after")
    def validate_catalog(self) -> OllamaCatalogReadiness:
        if not self.reachable and self.model_names:
            raise ReadinessError() from None
        if any(type(name) is not str for name in self.model_names):
            raise ReadinessError() from None
        if len(set(self.model_names)) != len(self.model_names):
            raise ReadinessError() from None
        return self


class BudgetReadiness(ReadinessModel):
    state: Literal["ready", "not_ready", "unknown"]
    reason: Literal[
        "ready",
        "budget_exhausted",
        "accounting_uncertain",
        "budget_overrun",
        "unavailable",
        "misconfigured",
    ]

    @model_validator(mode="after")
    def validate_budget(self) -> BudgetReadiness:
        allowed = {
            "ready": frozenset(("ready",)),
            "not_ready": frozenset(("budget_exhausted", "budget_overrun")),
            "unknown": frozenset(("accounting_uncertain", "unavailable", "misconfigured")),
        }
        if self.reason not in allowed[self.state]:
            raise ReadinessError() from None
        return self


class OllamaCatalogReadinessProbe(Protocol):
    async def check_readiness(self) -> object: ...


class GeminiReadinessProbe(Protocol):
    async def check_readiness(self) -> object: ...


class BudgetReadinessProbe(Protocol):
    async def check_readiness(self) -> object: ...


class RetrievalReadinessProbe(Protocol):
    async def check_readiness(self) -> object: ...


def _parse_catalog_body(body: bytearray) -> OllamaCatalogReadiness | None:
    parsed: OllamaCatalogReadiness | None = None
    try:
        payload = json.loads(bytes(body))
        if type(payload) is not dict:
            raise ValueError
        models = dict.get(cast(dict[object, object], payload), "models")
        if type(models) is not list or len(models) > OLLAMA_CATALOG_MAX_MODELS:
            raise ValueError
        names: list[str] = []
        for item in models:
            if type(item) is not dict:
                raise ValueError
            name = dict.get(cast(dict[object, object], item), "name")
            if (
                type(name) is not str
                or not 1 <= len(name) <= OLLAMA_MODEL_NAME_MAX_CHARS
            ):
                raise ValueError
            names.append(name)
        if len(set(names)) != len(names):
            raise ValueError
        parsed = OllamaCatalogReadiness(
            reachable=True, model_names=tuple(names)
        )
    except Exception as error:
        BaseException.with_traceback(error, None)
    finally:
        body.clear()
    return parsed


class OllamaCatalogProbe:
    """One bounded no-retry read of Ollama's model catalog."""

    __slots__ = ("_base_url", "_client", "_owns_client", "_closed")

    def __init__(
        self,
        *,
        base_url: str,
        client: httpx.AsyncClient,
        owns_client: bool,
    ) -> None:
        if type(base_url) is not str or not base_url:
            raise ReadinessError() from None
        if type(client) is not httpx.AsyncClient or type(owns_client) is not bool:
            raise ReadinessError() from None
        self._base_url = _content_free(
            lambda: str(
                httpx.URL(base_url).copy_with(username=None, password=None)
            ).rstrip("/")
        )
        self._client = client
        self._owns_client = owns_client
        self._closed = False

    @classmethod
    def application_owned(cls, *, base_url: str) -> OllamaCatalogProbe:
        return cls(
            base_url=base_url,
            client=httpx.AsyncClient(
                timeout=None,
                follow_redirects=False,
                trust_env=False,
            ),
            owns_client=True,
        )

    async def _catalog_body(
        self,
    ) -> tuple[Literal["ready", "unreachable", "invalid"], bytearray | None]:
        response: httpx.Response | None = None
        outcome: Literal["ready", "unreachable", "invalid"] = "ready"
        body = bytearray()
        with _without_transport_logs():
            try:
                request = httpx.Request(
                    "GET", f"{self._base_url}/api/tags", headers={}
                )
                transport = object.__getattribute__(
                    self._client, "_transport_for_url"
                )(request.url)
                response = await transport.handle_async_request(request)
                if response.status_code < 200 or response.status_code >= 300:
                    outcome = "invalid"
                length = response.headers.get("content-length")
                if outcome == "ready" and length is not None and (
                    not length.isascii()
                    or not length.isdecimal()
                    or int(length) > OLLAMA_CATALOG_MAX_RESPONSE_BYTES
                ):
                    outcome = "invalid"
                if outcome == "ready":
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > OLLAMA_CATALOG_MAX_RESPONSE_BYTES:
                            outcome = "invalid"
                            break
                        body.extend(chunk)
            except asyncio.CancelledError:
                raise
            except httpx.TransportError as error:
                BaseException.with_traceback(error, None)
                outcome = "unreachable"
            except Exception as error:
                BaseException.with_traceback(error, None)
                outcome = "invalid"
            if response is not None:
                try:
                    await response.aclose()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    BaseException.with_traceback(error, None)
                    if outcome == "ready":
                        outcome = "invalid"
        if outcome != "ready":
            body.clear()
            return outcome, None
        return outcome, body

    async def check_readiness(self) -> OllamaCatalogReadiness:
        if self._closed:
            raise ReadinessError() from None
        if self._client.is_closed:
            raise ReadinessError() from None
        try:
            async with asyncio.timeout(OLLAMA_CATALOG_TIMEOUT_SECONDS):
                outcome, body = await self._catalog_body()
        except asyncio.CancelledError:
            raise
        except TimeoutError as error:
            BaseException.with_traceback(error, None)
            return OllamaCatalogReadiness(reachable=False, model_names=())
        except Exception as error:
            BaseException.with_traceback(error, None)
            raise ReadinessError() from None
        if outcome == "unreachable":
            return OllamaCatalogReadiness(reachable=False, model_names=())
        if outcome != "ready" or body is None:
            raise ReadinessError() from None

        parsed = _parse_catalog_body(body)
        if parsed is None:
            raise ReadinessError() from None
        return parsed

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            failed = False
            try:
                with _without_transport_logs():
                    await self._client.aclose()
            except asyncio.CancelledError:
                raise
            except Exception:
                failed = True
            if failed:
                raise ReadinessError() from None


def _copy_catalog(value: object) -> OllamaCatalogReadiness:
    return cast(OllamaCatalogReadiness, _copy_simple_model(value, OllamaCatalogReadiness))


def _copy_budget(value: object) -> BudgetReadiness:
    return cast(BudgetReadiness, _copy_simple_model(value, BudgetReadiness))


def _copy_simple_model(value: object, model: type[ReadinessModel]) -> ReadinessModel:
    def copy() -> ReadinessModel:
        if type(value) is not model:
            raise TypeError
        raw = object.__getattribute__(value, "__dict__")
        extra = object.__getattribute__(value, "__pydantic_extra__")
        private = object.__getattribute__(value, "__pydantic_private__")
        fields_set = object.__getattribute__(value, "__pydantic_fields_set__")
        if (
            type(raw) is not dict
            or extra is not None
            or private is not None
            or len(raw) != len(model.model_fields)
        ):
            raise ValueError
        _copy_fields_set(fields_set, model.model_fields)
        controlled: dict[str, object] = {}
        for key, item in dict.items(cast(dict[object, object], raw)):
            if type(key) is not str or key not in model.model_fields:
                raise ValueError
            if model is OllamaCatalogReadiness and key == "model_names":
                if (
                    type(item) is not tuple
                    or len(item) > OLLAMA_CATALOG_MAX_MODELS
                    or any(type(name) is not str for name in item)
                ):
                    raise ValueError
                controlled[key] = tuple(item)
            else:
                if type(item) not in {str, bool}:
                    raise ValueError
                controlled[key] = item
        if len(controlled) != len(model.model_fields):
            raise ValueError
        return model.model_validate(controlled)

    return _content_free(copy)


def _copy_gemini(value: object) -> GeminiReadiness:
    def copy() -> GeminiReadiness:
        if type(value) is not GeminiReadiness:
            raise TypeError
        raw = object.__getattribute__(value, "__dict__")
        if type(raw) is not dict or len(raw) != 2:
            raise ValueError
        controlled: dict[str, bool] = {}
        for key, item in dict.items(cast(dict[object, object], raw)):
            if (
                type(key) is not str
                or key not in {"reachable", "model_ready"}
                or type(item) is not bool
            ):
                raise ValueError
            controlled[key] = item
        if len(controlled) != 2:
            raise ValueError
        reachable = controlled["reachable"]
        model_ready = controlled["model_ready"]
        if type(reachable) is not bool or type(model_ready) is not bool:
            raise ValueError
        if not reachable and model_ready:
            raise ValueError
        return GeminiReadiness(reachable=reachable, model_ready=model_ready)

    return _content_free(copy)


def _copy_scope(value: object) -> RetrievalScope:
    def copy() -> RetrievalScope:
        if type(value) is LocalActiveScope:
            model: type[BaseModel] = LocalActiveScope
        elif type(value) is ExactCorpusReference:
            model = ExactCorpusReference
        else:
            raise TypeError
        raw = object.__getattribute__(value, "__dict__")
        extra = object.__getattribute__(value, "__pydantic_extra__")
        private = object.__getattribute__(value, "__pydantic_private__")
        fields_set = object.__getattribute__(value, "__pydantic_fields_set__")
        if (
            type(raw) is not dict
            or extra is not None
            or private is not None
            or len(raw) != len(model.model_fields)
        ):
            raise ValueError
        _copy_fields_set(fields_set, model.model_fields)
        controlled: dict[str, object] = {}
        for key, item in dict.items(cast(dict[object, object], raw)):
            if type(key) is not str or key not in model.model_fields or type(item) is not str:
                raise ValueError
            controlled[key] = item
        if len(controlled) != len(model.model_fields):
            raise ValueError
        return cast(RetrievalScope, model.model_validate(controlled, strict=True))

    return _content_free(copy)


def _copy_retrieval_probe(value: object) -> RetrievalProbe:
    def copy() -> RetrievalProbe:
        if type(value) is not RetrievalProbe:
            raise TypeError
        raw = object.__getattribute__(value, "__dict__")
        extra = object.__getattribute__(value, "__pydantic_extra__")
        private = object.__getattribute__(value, "__pydantic_private__")
        expected = RetrievalProbe.model_fields
        fields_set = object.__getattribute__(value, "__pydantic_fields_set__")
        if (
            type(raw) is not dict
            or extra is not None
            or private is not None
            or len(raw) != len(expected)
        ):
            raise ValueError
        _copy_fields_set(fields_set, expected)
        controlled: dict[str, object] = {}
        for key, item in dict.items(cast(dict[object, object], raw)):
            if type(key) is not str or key not in expected:
                raise ValueError
            controlled[key] = item
        if len(controlled) != len(expected):
            raise ValueError
        contract = controlled["contract_version"]
        reachable = controlled["reachable"]
        store_ready = controlled["store_ready"]
        exact_ready = controlled["exact_version_ready"]
        if (
            type(contract) is not str
            or type(reachable) is not bool
            or type(store_ready) is not bool
            or type(exact_ready) is not bool
        ):
            raise ValueError
        if contract != "1.0":
            raise ValueError
        return RetrievalProbe(
            contract_version=cast(Literal["1.0"], contract),
            scope=_copy_scope(controlled["scope"]),
            reachable=reachable,
            store_ready=store_ready,
            exact_version_ready=exact_ready,
        )

    return _content_free(copy)


def _check(
    dimension: ReadinessDimension,
    state: ReadinessState,
    required: bool,
    reason: ReadinessReason,
) -> ReadinessCheck:
    return ReadinessCheck(
        dimension=dimension,
        state=state,
        required=required,
        reason=reason,
    )


async def _call_readiness_probe(
    probe: object,
) -> tuple[object, ReadinessReason | None]:
    try:
        operation = cast(Any, probe).check_readiness
    except (AttributeError, TypeError) as error:
        BaseException.with_traceback(error, None)
        return _MISSING, "misconfigured"
    except Exception as error:
        BaseException.with_traceback(error, None)
        return _MISSING, "unavailable"
    if not callable(operation):
        return _MISSING, "misconfigured"
    try:
        pending = operation()
    except asyncio.CancelledError:
        raise
    except TypeError as error:
        BaseException.with_traceback(error, None)
        return _MISSING, "misconfigured"
    except Exception as error:
        BaseException.with_traceback(error, None)
        return _MISSING, "unavailable"
    if not inspect.isawaitable(pending):
        return _MISSING, "misconfigured"
    try:
        return await pending, None
    except asyncio.CancelledError:
        raise
    except Exception as error:
        BaseException.with_traceback(error, None)
        return _MISSING, "unavailable"


class ReadinessEvaluator:
    """Evaluate one immutable application-owned readiness composition."""

    _database_probe: Callable[[], object]
    _corpus_probe: Callable[[], object]
    _local_vector_probe: Callable[[], object]
    _route_resolver: RetrievalReadinessProbe
    _retrieval_profile: RetrievalReadinessProfile
    _retrieval_composition_valid: bool
    _expected_scope: RetrievalScope | None
    _provider_name: Literal["echo", "ollama", "gemini"]
    _provider_model: str
    _embedding_name: Literal["fake", "ollama"]
    _embedding_model: str
    _gemini_probe: GeminiReadinessProbe | None
    _catalog_probe: OllamaCatalogReadinessProbe | None
    _budget_probe: BudgetReadinessProbe | None
    _sealed: bool

    __slots__ = (
        "_database_probe",
        "_corpus_probe",
        "_local_vector_probe",
        "_route_resolver",
        "_retrieval_profile",
        "_retrieval_composition_valid",
        "_expected_scope",
        "_provider_name",
        "_provider_model",
        "_embedding_name",
        "_embedding_model",
        "_gemini_probe",
        "_catalog_probe",
        "_budget_probe",
        "_sealed",
    )

    def __init__(
        self,
        *,
        database_probe: Callable[[], object],
        corpus_probe: Callable[[], object],
        local_vector_probe: Callable[[], object],
        retrieval_route_resolver: RetrievalReadinessProbe,
        retrieval_profile: RetrievalReadinessProfile,
        expected_retrieval_scope: RetrievalScope | None,
        provider_name: Literal["echo", "ollama", "gemini"],
        provider_model: str,
        embedding_name: Literal["fake", "ollama"],
        embedding_model: str,
        gemini_probe: GeminiReadinessProbe | None,
        ollama_catalog_probe: OllamaCatalogReadinessProbe | None,
        budget_probe: BudgetReadinessProbe | None,
        retrieval_composition_valid: bool = True,
    ) -> None:
        if (
            type(retrieval_profile) is not str
            or retrieval_profile
            not in {"local_static", "firestore_static", "lifecycle_exact"}
            or type(provider_name) is not str
            or provider_name not in {"echo", "ollama", "gemini"}
            or type(provider_model) is not str
            or type(embedding_name) is not str
            or embedding_name not in {"fake", "ollama"}
            or type(embedding_model) is not str
            or type(retrieval_composition_valid) is not bool
        ):
            raise ReadinessError() from None
        object.__setattr__(self, "_database_probe", database_probe)
        object.__setattr__(self, "_corpus_probe", corpus_probe)
        object.__setattr__(self, "_local_vector_probe", local_vector_probe)
        object.__setattr__(self, "_route_resolver", retrieval_route_resolver)
        object.__setattr__(self, "_retrieval_profile", retrieval_profile)
        object.__setattr__(
            self, "_retrieval_composition_valid", retrieval_composition_valid
        )
        object.__setattr__(self, "_expected_scope", (
            None if expected_retrieval_scope is None else _copy_scope(expected_retrieval_scope)
        ))
        object.__setattr__(self, "_provider_name", provider_name)
        object.__setattr__(self, "_provider_model", provider_model)
        object.__setattr__(self, "_embedding_name", embedding_name)
        object.__setattr__(self, "_embedding_model", embedding_model)
        object.__setattr__(self, "_gemini_probe", gemini_probe)
        object.__setattr__(self, "_catalog_probe", ollama_catalog_probe)
        object.__setattr__(self, "_budget_probe", budget_probe)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise ReadinessError() from None

    def __delattr__(self, name: str) -> None:
        del name
        raise ReadinessError() from None

    @staticmethod
    def _sync_boolean(
        dimension: ReadinessDimension,
        operation: Callable[[], object],
    ) -> ReadinessCheck:
        try:
            value = operation()
        except Exception:
            return _check(dimension, "unknown", True, "unavailable")
        if type(value) is not bool:
            return _check(dimension, "unknown", True, "misconfigured")
        return _check(
            dimension,
            "ready" if value else "not_ready",
            True,
            "ready" if value else "unavailable",
        )

    async def _route_probe(self) -> tuple[RetrievalProbe | None, ReadinessReason | None]:
        raw, failure = await _call_readiness_probe(self._route_resolver)
        if failure is not None:
            return None, failure
        try:
            probe = _copy_retrieval_probe(raw)
        except ReadinessError:
            return None, "misconfigured"
        if self._expected_scope is not None and probe.scope != self._expected_scope:
            return None, "misconfigured"
        if self._retrieval_profile == "local_static" and type(probe.scope) is not LocalActiveScope:
            return None, "misconfigured"
        if (
            self._retrieval_profile != "local_static"
            and type(probe.scope) is not ExactCorpusReference
        ):
            return None, "misconfigured"
        return probe, None

    async def _gemini(self) -> tuple[GeminiReadiness | None, ReadinessReason | None]:
        if self._gemini_probe is None:
            return None, "misconfigured"
        raw, failure = await _call_readiness_probe(self._gemini_probe)
        if failure is not None:
            return None, failure
        try:
            return _copy_gemini(raw), None
        except ReadinessError:
            return None, "unavailable"

    async def _catalog(
        self,
    ) -> tuple[OllamaCatalogReadiness | None, ReadinessReason | None]:
        if self._catalog_probe is None:
            return None, "misconfigured"
        raw, failure = await _call_readiness_probe(self._catalog_probe)
        if failure is not None:
            return None, failure
        try:
            return _copy_catalog(raw), None
        except ReadinessError:
            return None, "unavailable"

    async def _budget(
        self,
    ) -> tuple[BudgetReadiness | None, ReadinessReason | None]:
        if self._budget_probe is None:
            return None, "misconfigured"
        raw, failure = await _call_readiness_probe(self._budget_probe)
        if failure is not None:
            return None, failure
        try:
            return _copy_budget(raw), None
        except ReadinessError:
            return None, "misconfigured"

    @staticmethod
    def _valid_model_name(value: str) -> bool:
        return type(value) is str and 1 <= len(value) <= OLLAMA_MODEL_NAME_MAX_CHARS

    async def evaluate(self) -> ReadinessReport:
        checks: list[ReadinessCheck] = []
        checks.append(self._sync_boolean("database", self._database_probe))

        if self._retrieval_composition_valid:
            route, route_failure = await self._route_probe()
        else:
            route, route_failure = None, "misconfigured"
        heartbeat_ok = True
        if self._retrieval_profile == "local_static":
            try:
                self._local_vector_probe()
            except Exception:
                heartbeat_ok = False
        if not self._retrieval_composition_valid:
            vector = _check("vector_store", "unknown", True, "misconfigured")
        elif route is None:
            vector = _check(
                "vector_store",
                "unknown",
                True,
                cast(ReadinessReason, route_failure),
            )
        elif not heartbeat_ok or not route.reachable:
            vector = _check("vector_store", "not_ready", True, "unreachable")
        elif not route.store_ready:
            vector = _check("vector_store", "not_ready", True, "store_unready")
        else:
            vector = _check("vector_store", "ready", True, "ready")
        checks.append(vector)

        checks.append(self._sync_boolean("corpus", self._corpus_probe))

        catalog: OllamaCatalogReadiness | None = None
        catalog_failure: ReadinessReason | None = None
        catalog_attempted = False
        provider_model_valid = (
            self._provider_name != "ollama"
            or self._valid_model_name(self._provider_model)
        )
        embedding_model_valid = (
            self._embedding_name != "ollama"
            or self._valid_model_name(self._embedding_model)
        )
        if self._provider_name == "ollama" and provider_model_valid:
            catalog_attempted = True
            catalog, catalog_failure = await self._catalog()
        if self._provider_name == "echo":
            provider = _check("provider", "ready", True, "ready")
            model = _check("model", "not_required", False, "not_required")
        elif self._provider_name == "gemini":
            gemini, gemini_failure = await self._gemini()
            if gemini is None:
                reason = cast(ReadinessReason, gemini_failure)
                provider = _check("provider", "unknown", True, reason)
                model = _check("model", "unknown", True, reason)
            elif not gemini.reachable:
                provider = _check("provider", "not_ready", True, "unreachable")
                model = _check("model", "unknown", True, "unavailable")
            else:
                provider = _check("provider", "ready", True, "ready")
                model = _check(
                    "model",
                    "ready" if gemini.model_ready else "not_ready",
                    True,
                    "ready" if gemini.model_ready else "model_missing",
                )
        else:
            if not provider_model_valid:
                provider = _check("provider", "unknown", True, "misconfigured")
                model = _check("model", "unknown", True, "misconfigured")
            elif catalog is None:
                reason = cast(ReadinessReason, catalog_failure)
                provider = _check("provider", "unknown", True, reason)
                model = _check("model", "unknown", True, reason)
            elif not catalog.reachable:
                provider = _check("provider", "not_ready", True, "unreachable")
                model = _check("model", "unknown", True, "unavailable")
            else:
                present = self._provider_model in catalog.model_names
                provider = _check("provider", "ready", True, "ready")
                model = _check(
                    "model",
                    "ready" if present else "not_ready",
                    True,
                    "ready" if present else "model_missing",
                )
        checks.extend((provider, model))

        if self._embedding_name == "fake":
            embedding = _check("embedding", "ready", True, "ready")
        else:
            if not embedding_model_valid:
                embedding = _check("embedding", "unknown", True, "misconfigured")
            else:
                if not catalog_attempted:
                    catalog, catalog_failure = await self._catalog()
                if catalog is None:
                    embedding = _check(
                        "embedding",
                        "unknown",
                        True,
                        cast(ReadinessReason, catalog_failure),
                    )
                elif not catalog.reachable:
                    embedding = _check("embedding", "not_ready", True, "unreachable")
                else:
                    present = self._embedding_model in catalog.model_names
                    embedding = _check(
                        "embedding",
                        "ready" if present else "not_ready",
                        True,
                        "ready" if present else "model_missing",
                    )
        checks.append(embedding)

        if self._retrieval_profile != "lifecycle_exact":
            exact = _check("exact_corpus", "not_required", False, "not_required")
        elif not self._retrieval_composition_valid:
            exact = _check("exact_corpus", "unknown", True, "misconfigured")
        elif route is None:
            exact = _check(
                "exact_corpus",
                "unknown",
                True,
                cast(ReadinessReason, route_failure),
            )
        elif not route.reachable or not route.store_ready:
            exact = _check("exact_corpus", "unknown", True, "unavailable")
        elif not route.exact_version_ready:
            exact = _check(
                "exact_corpus",
                "not_ready",
                True,
                "exact_corpus_unready",
            )
        else:
            exact = _check("exact_corpus", "ready", True, "ready")
        checks.append(exact)

        if self._budget_probe is None:
            budget = _check("budget", "not_required", False, "not_required")
        else:
            budget_result, budget_failure = await self._budget()
            if budget_result is None:
                budget = _check(
                    "budget",
                    "unknown",
                    True,
                    cast(ReadinessReason, budget_failure),
                )
            else:
                budget = _check(
                    "budget",
                    budget_result.state,
                    True,
                    budget_result.reason,
                )
        checks.append(budget)
        return ReadinessReport(checks=tuple(checks))


def public_readiness(report: object) -> tuple[bool, dict[str, bool]]:
    """Project one exact report to the frozen public readiness response facts."""

    def project() -> tuple[bool, dict[str, bool]]:
        if type(report) is not ReadinessReport:
            raise TypeError
        raw = object.__getattribute__(report, "__dict__")
        extra = object.__getattribute__(report, "__pydantic_extra__")
        private = object.__getattribute__(report, "__pydantic_private__")
        raw_fields_set = object.__getattribute__(report, "__pydantic_fields_set__")
        if (
            type(raw) is not dict
            or extra is not None
            or private is not None
            or len(raw) != len(ReadinessReport.model_fields)
        ):
            raise ValueError
        _copy_fields_set(raw_fields_set, ReadinessReport.model_fields)
        controlled: dict[str, object] = {}
        for key, item in dict.items(cast(dict[object, object], raw)):
            if type(key) is not str or key not in ReadinessReport.model_fields:
                raise ValueError
            controlled[key] = item
        if len(controlled) != len(ReadinessReport.model_fields):
            raise ValueError
        if (
            type(controlled["contract_version"]) is not str
            or controlled["contract_version"] != "1.0"
        ):
            raise ValueError
        if type(controlled["ready"]) is not bool:
            raise ValueError
        checks_value = controlled["checks"]
        if type(checks_value) is not tuple or len(checks_value) != len(_DIMENSION_ORDER):
            raise ValueError
        copied = tuple(_copy_check(item) for item in checks_value)
        if tuple(item.dimension for item in copied) != _DIMENSION_ORDER:
            raise ValueError
        by_dimension = {item.dimension: item for item in copied}
        exact = by_dimension["exact_corpus"]
        public_checks = {
            "database": by_dimension["database"].state == "ready",
            "vector_store": by_dimension["vector_store"].state == "ready",
            "corpus": by_dimension["corpus"].state == "ready"
            and exact.state in {"ready", "not_required"},
        }
        ready = all(not item.required or item.state == "ready" for item in copied)
        if controlled["ready"] is not ready:
            raise ValueError
        return ready, public_checks

    return _content_free(project)
