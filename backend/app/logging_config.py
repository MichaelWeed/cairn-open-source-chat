"""Mechanical, content-free JSON application logging."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, TextIO, cast

from app.api.contracts import ErrorCode
from app.providers.contracts import MAX_SAFE_TOKEN_COUNT
from app.retrieval_contracts import RetrievalErrorCode

MAX_SAFE_LOG_BYTES = 2048
_APP_EVENT_KEY = "_cairn_event"
_FAILED = object()


class SafeLogError(ValueError):
    def __init__(self) -> None:
        super().__init__("Application log input is invalid.")

    def __repr__(self) -> str:
        return "SafeLogError()"

    def __getattribute__(self, name: str) -> object:
        if name in {"__traceback__", "__cause__", "__context__"}:
            return None
        return super().__getattribute__(name)


class _SafeLogMeta(type):
    def __call__(cls, *args: object, **kwargs: object) -> Any:
        result: object = _FAILED
        try:
            result = super().__call__(*args, **kwargs)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            pass
        if result is _FAILED:
            del args, kwargs, result
            raise SafeLogError from None
        return result


class _SafeLogEvent(metaclass=_SafeLogMeta):
    __slots__ = ()


@dataclass(frozen=True, slots=True)
class RetrievalFailedLog(_SafeLogEvent):
    code: RetrievalErrorCode
    event: Literal["retrieval_failed"] = "retrieval_failed"

    def __post_init__(self) -> None:
        if (
            type(self.code) is not str
            or self.code
            not in {
                "invalid_request",
                "unsupported_scope",
                "store_unavailable",
                "malformed_result",
                "context_too_large",
            }
            or type(self.event) is not str
            or self.event != "retrieval_failed"
        ):
            raise SafeLogError from None


@dataclass(frozen=True, slots=True)
class ProviderStreamFailedLog(_SafeLogEvent):
    event: Literal["provider_stream_failed"] = "provider_stream_failed"

    def __post_init__(self) -> None:
        if type(self.event) is not str or self.event != "provider_stream_failed":
            raise SafeLogError from None


@dataclass(frozen=True, slots=True)
class GeminiProviderStreamFailedLog(_SafeLogEvent):
    code: ErrorCode
    retryable: bool
    attempt: int
    provider: Literal["gemini"] = "gemini"
    event: Literal["gemini_provider_stream_failed"] = "gemini_provider_stream_failed"

    def __post_init__(self) -> None:
        if (
            type(self.code) is not str
            or self.code
            not in {
                "invalid_request",
                "rate_limited",
                "budget_exhausted",
                "concurrency_limited",
                "provider_timeout",
                "provider_unavailable",
                "retrieval_unavailable",
                "guardrail_block",
                "request_cancelled",
                "internal",
            }
            or type(self.retryable) is not bool
            or type(self.attempt) is not int
            or self.attempt not in {1, 2}
            or type(self.provider) is not str
            or self.provider != "gemini"
            or type(self.event) is not str
            or self.event != "gemini_provider_stream_failed"
        ):
            raise SafeLogError from None


@dataclass(frozen=True, slots=True)
class StartupCorpusIngestedLog(_SafeLogEvent):
    document_count: int
    chunk_count: int
    event: Literal["startup_corpus_ingested"] = "startup_corpus_ingested"

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or not 0 <= value <= MAX_SAFE_TOKEN_COUNT
            for value in (self.document_count, self.chunk_count)
        ) or type(self.event) is not str or self.event != "startup_corpus_ingested":
            raise SafeLogError from None


@dataclass(frozen=True, slots=True)
class AppStartedLog(_SafeLogEvent):
    event: Literal["app_started"] = "app_started"

    def __post_init__(self) -> None:
        if type(self.event) is not str or self.event != "app_started":
            raise SafeLogError from None


@dataclass(frozen=True, slots=True)
class TelemetrySinkFailedLog(_SafeLogEvent):
    event: Literal["telemetry_sink_failed"] = "telemetry_sink_failed"

    def __post_init__(self) -> None:
        if type(self.event) is not str or self.event != "telemetry_sink_failed":
            raise SafeLogError from None


@dataclass(frozen=True, slots=True)
class TelemetryInputRejectedLog(_SafeLogEvent):
    event: Literal["telemetry_input_rejected"] = "telemetry_input_rejected"

    def __post_init__(self) -> None:
        if type(self.event) is not str or self.event != "telemetry_input_rejected":
            raise SafeLogError from None


AppLogEvent = (
    RetrievalFailedLog
    | ProviderStreamFailedLog
    | GeminiProviderStreamFailedLog
    | StartupCorpusIngestedLog
    | AppStartedLog
    | TelemetrySinkFailedLog
    | TelemetryInputRejectedLog
)

_EVENT_LEVELS: dict[type[object], tuple[int, str]] = {
    RetrievalFailedLog: (logging.WARNING, "warning"),
    ProviderStreamFailedLog: (logging.ERROR, "error"),
    GeminiProviderStreamFailedLog: (logging.ERROR, "error"),
    StartupCorpusIngestedLog: (logging.INFO, "info"),
    AppStartedLog: (logging.INFO, "info"),
    TelemetrySinkFailedLog: (logging.ERROR, "error"),
    TelemetryInputRejectedLog: (logging.ERROR, "error"),
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _slot(value: object, name: str) -> object:
    try:
        return object.__getattribute__(value, name)
    except BaseException:
        raise SafeLogError from None


def _copy_log_event(value: object) -> AppLogEvent | None:
    event_type = type(value)
    if event_type not in _EVENT_LEVELS:
        return None
    if event_type is RetrievalFailedLog:
        code = _slot(value, "code")
        event = _slot(value, "event")
        if type(code) is not str or code not in {
            "invalid_request",
            "unsupported_scope",
            "store_unavailable",
            "malformed_result",
            "context_too_large",
        } or type(event) is not str or event != "retrieval_failed":
            raise SafeLogError from None
        return RetrievalFailedLog(cast(RetrievalErrorCode, code))
    if event_type is ProviderStreamFailedLog:
        event = _slot(value, "event")
        if type(event) is not str or event != "provider_stream_failed":
            raise SafeLogError from None
        return ProviderStreamFailedLog()
    if event_type is GeminiProviderStreamFailedLog:
        code = _slot(value, "code")
        retryable = _slot(value, "retryable")
        attempt = _slot(value, "attempt")
        provider = _slot(value, "provider")
        event = _slot(value, "event")
        if (
            type(code) is not str
            or code not in {
                "invalid_request",
                "rate_limited",
                "budget_exhausted",
                "concurrency_limited",
                "provider_timeout",
                "provider_unavailable",
                "retrieval_unavailable",
                "guardrail_block",
                "request_cancelled",
                "internal",
            }
            or type(retryable) is not bool
            or type(attempt) is not int
            or attempt not in {1, 2}
            or type(provider) is not str
            or provider != "gemini"
            or type(event) is not str
            or event != "gemini_provider_stream_failed"
        ):
            raise SafeLogError from None
        return GeminiProviderStreamFailedLog(cast(ErrorCode, code), retryable, attempt)
    if event_type is StartupCorpusIngestedLog:
        document_count = _slot(value, "document_count")
        chunk_count = _slot(value, "chunk_count")
        event = _slot(value, "event")
        if (
            type(document_count) is not int
            or not 0 <= document_count <= MAX_SAFE_TOKEN_COUNT
            or type(chunk_count) is not int
            or not 0 <= chunk_count <= MAX_SAFE_TOKEN_COUNT
            or type(event) is not str
            or event != "startup_corpus_ingested"
        ):
            raise SafeLogError from None
        return StartupCorpusIngestedLog(document_count, chunk_count)
    if event_type is AppStartedLog:
        expected = "app_started"
    elif event_type is TelemetrySinkFailedLog:
        expected = "telemetry_sink_failed"
    else:
        expected = "telemetry_input_rejected"
    event = _slot(value, "event")
    if type(event) is not str or event != expected:
        raise SafeLogError from None
    return cast(AppLogEvent, event_type())


def _event_payload(value: object) -> tuple[str, str, dict[str, object]] | None:
    copied = _copy_log_event(value)
    if copied is None:
        return None
    event_type = type(copied)
    level = _EVENT_LEVELS.get(event_type)
    assert level is not None
    if event_type is RetrievalFailedLog:
        typed_retrieval = cast(RetrievalFailedLog, copied)
        return level[1], typed_retrieval.event, {"code": typed_retrieval.code}
    if event_type is ProviderStreamFailedLog:
        return (
            level[1],
            cast(ProviderStreamFailedLog, copied).event,
            {"code": "provider_unavailable"},
        )
    if event_type is GeminiProviderStreamFailedLog:
        typed_gemini = cast(GeminiProviderStreamFailedLog, copied)
        return (
            level[1],
            typed_gemini.event,
            {
                "provider": typed_gemini.provider,
                "code": typed_gemini.code,
                "retryable": typed_gemini.retryable,
                "attempt": typed_gemini.attempt,
            },
        )
    if event_type is StartupCorpusIngestedLog:
        typed_startup = cast(StartupCorpusIngestedLog, copied)
        return (
            level[1],
            typed_startup.event,
            {
                "document_count": typed_startup.document_count,
                "chunk_count": typed_startup.chunk_count,
            },
        )
    return (
        level[1],
        cast(AppStartedLog | TelemetrySinkFailedLog | TelemetryInputRejectedLog, copied).event,
        {},
    )


class JSONFormatter(logging.Formatter):
    """Format only exact Cairn events or a fixed external-record placeholder."""

    def __init__(self, *, utc_clock: Callable[[], datetime] = _utc_now) -> None:
        super().__init__()
        self._utc_clock = utc_clock

    def format(self, record: logging.LogRecord) -> str:
        try:
            timestamp = self._utc_clock()
            if type(timestamp) is not datetime or timestamp.tzinfo is not UTC:
                return ""
            timestamp_text = timestamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            level = "info"
            logger_name = "external"
            event_name = "external_log"
            fields: dict[str, object] = {}
            if type(record) is logging.LogRecord:
                raw = object.__getattribute__(record, "__dict__")
                if type(raw) is dict and len(raw) <= 64:
                    items = tuple(dict.items(raw))
                    if all(type(key) is str for key, _ in items):
                        controlled = {key: value for key, value in items}
                        raw_level = dict.get(controlled, "levelno")
                        if type(raw_level) is int:
                            level = {
                                10: "debug",
                                20: "info",
                                30: "warning",
                                40: "error",
                                50: "critical",
                            }.get(raw_level, "info")
                        raw_event = dict.get(controlled, _APP_EVENT_KEY)
                        try:
                            typed = _event_payload(raw_event)
                        except BaseException:
                            typed = None
                        if typed is not None and raw_level == _EVENT_LEVELS[type(raw_event)][0]:
                            level, event_name, fields = typed
                            logger_name = "app"
            payload: dict[str, object] = {
                "timestamp": timestamp_text,
                "level": level,
                "logger": logger_name,
                "event": event_name,
            }
            payload.update(fields)
            encoded = json.dumps(
                payload,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            )
            return encoded if len(encoded.encode("utf-8")) <= MAX_SAFE_LOG_BYTES else ""
        except BaseException:
            return ""


class SafeStreamHandler(logging.StreamHandler[TextIO]):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            encoded = self.format(record)
            if encoded:
                self.stream.write(encoded + self.terminator)
                self.flush()
        except BaseException:
            return

    def handleError(self, record: logging.LogRecord) -> None:
        del record


def emit_app_log(event: AppLogEvent) -> None:
    copied: object = _FAILED
    try:
        copied = _copy_log_event(event)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        pass
    if copied is _FAILED or copied is None:
        del event, copied
        raise SafeLogError from None
    safe_event = cast(AppLogEvent, copied)
    level = _EVENT_LEVELS[type(safe_event)]
    try:
        logging.getLogger("app").log(level[0], "", extra={_APP_EVENT_KEY: safe_event})
    except BaseException:
        return


def emit_retrieval_failed(code: object) -> None:
    try:
        emit_app_log(RetrievalFailedLog(cast(RetrievalErrorCode, code)))
    except Exception:
        return


def emit_gemini_provider_stream_failed(
    code: object, retryable: object, attempt: object
) -> None:
    try:
        emit_app_log(
            GeminiProviderStreamFailedLog(
                cast(ErrorCode, code),
                cast(bool, retryable),
                cast(int, attempt),
            )
        )
    except Exception:
        return


def emit_startup_corpus_ingested(document_count: object, chunk_count: object) -> None:
    try:
        emit_app_log(
            StartupCorpusIngestedLog(
                cast(int, document_count),
                cast(int, chunk_count),
            )
        )
    except Exception:
        return


def configure_logging(
    level: int = logging.INFO,
    *,
    utc_clock: Callable[[], datetime] | None = None,
) -> None:
    if type(level) is not int or level not in {
        logging.DEBUG,
        logging.INFO,
        logging.WARNING,
        logging.ERROR,
        logging.CRITICAL,
    }:
        raise SafeLogError from None
    handler = SafeStreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter(utc_clock=_utc_now if utc_clock is None else utc_clock))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
