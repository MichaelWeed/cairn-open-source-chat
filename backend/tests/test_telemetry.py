from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
from pydantic import TypeAdapter

from app.config import Settings
from app.main import create_app
from app.providers.accounting import (
    ProviderAttemptAccountingInput,
    ProviderAttemptCostRecord,
    price_provider_attempt,
)
from app.providers.contracts import MAX_SAFE_TOKEN_COUNT, ProviderUsage
from app.readiness import ReadinessCheck, ReadinessReport
from app.request_accounting import (
    AttemptIdentity,
    RequestAccountingSummary,
    SettledAttempt,
    UncertainAttempt,
)
from app.telemetry import (
    MAX_TELEMETRY_DURATION_NS,
    ChatDurationSeconds,
    ChatOutcomeTotal,
    ChatRequestsTotal,
    ChatTelemetryUnit,
    ExactTelemetryEvent,
    NullTelemetrySink,
    ProviderReportedTotalTokens,
    ProviderUsageRecordsTotal,
    ProviderUsageTokensTotal,
    ReadinessChecksTotal,
    ReadinessDurationSeconds,
    TelemetryError,
    TelemetryProjector,
    deliver_telemetry,
)


def _projector() -> TelemetryProjector:
    app = create_app(Settings(provider="ollama", ollama_model="llama3.2"))
    return cast(TelemetryProjector, app.state.telemetry_projector)


def _settled_summary(
    *,
    completion: str = "completed",
    cost_state: str = "snapshot_missing",
    usage: ProviderUsage | None = None,
) -> RequestAccountingSummary:
    identity = AttemptIdentity(provider="ollama", model="llama3.2", provider_attempt=1)
    record = price_provider_attempt(
        ProviderAttemptAccountingInput(
            provider="ollama",
            model="llama3.2",
            provider_attempt=1,
            attempt_date=datetime(2026, 9, 10, tzinfo=UTC).date(),
            completion_state="completed",
            answer_outcome="grounded",
            usage=usage,
        )
    )
    assert record.cost_state == cost_state
    return RequestAccountingSummary(
        provider_work_started=True,
        request_completion=cast(Any, completion),
        attempts=(SettledAttempt(identity=identity, cost_record=record),),
    )


def test_all_exact_event_variants_round_trip() -> None:
    events: tuple[ExactTelemetryEvent, ...] = (
        ChatRequestsTotal(),
        ChatOutcomeTotal(chat_outcome="grounded"),
        ChatDurationSeconds(chat_outcome="grounded", value=Decimal("1.25")),
        ProviderUsageRecordsTotal(
            provider="ollama", model="llama3.2", provider_attempt=1, usage_state="complete"
        ),
        ProviderUsageTokensTotal(
            provider="ollama",
            model="llama3.2",
            provider_attempt=1,
            token_kind="input",
            delta=4,
        ),
        ProviderReportedTotalTokens(
            provider="ollama", model="llama3.2", provider_attempt=1, value=4
        ),
        ReadinessChecksTotal(
            readiness_dimension="database",
            readiness_state="ready",
            readiness_reason="ready",
            required=True,
        ),
        ReadinessDurationSeconds(value=Decimal("0")),
    )
    adapter: TypeAdapter[Any] = TypeAdapter(ExactTelemetryEvent)
    for event in events:
        assert adapter.validate_python(adapter.dump_python(event)) == event
        assert adapter.validate_json(adapter.dump_json(event)) == event


def test_union_invalid_discriminator_is_fixed_and_content_free() -> None:
    adapter: TypeAdapter[Any] = TypeAdapter(ExactTelemetryEvent)
    operations: tuple[Callable[[], object], ...] = (
        lambda: adapter.validate_python({"metric": "PRIVATE-CANARY"}),
        lambda: adapter.validate_json('{"metric":"PRIVATE-CANARY"}'),
    )
    for operation in operations:
        with pytest.raises(TelemetryError) as caught:
            operation()
        assert str(caught.value) == "Telemetry input is invalid."
        assert repr(caught.value) == "TelemetryError()"
        assert "PRIVATE-CANARY" not in str(caught.value)
        assert caught.value.errors() == [
            {"type": "telemetry_error", "loc": (), "msg": "Telemetry input is invalid."}
        ]


