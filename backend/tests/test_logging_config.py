from __future__ import annotations

import json
import logging
import weakref
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from app.logging_config import (
    AppStartedLog,
    GeminiProviderStreamFailedLog,
    JSONFormatter,
    ProviderStreamFailedLog,
    RetrievalFailedLog,
    SafeLogError,
    SafeStreamHandler,
    StartupCorpusIngestedLog,
    TelemetryInputRejectedLog,
    TelemetrySinkFailedLog,
    configure_logging,
    emit_app_log,
    emit_gemini_provider_stream_failed,
    emit_retrieval_failed,
    emit_startup_corpus_ingested,
)


def _record(*, level: int = logging.INFO, event: object | None = None) -> logging.LogRecord:
    record = logging.LogRecord(
        "hostile-name", level, "/private/path", 12, "SECRET %s", ("VALUE",), None
    )
    if event is not None:
        record.__dict__["_cairn_event"] = event
    return record


def test_formatter_emits_only_typed_fields_in_frozen_order() -> None:
    formatter = JSONFormatter(utc_clock=lambda: datetime(2026, 9, 10, 1, 2, 3, 4, tzinfo=UTC))
    encoded = formatter.format(
        _record(event=StartupCorpusIngestedLog(document_count=2, chunk_count=5))
    )
    assert encoded == (
        '{"timestamp":"2026-09-10T01:02:03.000004Z","level":"info",'
        '"logger":"app","event":"startup_corpus_ingested","document_count":2,"chunk_count":5}'
    )
    assert "SECRET" not in encoded
    assert "/private/path" not in encoded


@pytest.mark.parametrize(
    ("level", "event", "expected_event"),
    [
        (logging.WARNING, RetrievalFailedLog("invalid_request"), "retrieval_failed"),
        (logging.ERROR, ProviderStreamFailedLog(), "provider_stream_failed"),
        (
            logging.ERROR,
            GeminiProviderStreamFailedLog("provider_timeout", True, 2),
            "gemini_provider_stream_failed",
        ),
        (logging.INFO, StartupCorpusIngestedLog(2, 5), "startup_corpus_ingested"),
        (logging.INFO, AppStartedLog(), "app_started"),
        (logging.ERROR, TelemetrySinkFailedLog(), "telemetry_sink_failed"),
        (logging.ERROR, TelemetryInputRejectedLog(), "telemetry_input_rejected"),
    ],
)
def test_every_typed_log_variant_is_bounded_and_deterministic(
    level: int, event: object, expected_event: str
) -> None:
    formatter = JSONFormatter(utc_clock=lambda: datetime(2026, 9, 10, tzinfo=UTC))
    encoded = formatter.format(_record(level=level, event=event))
    payload = json.loads(encoded)
    assert payload["logger"] == "app"
    assert payload["event"] == expected_event
    assert len(encoded.encode("utf-8")) <= 2048


def test_external_record_is_fixed_and_content_free() -> None:
    formatter = JSONFormatter(utc_clock=lambda: datetime(2026, 9, 10, tzinfo=UTC))
    record = _record(level=logging.ERROR)
    record.exc_info = (RuntimeError, RuntimeError("EXCEPTION-CANARY"), None)
    record.stack_info = "STACK-CANARY"
    record.__dict__["secret"] = object()
    payload = json.loads(formatter.format(record))
    assert payload == {
        "timestamp": "2026-09-10T00:00:00.000000Z",
        "level": "error",
        "logger": "external",
        "event": "external_log",
    }


@pytest.mark.parametrize(
    ("raw_level", "expected"),
    [(10, "debug"), (20, "info"), (30, "warning"), (40, "error"), (50, "critical"), (41, "info")],
)
def test_external_raw_level_mapping_is_exact(raw_level: int, expected: str) -> None:
    formatter = JSONFormatter(utc_clock=lambda: datetime(2026, 9, 10, tzinfo=UTC))
    assert json.loads(formatter.format(_record(level=raw_level)))["level"] == expected


def test_log_record_subclass_becomes_external_without_invoking_hooks() -> None:
    calls: list[str] = []

    class HostileRecord(logging.LogRecord):
        def __getattribute__(self, name: str) -> object:
            calls.append(name)
            raise AssertionError("record hook invoked")

    record = object.__new__(HostileRecord)
    formatter = JSONFormatter(utc_clock=lambda: datetime(2026, 9, 10, tzinfo=UTC))
    payload = json.loads(formatter.format(record))
    assert payload["event"] == "external_log"
    assert calls == []


def test_invalid_formatter_clock_and_broken_stream_are_silent() -> None:
    assert JSONFormatter(utc_clock=cast(Any, lambda: True)).format(_record()) == ""

    class BrokenStream:
        def write(self, value: str) -> None:
            del value
            raise OSError("STREAM-CANARY")

        def flush(self) -> None:
            raise OSError("STREAM-CANARY")

    handler = SafeStreamHandler(cast(Any, BrokenStream()))
    handler.setFormatter(JSONFormatter(utc_clock=lambda: datetime(2026, 9, 10, tzinfo=UTC)))
    handler.emit(_record())


def test_typed_log_variants_are_strict() -> None:
    assert RetrievalFailedLog(code="invalid_request").event == "retrieval_failed"
    assert AppStartedLog().event == "app_started"


