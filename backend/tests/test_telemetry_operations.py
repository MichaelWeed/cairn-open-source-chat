from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter
from test_chat_endpoint import _ingest
from test_provider_accounting import _attempt, _snapshot

from app.config import Settings
from app.main import create_app
from app.providers.accounting import price_provider_attempt
from app.request_accounting import (
    AttemptIdentity,
    RequestAccountingSummary,
    SettledAttempt,
    UncertainAttempt,
)
from app.telemetry import (
    ChatConcurrency,
    ChatConcurrencyTracker,
    ChatFirstTokenSeconds,
    ChatTelemetryUnit,
    CorpusVersionState,
    ExactTelemetryEvent,
    HostLifecycleSignal,
    HostLifecycleTelemetry,
    NullTelemetrySink,
    ProviderCostObservation,
    TelemetryError,
)


class Sink:
    def __init__(self) -> None:
        self.events: list[ExactTelemetryEvent] = []

    def emit(self, event: ExactTelemetryEvent) -> None:
        self.events.append(event)


def test_operational_events_roundtrip_without_content() -> None:
    adapter: TypeAdapter[Any] = TypeAdapter(ExactTelemetryEvent)
    for event in (
        ChatConcurrency(value=2),
        ChatFirstTokenSeconds(value=Decimal("0.125")),
        CorpusVersionState(corpus_state="exact_version"),
        HostLifecycleSignal(signal="resolved"),
        ProviderCostObservation(cost_state="priced", amount=Decimal("0.00028"), currency="USD"),
        ProviderCostObservation(cost_state="uncertain"),
    ):
        assert adapter.validate_json(adapter.dump_json(event)) == event
        assert adapter.validate_python(adapter.dump_python(event)) == event


@pytest.mark.parametrize(
    "factory",
    [
        lambda: HostLifecycleSignal(signal="private customer message"),
        lambda: CorpusVersionState(corpus_state="customer-private-v1"),
        lambda: HostLifecycleSignal(signal="resolved", message="private"),
        lambda: ChatConcurrency(value=True),
        lambda: ProviderCostObservation(cost_state="priced"),
        lambda: ProviderCostObservation(cost_state="uncertain", amount=Decimal(0), currency="USD"),
        lambda: ProviderCostObservation(cost_state="priced", amount=Decimal("NaN"), currency="USD"),
    ],
)
def test_operational_events_reject_unsafe_or_false_evidence(factory: Any) -> None:
    with pytest.raises(TelemetryError) as caught:
        factory()
    assert str(caught.value) == "Telemetry input is invalid."


def test_cost_projection_reconciles_provider_fixture_exactly() -> None:
    app = create_app(Settings(provider="ollama", ollama_model="llama3.2"))
    record = price_provider_attempt(
        _attempt(
            provider="ollama",
            model="llama3.2",
            price_snapshot=_snapshot(provider="ollama", model="llama3.2"),
        )
    )
    projected = app.state.telemetry_projector.project_cost(record)
    assert projected.amount == record.model_cost == Decimal("0.000280")
    assert projected.currency == record.currency == "USD"
    assert "example.com" not in projected.model_dump_json()
    missing = price_provider_attempt(
        _attempt(provider="ollama", model="llama3.2", price_snapshot=None)
    )
    assert app.state.telemetry_projector.project_cost(missing).amount is None
    sink = Sink()
    unit = ChatTelemetryUnit(app.state.telemetry_projector, sink, lambda: 0)
    unit.complete(
        RequestAccountingSummary(
            provider_work_started=True,
            request_completion="completed",
            attempts=(
                SettledAttempt(
                    identity=AttemptIdentity(
                        provider="ollama", model="llama3.2", provider_attempt=1
                    ),
                    cost_record=record,
                ),
            ),
        )
    )
    assert [e for e in sink.events if type(e) is ProviderCostObservation] == [projected]


def test_unsettled_attempt_emits_unknown_cost_never_zero() -> None:
    app = create_app(Settings(provider="ollama", ollama_model="llama3.2"))
    sink = Sink()
    unit = ChatTelemetryUnit(app.state.telemetry_projector, sink, lambda: 0)
    unit.complete(
        RequestAccountingSummary(
            provider_work_started=True,
            request_completion="cancelled",
            attempts=(
                UncertainAttempt(
                    identity=AttemptIdentity(
                        provider="ollama", model="llama3.2", provider_attempt=1
                    ),
                    reason="cleanup_uncertain",
                ),
            ),
        )
    )
    assert [e for e in sink.events if type(e) is ProviderCostObservation] == [
        ProviderCostObservation(cost_state="uncertain")
    ]


def test_shared_concurrency_balances_completion_and_missing_summary() -> None:
    app = create_app(Settings())
    sink = Sink()
    tracker = ChatConcurrencyTracker()
    one = ChatTelemetryUnit(app.state.telemetry_projector, sink, lambda: 0, tracker)
    two = ChatTelemetryUnit(app.state.telemetry_projector, sink, lambda: 1, tracker)
    one.complete(
        RequestAccountingSummary(provider_work_started=False, request_completion="cancelled")
    )
    two.summary_missing()
    assert tracker.active == 0
    assert [e.value for e in sink.events if type(e) is ChatConcurrency] == [1, 2, 1, 0]
    assert not any(type(e) is HostLifecycleSignal for e in sink.events)


def test_host_signals_are_explicit_and_revalidated() -> None:
    sink = Sink()
    port = HostLifecycleTelemetry(sink)
    for signal in ("handoff_requested", "handoff_completed", "resolved", "unresolved"):
        event = HostLifecycleSignal.model_validate({"signal": signal})
        port.emit(event)
    assert len(sink.events) == 4
    forged = HostLifecycleSignal(signal="resolved")
    object.__setattr__(forged, "signal", "private message")
    with pytest.raises(TelemetryError):
        port.emit(forged)
    with pytest.raises(TelemetryError):
        port.emit({"signal": "resolved"})  # type: ignore[arg-type]
    assert len(sink.events) == 4


def test_request_measures_first_generated_chunk_without_changing_sse(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "db.sqlite",
        chroma_path=tmp_path / "vectors",
        retrieval_max_distance=1000,
    )
    sink = Sink()
    ticks = iter((0, 125_000_000, 500_000_000, 0, 125_000_000, 500_000_000))
    app = create_app(settings, telemetry_sink=sink, telemetry_monotonic_ns=ticks.__next__)
    with TestClient(app) as client:
        _ingest(app, settings, "private-document.md", b"Private canary content.")
        observed = client.post("/api/v1/chat/message", json={"session_id": "one", "message": "hi"})
        app.state.telemetry_sink = NullTelemetrySink()
        baseline = client.post("/api/v1/chat/message", json={"session_id": "two", "message": "hi"})
    assert observed.status_code == baseline.status_code == 200
    assert observed.content == baseline.content
    assert [e.value for e in sink.events if type(e) is ChatFirstTokenSeconds] == [Decimal("0.125")]
    assert [e.corpus_state for e in sink.events if type(e) is CorpusVersionState] == [
        "local_active"
    ]
    assert [e.value for e in sink.events if type(e) is ChatConcurrency] == [1, 0]
    encoded = "".join(e.model_dump_json() for e in sink.events)
    assert "private" not in encoded.lower()
    assert not any(type(e) is HostLifecycleSignal for e in sink.events)