def test_union_rejects_forged_exact_event_metric_on_validate_and_dump() -> None:
    adapter: TypeAdapter[Any] = TypeAdapter(ExactTelemetryEvent)
    event = ChatRequestsTotal()
    object.__setattr__(event, "metric", "PRIVATE-CANARY")
    with pytest.raises(TelemetryError):
        adapter.validate_python(event)
    with pytest.raises(Exception) as caught:
        adapter.dump_python(event)
    assert "PRIVATE-CANARY" not in str(caught.value)


def test_union_serializer_rejects_raw_mapping_without_echoing_it() -> None:
    adapter: TypeAdapter[Any] = TypeAdapter(ExactTelemetryEvent)
    raw = {"metric": "PRIVATE-CANARY"}
    for operation in (lambda: adapter.dump_python(raw), lambda: adapter.dump_json(raw)):
        with pytest.raises(Exception) as caught:
            operation()
        assert "PRIVATE-CANARY" not in str(caught.value)


def test_projector_rechecks_app_binding_after_object_level_mutation() -> None:
    projector = _projector()
    object.__setattr__(projector, "_provider", "gemini")
    object.__setattr__(projector, "_model", "gemini-3.8-flash")
    with pytest.raises(TelemetryError):
        projector.project_chat_start()


@pytest.mark.parametrize(
    "factory",
    [
        lambda: ChatRequestsTotal(delta=0),
        lambda: ChatRequestsTotal(delta=True),
        lambda: ProviderUsageTokensTotal(
            provider="ollama",
            model="llama3.2",
            provider_attempt=1,
            token_kind="total",
            delta=MAX_SAFE_TOKEN_COUNT + 1,
        ),
        lambda: ChatDurationSeconds(chat_outcome="grounded", value=Decimal("NaN")),
        lambda: ChatDurationSeconds(chat_outcome="grounded", value=Decimal("0.0000000001")),
        lambda: ReadinessDurationSeconds(value=Decimal("-1")),
    ],
)
def test_invalid_event_values_fail_content_free(factory: Any) -> None:
    with pytest.raises(TelemetryError) as caught:
        factory()
    assert str(caught.value) == "Telemetry input is invalid."
    assert repr(caught.value) == "TelemetryError()"
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


def test_event_model_bypass_surfaces_fail_closed() -> None:
    event = ChatRequestsTotal()
    operations = (
        lambda: ChatRequestsTotal.model_construct(delta=0),
        lambda: ChatRequestsTotal.construct(delta=0),
        lambda: event.model_copy(update={"delta": 0}),
        lambda: event.copy(update={"delta": 0}),
        lambda: event.__replace__(delta=0),
        lambda: setattr(event, "delta", 2),
    )
    for operation in operations:
        with pytest.raises(TelemetryError):
            operation()


def test_mutated_model_internal_state_is_rejected_before_dump() -> None:
    event = ChatRequestsTotal()
    object.__setattr__(event, "__pydantic_extra__", {"PRIVATE-CANARY": object()})
    with pytest.raises(Exception) as caught:
        event.model_dump()
    assert "PRIVATE-CANARY" not in str(caught.value)


@pytest.mark.parametrize(
    "encoded",
    [
        '{"metric":"cairn_readiness_duration_seconds","value":"1E-100000"}',
        '{"metric":"cairn_readiness_duration_seconds","value":"0.'
        + "0" * 1000
        + '1"}',
    ],
)
def test_duration_json_is_bounded_before_decimal_conversion(encoded: str) -> None:
    with pytest.raises(TelemetryError):
        TypeAdapter(ExactTelemetryEvent).validate_json(encoded)


def test_chat_projection_order_zero_omission_and_duration_bound() -> None:
    usage = ProviderUsage(
        input_tokens=3,
        cached_input_tokens=0,
        output_tokens=2,
        thinking_tokens=0,
        total_tokens=5,
    )
    summary = _settled_summary(usage=usage)
    events = _projector().project_chat_completion(summary, 1_500_000_000)
    assert [event.metric for event in events] == [
        "cairn_chat_outcomes_total",
        "cairn_chat_duration_seconds",
        "cairn_provider_usage_records_total",
        "cairn_provider_usage_tokens_total",
        "cairn_provider_usage_tokens_total",
        "cairn_provider_usage_tokens_total",
        "cairn_provider_reported_total_tokens",
    ]
    token_events = [event for event in events if type(event) is ProviderUsageTokensTotal]
    assert [(event.token_kind, event.delta) for event in token_events] == [
        ("input", 3),
        ("output", 2),
        ("total", 5),
    ]
    assert events[1].value == Decimal("1.5")  # type: ignore[union-attr]
    assert len(_projector().project_chat_completion(summary, MAX_TELEMETRY_DURATION_NS + 1)) == 6