@pytest.mark.parametrize(
    "factory",
    [
        lambda: cast(Any, RetrievalFailedLog)("invalid_request", event="PRIVATE-CANARY"),
        lambda: cast(Any, ProviderStreamFailedLog)(event="PRIVATE-CANARY"),
        lambda: cast(Any, GeminiProviderStreamFailedLog)(
            "provider_unavailable", False, 1, event="PRIVATE-CANARY"
        ),
        lambda: cast(Any, GeminiProviderStreamFailedLog)(
            "provider_unavailable", False, 1, provider="PRIVATE-CANARY"
        ),
        lambda: cast(Any, StartupCorpusIngestedLog)(1, 2, event="PRIVATE-CANARY"),
        lambda: cast(Any, AppStartedLog)(event="PRIVATE-CANARY"),
        lambda: cast(Any, TelemetrySinkFailedLog)(event="PRIVATE-CANARY"),
        lambda: cast(Any, TelemetryInputRejectedLog)(event="PRIVATE-CANARY"),
    ],
)
def test_direct_event_override_rejects_content_free(factory: Any) -> None:
    with pytest.raises(SafeLogError) as caught:
        factory()
    assert str(caught.value) == "Application log input is invalid."
    assert repr(caught.value) == "SafeLogError()"
    assert "PRIVATE-CANARY" not in str(caught.value)


def test_invalid_constructor_does_not_retain_rejected_object() -> None:
    class Canary:
        pass

    rejected = Canary()
    reference = weakref.ref(rejected)

    def capture_error(value: object) -> SafeLogError:
        try:
            RetrievalFailedLog(value)  # type: ignore[arg-type]
        except SafeLogError as error:
            del value
            return error
        raise AssertionError("invalid log event was accepted")

    error = capture_error(rejected)
    del rejected
    assert reference() is None
    assert error.__traceback__ is None


def test_emit_failure_does_not_retain_mutated_event_value() -> None:
    class Canary:
        pass

    rejected = Canary()
    reference = weakref.ref(rejected)
    event = StartupCorpusIngestedLog(1, 2)
    object.__setattr__(event, "document_count", rejected)

    def capture_error(value: object) -> SafeLogError:
        try:
            emit_app_log(value)  # type: ignore[arg-type]
        except SafeLogError as error:
            del value
            return error
        raise AssertionError("invalid log event was accepted")

    error = capture_error(event)
    del event, rejected
    assert reference() is None
    assert error.__traceback__ is None


@pytest.mark.parametrize(
    ("event", "field", "forged"),
    [
        (RetrievalFailedLog("invalid_request"), "code", "PRIVATE-CANARY"),
        (ProviderStreamFailedLog(), "event", "PRIVATE-CANARY"),
        (
            GeminiProviderStreamFailedLog("provider_unavailable", False, 1),
            "provider",
            "PRIVATE-CANARY",
        ),
        (
            GeminiProviderStreamFailedLog("provider_unavailable", False, 1),
            "retryable",
            1,
        ),
        (StartupCorpusIngestedLog(1, 2), "document_count", True),
        (AppStartedLog(), "event", "PRIVATE-CANARY"),
        (TelemetrySinkFailedLog(), "event", "PRIVATE-CANARY"),
        (TelemetryInputRejectedLog(), "event", "PRIVATE-CANARY"),
    ],
)
def test_mutated_exact_events_are_rejected_before_logging(
    event: object, field: str, forged: object
) -> None:
    object.__setattr__(event, field, forged)
    with pytest.raises(SafeLogError) as caught:
        emit_app_log(event)  # type: ignore[arg-type]
    assert str(caught.value) == "Application log input is invalid."
    assert "PRIVATE-CANARY" not in str(caught.value)


def test_formatter_treats_mutated_typed_event_as_external_content_free() -> None:
    formatter = JSONFormatter(utc_clock=lambda: datetime(2026, 9, 10, tzinfo=UTC))
    event = GeminiProviderStreamFailedLog("provider_unavailable", False, 1)
    object.__setattr__(event, "code", "PRIVATE-CANARY")
    encoded = formatter.format(_record(level=logging.ERROR, event=event))
    assert json.loads(encoded) == {
        "timestamp": "2026-09-10T00:00:00.000000Z",
        "level": "error",
        "logger": "external",
        "event": "external_log",
    }
    assert "PRIVATE-CANARY" not in encoded


def test_configure_logging_preserves_default_and_explicit_filter_levels() -> None:
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    try:
        configure_logging()
        assert root.level == logging.INFO
        configure_logging(logging.ERROR)
        assert root.level == logging.ERROR
        with pytest.raises(SafeLogError):
            configure_logging(True)
    finally:
        root.handlers = original_handlers
        root.setLevel(original_level)


def test_emit_attaches_fresh_validated_event_copy() -> None:
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    try:
        root.handlers = [Capture()]
        root.setLevel(logging.DEBUG)
        original = RetrievalFailedLog("invalid_request")
        emit_app_log(original)
        copied = records[0].__dict__["_cairn_event"]
        assert type(copied) is RetrievalFailedLog
        assert copied == original
        assert copied is not original
    finally:
        root.handlers = original_handlers
        root.setLevel(original_level)


def test_malformed_call_site_fields_are_advisory_and_never_logged() -> None:
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    try:
        root.handlers = [Capture()]
        root.setLevel(logging.DEBUG)
        emit_retrieval_failed(object())
        emit_gemini_provider_stream_failed("provider_unavailable", False, 3)
        emit_startup_corpus_ingested(True, object())
        assert records == []
    finally:
        root.handlers = original_handlers
        root.setLevel(original_level)