def test_chat_projection_reaches_exact_sixteen_event_completion_bound() -> None:
    usage = ProviderUsage(
        input_tokens=1,
        cached_input_tokens=1,
        output_tokens=1,
        thinking_tokens=1,
        total_tokens=3,
    )

    def record(attempt: int, completion: str) -> ProviderAttemptCostRecord:
        return ProviderAttemptCostRecord(
            provider="ollama",
            model="llama3.2",
            provider_attempt=attempt,
            attempt_date=datetime(2026, 9, 10, tzinfo=UTC).date(),
            completion_state=cast(Any, completion),
            answer_outcome="grounded" if completion == "completed" else "unverified",
            usage=usage,
            snapshot_id=None,
            currency=None,
            model_cost=None,
            cost_state="snapshot_missing",
        )

    summary = RequestAccountingSummary(
        provider_work_started=True,
        request_completion="completed",
        attempts=(
            SettledAttempt(
                identity=AttemptIdentity(
                    provider="ollama", model="llama3.2", provider_attempt=1
                ),
                cost_record=record(1, "error"),
            ),
            SettledAttempt(
                identity=AttemptIdentity(
                    provider="ollama", model="llama3.2", provider_attempt=2
                ),
                cost_record=record(2, "completed"),
            ),
        ),
    )
    events = _projector().project_chat_completion(summary, 0)
    assert len(events) == 16
    assert [event.provider_attempt for event in events[2:9]] == [1] * 7  # type: ignore[union-attr]
    assert [event.provider_attempt for event in events[9:]] == [2] * 7  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("completion", "outcome"),
    [
        ("completed", "grounded"),
        ("refused", "refused"),
        ("limit", "limit"),
        ("error", "error"),
        ("abandoned", "error"),
        ("cancelled", "cancelled"),
    ],
)
def test_empty_summary_maps_only_fixed_outcome(
    completion: str, outcome: str
) -> None:
    summary = RequestAccountingSummary(
        provider_work_started=False,
        request_completion=cast(Any, completion),
    )
    events = _projector().project_chat_completion(summary, None)
    assert events == (ChatOutcomeTotal(chat_outcome=cast(Any, outcome)),)


def test_uncertain_attempt_emits_record_only() -> None:
    summary = RequestAccountingSummary(
        provider_work_started=True,
        request_completion="abandoned",
        attempts=(
            UncertainAttempt(
                identity=AttemptIdentity(provider="ollama", model="llama3.2", provider_attempt=1),
                last_usage=ProviderUsage(input_tokens=5),
                reason="cleanup_uncertain",
            ),
        ),
    )
    events = _projector().project_chat_completion(summary, None)
    assert [event.metric for event in events] == [
        "cairn_chat_outcomes_total",
        "cairn_provider_usage_records_total",
    ]
    assert cast(ProviderUsageRecordsTotal, events[1]).usage_state == "uncertain"


def test_projector_rejects_forged_identity_before_events() -> None:
    summary = RequestAccountingSummary(
        provider_work_started=True,
        request_completion="error",
        attempts=(
            UncertainAttempt(
                identity=AttemptIdentity(
                    provider="gemini", model="gemini-3.8-flash", provider_attempt=1
                ),
                reason="finish_missing",
            ),
        ),
    )
    with pytest.raises(TelemetryError):
        _projector().project_chat_completion(summary, 0)


def test_readiness_projection_is_canonical_and_bounded() -> None:
    dimensions = (
        "database",
        "vector_store",
        "corpus",
        "provider",
        "model",
        "embedding",
        "exact_corpus",
        "budget",
    )
    checks = tuple(
        ReadinessCheck(
            dimension=cast(Any, dimension),
            state="not_required" if dimension == "budget" else "ready",
            required=dimension != "budget",
            reason="not_required" if dimension == "budget" else "ready",
        )
        for dimension in dimensions
    )
    events = _projector().project_readiness(ReadinessReport(checks=checks), 0)
    assert len(events) == 9
    assert [cast(ReadinessChecksTotal, event).readiness_dimension for event in events[:-1]] == list(
        dimensions
    )
    assert type(events[-1]) is ReadinessDurationSeconds


def test_sink_failure_fuses_without_retry_and_base_exception_propagates() -> None:
    class Sink:
        def __init__(self, failure: BaseException) -> None:
            self.failure = failure
            self.calls = 0

        def emit(self, event: ExactTelemetryEvent) -> None:
            del event
            self.calls += 1
            raise self.failure

    ordinary = Sink(RuntimeError("SINK-CANARY"))
    assert deliver_telemetry(ordinary, (ChatRequestsTotal(), ChatRequestsTotal())) is False
    assert ordinary.calls == 1

    cancellation = asyncio.CancelledError("cancel")
    cancelling = Sink(cancellation)
    with pytest.raises(asyncio.CancelledError) as caught:
        deliver_telemetry(cancelling, (ChatRequestsTotal(),))
    assert caught.value is cancellation


@pytest.mark.parametrize(
    "failure",
    [GeneratorExit("exit"), KeyboardInterrupt("interrupt"), SystemExit("exit")],
)
def test_every_nonordinary_sink_failure_propagates_unchanged(failure: BaseException) -> None:
    class Sink:
        def emit(self, event: ExactTelemetryEvent) -> None:
            del event
            raise failure

    with pytest.raises(type(failure)) as caught:
        deliver_telemetry(Sink(), (ChatRequestsTotal(),))
    assert caught.value is failure


def test_chat_unit_consume_then_raise_fuses_all_later_delivery() -> None:
    class Sink:
        def __init__(self) -> None:
            self.events: list[ExactTelemetryEvent] = []

        def emit(self, event: ExactTelemetryEvent) -> None:
            self.events.append(event)
            raise RuntimeError("SINK-CANARY")

    sink = Sink()
    ticks = iter((0, 1))
    unit = ChatTelemetryUnit(_projector(), sink, ticks.__next__)
    unit.complete(
        RequestAccountingSummary(provider_work_started=False, request_completion="refused")
    )
    assert len(sink.events) == 1
    assert type(sink.events[0]) is ChatRequestsTotal


@pytest.mark.parametrize("end", [-1, True, MAX_TELEMETRY_DURATION_NS + 1])
def test_invalid_end_clock_omits_duration_only(end: object) -> None:
    class Sink:
        def __init__(self) -> None:
            self.events: list[ExactTelemetryEvent] = []

        def emit(self, event: ExactTelemetryEvent) -> None:
            self.events.append(event)

    sink = Sink()
    ticks = iter((0, end))
    unit = ChatTelemetryUnit(_projector(), sink, ticks.__next__)  # type: ignore[arg-type]
    unit.complete(
        RequestAccountingSummary(provider_work_started=False, request_completion="refused")
    )
    assert [type(event) for event in sink.events] == [ChatRequestsTotal, ChatOutcomeTotal]


def test_interleaved_units_keep_clock_and_sink_state_isolated() -> None:
    class Sink:
        def __init__(self) -> None:
            self.events: list[ExactTelemetryEvent] = []

        def emit(self, event: ExactTelemetryEvent) -> None:
            self.events.append(event)

    left, right = Sink(), Sink()
    left_unit = ChatTelemetryUnit(_projector(), left, iter((10, 12)).__next__)
    right_unit = ChatTelemetryUnit(_projector(), right, iter((100, 105)).__next__)
    summary = RequestAccountingSummary(
        provider_work_started=False, request_completion="cancelled"
    )
    right_unit.complete(summary)
    left_unit.complete(summary)
    assert [event.value for event in left.events if type(event) is ChatDurationSeconds] == [
        Decimal("0.000000002")
    ]
    assert [event.value for event in right.events if type(event) is ChatDurationSeconds] == [
        Decimal("0.000000005")
    ]


def test_null_sink_is_exact_and_stateless() -> None:
    sink = NullTelemetrySink()
    sink.emit(ChatRequestsTotal())
    assert not hasattr(sink, "__dict__")
