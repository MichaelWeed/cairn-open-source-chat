import asyncio
import gc
import weakref
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from typing import Any, cast

import pytest
from starlette.types import Message, Scope, Send

from app.api.chat import (
    _cancel_stream_tasks,
    _single_event_stream,
    _sse_response,
    chat_event_stream,
    stream_with_pings,
)
from app.api.contracts import (
    RETRIEVED_CONTEXT_MAX_CHARS,
    ChatEvent,
    ChatMessageRequest,
    DoneEvent,
    ProviderGenerationRequest,
)
from app.corpus_lifecycle import (
    ActiveCorpusPointer,
    ActivePointerSnapshot,
    CorpusLifecycleRecord,
    CorpusLifecycleService,
    ExpectedActivePointer,
    LifecycleAuditRecord,
    LifecycleSnapshot,
    MarkReadyRequest,
    RemoveCorpusVersionRequest,
    StoreMutationResult,
    SwitchActiveRequest,
    VerifiedLifecycleEvidence,
)
from app.ingest.candidate_persistence import (
    AttestationIdentity,
    AttestationVerifier,
    VerifiedCandidateEvidence,
)
from app.logging_config import (
    GeminiProviderStreamFailedLog,
    ProviderStreamFailedLog,
    RetrievalFailedLog,
)
from app.providers.base import Provider
from app.providers.contracts import (
    ProviderStreamEvent,
    ProviderTextChunk,
    ProviderUsage,
    ProviderUsageChunk,
)
from app.providers.gemini import GeminiProviderError
from app.request_accounting import (
    RequestAccountingError,
    RequestAccountingSession,
    RequestAccountingSummary,
)
from app.retrieval import DEFAULT_MAX_DISTANCE, LocalRetrievalAdapter
from app.retrieval_contracts import (
    ExactCorpusReference,
    LocalActiveScope,
    RetrievalAdapter,
    RetrievalError,
    RetrievalProbe,
    RetrievalRequest,
    RetrievalResult,
    RetrievalScope,
    RetrievedChunk,
)
from app.retrieval_firestore import (
    FIRESTORE_RECORD_SCHEMA_VERSION,
    FirestoreRetrievalAdapter,
    FirestoreVectorQuery,
    FirestoreVectorRow,
    firestore_chunk_document_id,
)
from app.retrieval_integrity import compile_grounding_bundle
from app.retrieval_route import (
    ExactRetrievalAdapterBinding,
    LifecycleRetrievalRouteResolver,
    ResolvedRetrievalRoute,
    StaticRetrievalRouteResolver,
)
from app.retrieval_route import (
    validate_route_authority as _validate_route_authority,
)
from app.telemetry import ChatTelemetryUnit


@pytest.mark.asyncio
async def test_stream_cleanup_preserves_pending_caller_cancellation_before_child_cancel() -> None:
    child = asyncio.create_task(asyncio.Event().wait())
    current = asyncio.current_task()
    assert current is not None
    current.cancel("CALLER-CLEANUP-CANCEL")
    primary, cleanup_failed = await _cancel_stream_tasks(
        None, cast(asyncio.Future[object], child)
    )
    assert isinstance(primary, asyncio.CancelledError)
    assert primary.args == ("CALLER-CLEANUP-CANCEL",)
    assert cleanup_failed is False
    assert child.done()
    current.uncancel()


class _GroundedCollection:
    def count(self) -> int:
        return 1

    def query(self, **_: object) -> dict[str, list[list[object]]]:
        return {
            "ids": [["doc-1::chunk::0"]],
            "documents": [["grounding"]],
            "metadatas": [[{"document_id": "doc-1", "source": "source.md", "chunk_index": 0}]],
            "distances": [[0.0]],
        }


def _grounded_adapter() -> LocalRetrievalAdapter:
    return LocalRetrievalAdapter(_GroundedCollection())


def _static_route(adapter: RetrievalAdapter) -> StaticRetrievalRouteResolver:
    return StaticRetrievalRouteResolver(
        ResolvedRetrievalRoute(scope=LocalActiveScope(), adapter=adapter)
    )


class _LifecycleProvider(Provider):
    def __init__(self, delta: str) -> None:
        self.delta = delta
        self.closed = False
        self.never_finishes = asyncio.Event()

    async def stream(  # type: ignore[override]
        self, request: ProviderGenerationRequest
    ) -> AsyncIterator[ProviderStreamEvent]:
        try:
            yield ProviderTextChunk(delta=self.delta)
            await self.never_finishes.wait()
        finally:
            self.closed = True


class _SensitiveInvalidProvider(Provider):
    async def stream(  # type: ignore[override]
        self, request: ProviderGenerationRequest
    ) -> AsyncIterator[ProviderStreamEvent]:
        yield ProviderTextChunk(delta="provider-response-sentinel\x00")


class _NormalizedGeminiFailureProvider(Provider):
    async def stream(  # type: ignore[override]
        self, request: ProviderGenerationRequest
    ) -> AsyncIterator[ProviderStreamEvent]:
        if False:
            yield ProviderTextChunk(delta="unreachable")
        raise GeminiProviderError(code="rate_limited", retryable=True, attempt_count=2)


class _UsageThenGuardrailProvider(Provider):
    async def stream(  # type: ignore[override]
        self, request: ProviderGenerationRequest
    ) -> AsyncIterator[ProviderStreamEvent]:
        del request
        yield ProviderUsageChunk(
            provider="gemini",
            model="gemini-3.8-flash",
            provider_attempt=1,
            usage=ProviderUsage(input_tokens=17, total_tokens=17),
        )
        raise GeminiProviderError(code="guardrail_block", retryable=False, attempt_count=1)


async def _slow_source() -> AsyncIterator[ProviderStreamEvent]:
    yield ProviderTextChunk(delta="fast")
    await asyncio.sleep(0.05)
    yield ProviderTextChunk(delta="after a pause")


async def test_ping_interleaved_on_quiet_source() -> None:
    events = [event async for event in stream_with_pings(_slow_source(), ping_interval=0.01)]
    types = [event.type for event in events]
    assert types[0] == "chunk"
    assert "ping" in types
    assert types[-1] == "chunk"


async def _fast_source() -> AsyncIterator[ProviderStreamEvent]:
    yield ProviderTextChunk(delta="a")
    yield ProviderTextChunk(delta="b")


async def test_no_pings_when_source_is_fast() -> None:
    events = [event async for event in stream_with_pings(_fast_source(), ping_interval=1.0)]
    assert [event.type for event in events] == ["chunk", "chunk"]


async def test_usage_events_are_consumed_without_public_output_or_budget() -> None:
    closed = False

    async def source() -> AsyncIterator[ProviderStreamEvent]:
        nonlocal closed
        try:
            yield ProviderUsageChunk(
                provider="gemini",
                model="gemini-3.8-flash",
                provider_attempt=1,
                usage=ProviderUsage(input_tokens=10),
            )
            yield ProviderTextChunk(delta="abc")
            yield ProviderUsageChunk(
                provider="gemini",
                model="gemini-3.8-flash",
                provider_attempt=1,
                usage=ProviderUsage(input_tokens=10, output_tokens=2),
            )
            yield ProviderTextChunk(delta="def")
            yield ProviderUsageChunk(
                provider="gemini",
                model="gemini-3.8-flash",
                provider_attempt=1,
                usage=ProviderUsage(input_tokens=10, output_tokens=3),
            )
        finally:
            closed = True

    events = [event async for event in stream_with_pings(source(), max_output_chars=6)]

    assert [(event.type, getattr(event, "delta", None)) for event in events] == [
        ("chunk", "abc"),
        ("chunk", "def"),
    ]
    assert closed is True


async def test_hidden_usage_does_not_reset_public_ping_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    current_time = 0.0
    wait_count = 0
    observed_timeouts: list[float | None] = []

    async def deterministic_wait(
        tasks: set[asyncio.Task[ProviderStreamEvent]],
        timeout: float | None = None,
    ) -> tuple[
        set[asyncio.Task[ProviderStreamEvent]],
        set[asyncio.Task[ProviderStreamEvent]],
    ]:
        nonlocal current_time, wait_count
        wait_count += 1
        observed_timeouts.append(timeout)
        await asyncio.sleep(0)
        task = next(iter(tasks))
        assert task.done()
        if wait_count == 1:
            current_time = 0.9
        elif wait_count == 2:
            current_time = 1.1
        return {task}, set()

    async def source() -> AsyncIterator[ProviderStreamEvent]:
        yield ProviderUsageChunk(
            provider="gemini",
            model="gemini-3.8-flash",
            provider_attempt=1,
            usage=ProviderUsage(input_tokens=10),
        )
        yield ProviderTextChunk(delta="visible")

    monkeypatch.setattr(loop, "time", lambda: current_time)
    monkeypatch.setattr(asyncio, "wait", deterministic_wait)

    events = [event async for event in stream_with_pings(source(), ping_interval=1.0)]

    assert [event.type for event in events] == ["ping", "chunk"]
    assert observed_timeouts[0] == pytest.approx(1.0)
    assert observed_timeouts[1] == pytest.approx(0.1)


async def test_output_stops_at_exact_character_budget_and_closes_source() -> None:
    closed = False

    async def source() -> AsyncIterator[ProviderStreamEvent]:
        nonlocal closed
        try:
            yield ProviderTextChunk(delta="abc")
            yield ProviderTextChunk(delta="def")
            yield ProviderTextChunk(delta="must not be consumed")
        finally:
            closed = True

    events = [event async for event in stream_with_pings(source(), max_output_chars=5)]

    assert [(event.type, getattr(event, "delta", None)) for event in events] == [
        ("chunk", "abc"),
        ("chunk", "de"),
        ("done", None),
    ]
    assert events[-1].finish_reason == "limit"  # type: ignore[union-attr]
    assert closed is True


async def test_closing_stream_cancels_pending_provider_read_and_closes_source() -> None:
    closed = False
    never_finishes = asyncio.Event()

    async def source() -> AsyncIterator[ProviderStreamEvent]:
        nonlocal closed
        try:
            yield ProviderTextChunk(delta="first")
            await never_finishes.wait()
            yield ProviderTextChunk(delta="unreachable")
        finally:
            closed = True

    events = cast(
        AsyncGenerator[ChatEvent, None],
        stream_with_pings(source(), ping_interval=0.001),
    )
    first = await anext(events)
    ping = await anext(events)

    assert first.type == "chunk"
    assert ping.type == "ping"
    await events.aclose()
    assert closed is True


async def test_chat_event_stream_closes_provider_after_output_limit() -> None:
    provider = _LifecycleProvider("abcdef")

    events = [
        event
        async for event in chat_event_stream(
            provider,
            ChatMessageRequest(session_id="s1", message="hi"),
            _static_route(_grounded_adapter()),
            max_output_chars=5,
        )
    ]

    assert [event.type for event in events][-2:] == ["chunk", "done"]
    assert events[-1].finish_reason == "limit"  # type: ignore[union-attr]
    assert provider.closed is True


async def test_closing_chat_event_stream_closes_provider() -> None:
    provider = _LifecycleProvider("first")
    events = cast(
        AsyncGenerator[ChatEvent, None],
        chat_event_stream(
            provider,
            ChatMessageRequest(session_id="s1", message="hi"),
            _static_route(_grounded_adapter()),
            ping_interval=60,
        ),
    )

    while (await anext(events)).type != "chunk":
        pass
    await events.aclose()

    assert provider.closed is True


async def test_sse_response_send_failure_closes_provider() -> None:
    provider = _LifecycleProvider("first")
    response = _sse_response(
        chat_event_stream(
            provider,
            ChatMessageRequest(session_id="s1", message="hi"),
            _static_route(_grounded_adapter()),
            ping_interval=60,
        )
    )

    async def send(message: Message) -> None:
        if message.get("type") == "http.response.body" and b'"type":"chunk"' in cast(
            bytes, message.get("body", b"")
        ):
            raise OSError("simulated disconnect")

    with pytest.raises(OSError, match="simulated disconnect"):
        await response.stream_response(cast(Send, send))

    assert provider.closed is True


async def test_sse_response_disconnect_cancellation_closes_provider() -> None:
    provider = _LifecycleProvider("first")
    response = _sse_response(
        chat_event_stream(
            provider,
            ChatMessageRequest(session_id="s1", message="hi"),
            _static_route(_grounded_adapter()),
            ping_interval=60,
        )
    )
    chunk_sent = asyncio.Event()

    async def send(message: Message) -> None:
        if message.get("type") == "http.response.body" and b'"type":"chunk"' in cast(
            bytes, message.get("body", b"")
        ):
            chunk_sent.set()

    async def receive() -> Message:
        await chunk_sent.wait()
        return {"type": "http.disconnect"}

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/chat/message",
        "raw_path": b"/api/v1/chat/message",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "state": {},
    }

    await response(scope, receive, send)

    assert provider.closed is True


def _empty_accounting_session() -> RequestAccountingSession:
    from datetime import date

    return RequestAccountingSession(
        attempt_date=date(2026, 9, 10),
        price_snapshots=(),
        max_attempts=0,
    )


def _traceback_local_ids(error: BaseException) -> set[int]:
    found: set[int] = set()
    traceback = error.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_code.co_filename.endswith("app/api/chat.py"):
            found.update(id(value) for value in traceback.tb_frame.f_locals.values())
        traceback = traceback.tb_next
    return found


async def test_response_finalizes_after_terminal_send_and_invokes_owner_once() -> None:
    session = _empty_accounting_session()
    summaries: list[RequestAccountingSummary] = []
    sends: list[Message] = []

    async def owner(summary: RequestAccountingSummary) -> None:
        assert sends[-1] == {
            "type": "http.response.body",
            "body": b"",
            "more_body": False,
        }
        summaries.append(summary)

    async def send(message: Message) -> None:
        sends.append(message)

    response = _sse_response(
        _single_event_stream(DoneEvent(finish_reason="stop")),
        accounting_session=session,
        summary_owner=owner,
    )
    await response.stream_response(cast(Send, send))

    assert len(summaries) == 1
    assert summaries[0].request_completion == "completed"
    assert summaries[0].attempts == ()
    assert session.finalize("completed") is summaries[0]


class _CloseFailureBody:
    def __init__(self, close_error: BaseException, *, yield_body: bool = False) -> None:
        self.close_error = close_error
        self.yield_body = yield_body
        self.yielded = False
        self.close_calls = 0

    def __aiter__(self) -> "_CloseFailureBody":
        return self

    async def __anext__(self) -> bytes:
        if self.yield_body and not self.yielded:
            self.yielded = True
            return b"frame"
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_calls += 1
        raise self.close_error


async def test_close_window_cancellation_is_primary_and_summary_is_frozen() -> None:
    cancellation = asyncio.CancelledError("close-cancel-canary")
    body = _CloseFailureBody(cancellation)
    session = _empty_accounting_session()
    summaries: list[RequestAccountingSummary] = []

    async def send(message: Message) -> None:
        del message

    async def owner(summary: RequestAccountingSummary) -> None:
        summaries.append(summary)

    response = _sse_response(
        _single_event_stream(DoneEvent(finish_reason="stop")),
        accounting_session=session,
        summary_owner=owner,
    )
    response.body_iterator = body
    with pytest.raises(asyncio.CancelledError) as caught:
        await response.stream_response(cast(Send, send))
    assert caught.value is cancellation
    assert body.close_calls == 1
    assert len(summaries) == 1
    assert summaries[0].request_completion == "cancelled"


async def test_cleanup_only_failure_is_fixed_after_abandoned_summary() -> None:
    body = _CloseFailureBody(RuntimeError("CLOSE-FAILURE-CANARY"))
    session = _empty_accounting_session()
    summaries: list[RequestAccountingSummary] = []

    async def send(message: Message) -> None:
        del message

    async def owner(summary: RequestAccountingSummary) -> None:
        summaries.append(summary)

    response = _sse_response(
        _single_event_stream(DoneEvent(finish_reason="stop")),
        accounting_session=session,
        summary_owner=owner,
    )
    response.body_iterator = body
    with pytest.raises(RequestAccountingError) as caught:
        await response.stream_response(cast(Send, send))
    assert "CLOSE-FAILURE-CANARY" not in str(caught.value)
    assert caught.value.__context__ is None
    assert body.close_calls == 1
    assert [summary.request_completion for summary in summaries] == ["abandoned"]


async def test_send_failure_precedes_cancellation_during_close() -> None:
    cancellation = asyncio.CancelledError("close-cancel-canary")
    body = _CloseFailureBody(cancellation, yield_body=True)
    primary = OSError("send-primary")
    session = _empty_accounting_session()
    summaries: list[RequestAccountingSummary] = []

    async def send(message: Message) -> None:
        if message.get("type") == "http.response.body" and message.get("body") == b"frame":
            raise primary

    async def owner(summary: RequestAccountingSummary) -> None:
        summaries.append(summary)

    response = _sse_response(
        _single_event_stream(DoneEvent(finish_reason="stop")),
        accounting_session=session,
        summary_owner=owner,
    )
    response.body_iterator = body
    with pytest.raises(OSError) as caught:
        await response.stream_response(cast(Send, send))
    assert caught.value is primary
    assert body.close_calls == 1
    assert [summary.request_completion for summary in summaries] == ["abandoned"]


async def test_final_empty_send_failure_finalizes_abandoned_once() -> None:
    primary = OSError("FINAL-SEND-CANARY")
    session = _empty_accounting_session()
    summaries: list[RequestAccountingSummary] = []
    final_send_calls = 0

    async def send(message: Message) -> None:
        nonlocal final_send_calls
        if message.get("type") == "http.response.body" and not message.get("more_body", False):
            final_send_calls += 1
            raise primary

    async def owner(summary: RequestAccountingSummary) -> None:
        summaries.append(summary)

    response = _sse_response(
        _single_event_stream(DoneEvent(finish_reason="stop")),
        accounting_session=session,
        summary_owner=owner,
    )
    tracker = cast(Any, response)._delivery_tracker
    with pytest.raises(OSError) as caught:
        await response.stream_response(cast(Send, send))
    assert caught.value is primary
    assert final_send_calls == 1
    assert [summary.request_completion for summary in summaries] == ["abandoned"]
    retained = _traceback_local_ids(caught.value)
    assert id(session) not in retained
    assert id(summaries[0]) not in retained
    assert id(owner) not in retained
    assert id(tracker) not in retained


async def test_owner_ordinary_failure_is_fixed_after_summary_freezes() -> None:
    session = _empty_accounting_session()
    seen: list[RequestAccountingSummary] = []

    async def send(message: Message) -> None:
        del message

    async def owner(summary: RequestAccountingSummary) -> None:
        seen.append(summary)
        raise RuntimeError("OWNER-CANARY")

    response = _sse_response(
        _single_event_stream(DoneEvent(finish_reason="stop")),
        accounting_session=session,
        summary_owner=owner,
    )
    with pytest.raises(RequestAccountingError) as caught:
        await response.stream_response(cast(Send, send))
    assert "OWNER-CANARY" not in str(caught.value)
    assert caught.value.__context__ is None
    assert len(seen) == 1
    assert session.finalize("completed") is seen[0]


async def test_owner_cancellation_is_preserved_after_summary_freezes() -> None:
    cancellation = asyncio.CancelledError("OWNER-CANCEL-CANARY")
    session = _empty_accounting_session()
    seen: list[RequestAccountingSummary] = []

    async def send(message: Message) -> None:
        del message

    async def owner(summary: RequestAccountingSummary) -> None:
        seen.append(summary)
        raise cancellation

    response = _sse_response(
        _single_event_stream(DoneEvent(finish_reason="stop")),
        accounting_session=session,
        summary_owner=owner,
    )
    tracker = cast(Any, response)._delivery_tracker
    with pytest.raises(asyncio.CancelledError) as caught:
        await response.stream_response(cast(Send, send))
    assert caught.value is cancellation
    assert len(seen) == 1
    assert session.finalize("completed") is seen[0]
    retained = _traceback_local_ids(caught.value)
    assert id(session) not in retained
    assert id(seen[0]) not in retained
    assert id(owner) not in retained
    assert id(tracker) not in retained


async def test_telemetry_runs_after_lexical_owner_and_base_exception_is_preserved() -> None:
    order: list[str] = []
    session = _empty_accounting_session()
    failure = KeyboardInterrupt("TELEMETRY-BASE-CANARY")

    async def send(message: Message) -> None:
        del message

    async def owner(summary: RequestAccountingSummary) -> None:
        del summary
        order.append("owner")

    class Unit:
        def complete(self, summary: RequestAccountingSummary) -> None:
            del summary
            order.append("telemetry")
            raise failure

        def summary_missing(self) -> None:
            raise AssertionError("summary must exist")

    response = _sse_response(
        _single_event_stream(DoneEvent(finish_reason="stop")),
        accounting_session=session,
        summary_owner=owner,
        telemetry_unit=cast(ChatTelemetryUnit, Unit()),
    )
    with pytest.raises(KeyboardInterrupt) as caught:
        await response.stream_response(cast(Send, send))
    assert caught.value is failure
    assert order == ["owner", "telemetry"]


async def test_legacy_provider_is_called_without_observer_keyword() -> None:
    provider = _CountingProvider()
    session = _empty_accounting_session()
    events = [
        event
        async for event in chat_event_stream(
            provider,
            ChatMessageRequest(session_id="s1", message="hi"),
            _static_route(_grounded_adapter()),
            accounting_session=session,
            provider_observer=None,
        )
    ]
    assert provider.calls == 1
    assert events[-1].type == "done"


async def test_provider_failure_log_excludes_provider_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events = [
        event
        async for event in chat_event_stream(
            _SensitiveInvalidProvider(),
            ChatMessageRequest(session_id="s1", message="hi"),
            _static_route(_grounded_adapter()),
        )
    ]

    assert events[-1].type == "error"
    assert any(
        type(getattr(record, "_cairn_event", None)) is ProviderStreamFailedLog
        for record in caplog.records
    )
    assert "provider-response-sentinel" not in caplog.text


async def test_guardrail_usage_is_hidden_before_public_content_free_error() -> None:
    events = [
        event
        async for event in chat_event_stream(
            _UsageThenGuardrailProvider(),
            ChatMessageRequest(session_id="s1", message="hi"),
            _static_route(_grounded_adapter()),
        )
    ]

    assert [event.type for event in events] == [
        "status",
        "citations",
        "status",
        "error",
    ]
    assert events[-1].model_dump() == {
        "type": "error",
        "code": "guardrail_block",
        "message": "The model response was blocked by safety controls.",
        "retryable": False,
    }


class _StaticAdapter:
    def __init__(self, result: RetrievalResult) -> None:
        self.result = result
        self.requests: list[RetrievalRequest] = []

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        self.requests.append(request)
        return self.result

    async def check_readiness(self, scope: RetrievalScope) -> RetrievalProbe:
        return RetrievalProbe(
            scope=scope,
            reachable=True,
            store_ready=True,
            exact_version_ready=False,
        )


class _SequencedResolver:
    def __init__(self, routes: list[ResolvedRetrievalRoute]) -> None:
        self.routes = routes
        self.calls = 0

    async def resolve_route(self) -> ResolvedRetrievalRoute:
        route = self.routes[self.calls]
        self.calls += 1
        return route

    async def check_readiness(self) -> RetrievalProbe:
        raise AssertionError("chat must not call readiness")


class _RouteVectorClient:
    def __init__(
        self,
        *,
        rows: tuple[FirestoreVectorRow, ...] = (),
        blocked: bool = False,
    ) -> None:
        self.calls: list[FirestoreVectorQuery] = []
        self.rows = rows
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()

    async def vector_get(self, request: FirestoreVectorQuery) -> tuple[FirestoreVectorRow, ...]:
        self.calls.append(request)
        self.entered.set()
        await self.release.wait()
        return self.rows

    async def readiness_get(self, request: object) -> None:
        raise AssertionError("chat must not call readiness")

    async def aclose(self) -> None:
        raise AssertionError("caller-owned adapter must not close")


class _RouteEmbedding:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, input: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return [[0.0, 0.0] for _ in input]


def _exact_route(
    version: str,
    client: _RouteVectorClient,
) -> tuple[ResolvedRetrievalRoute, _RouteEmbedding]:
    scope = ExactCorpusReference(corpus_id="public-docs", corpus_version=version)
    embedding = _RouteEmbedding()
    adapter = FirestoreRetrievalAdapter(
        client=client,
        embedding_function=embedding,
        scope=scope,
        embedding_identity="embedding-v1",
        embedding_dimensions=2,
        distance_measure="cosine",
        timeout_seconds=3,
        max_retries=0,
        owns_client=False,
    )
    binding = ExactRetrievalAdapterBinding(
        scope=scope,
        embedding_identity="embedding-v1",
        embedding_dimensions=2,
        adapter=adapter,
    )
    return (
        ResolvedRetrievalRoute(
            scope=scope,
            adapter=adapter,
            exact_binding=binding,
        ),
        embedding,
    )


def _route_evidence(scope: ExactCorpusReference) -> VerifiedLifecycleEvidence:
    return VerifiedLifecycleEvidence(
        corpus=scope,
        plan_sha256="1" * 64,
        semantic_manifest_sha256="2" * 64,
        embedding_identity="embedding-v1",
        embedding_dimensions=2,
        document_count=1,
        chunk_count=1,
        inventory_sha256="3" * 64,
        attestation_payload_sha256="4" * 64,
        signature_algorithm_id="test-algorithm",
        signing_key_id="test-key",
    )


class _RouteAttestationVerifier:
    algorithm_id = "test-algorithm"
    key_id = "test-key"

    async def verify(self, payload: bytes, signature: bytes) -> bool:
        del payload, signature
        return True


class _RouteTrustPolicy:
    policy_version = "policy-v1"
    policy_generation = 1

    def __init__(self) -> None:
        self.verifier = _RouteAttestationVerifier()

    def verifier_for(self, identity: AttestationIdentity) -> _RouteAttestationVerifier | None:
        if (identity.algorithm_id, identity.key_id) != (
            self.verifier.algorithm_id,
            self.verifier.key_id,
        ):
            return None
        return self.verifier


class _RouteLifecycleMemoryStore:
    def __init__(self) -> None:
        self.lifecycle: dict[ExactCorpusReference, LifecycleSnapshot] = {}
        self.active: dict[str, ActivePointerSnapshot] = {}
        self.calls: list[tuple[str, object]] = []
        self.close_calls = 0
        self._lock = asyncio.Lock()

    async def read_lifecycle_snapshot(
        self,
        corpus: ExactCorpusReference,
        *,
        timeout_seconds: int,
    ) -> LifecycleSnapshot:
        assert timeout_seconds == 3
        self.calls.append(("read_lifecycle", corpus))
        return self.lifecycle.get(corpus, LifecycleSnapshot(record=None, audit=None))

    async def read_active_snapshot(
        self,
        corpus_id: str,
        *,
        timeout_seconds: int,
    ) -> ActivePointerSnapshot:
        assert timeout_seconds == 3
        self.calls.append(("read_active", corpus_id))
        return self.active.get(
            corpus_id,
            ActivePointerSnapshot(pointer=None, audit=None, target_lifecycle=None),
        )

    async def commit_ready(
        self,
        expected_absent: bool,
        replacement: CorpusLifecycleRecord,
        audit: LifecycleAuditRecord,
        *,
        timeout_seconds: int,
    ) -> StoreMutationResult:
        assert expected_absent is True
        assert timeout_seconds == 3
        self.calls.append(("ready", replacement.corpus))
        async with self._lock:
            if replacement.corpus in self.lifecycle:
                return "conflict"
            self.lifecycle[replacement.corpus] = LifecycleSnapshot(
                record=replacement,
                audit=audit,
            )
            return "applied"

    async def compare_and_swap_active(
        self,
        expected: ActivePointerSnapshot,
        replacement: ActiveCorpusPointer,
        target_ready: CorpusLifecycleRecord,
        audit: LifecycleAuditRecord,
        *,
        timeout_seconds: int,
    ) -> StoreMutationResult:
        assert timeout_seconds == 3
        self.calls.append(("switch", replacement.target))
        async with self._lock:
            current = self.active.get(
                replacement.corpus_id,
                ActivePointerSnapshot(pointer=None, audit=None, target_lifecycle=None),
            )
            target = self.lifecycle.get(target_ready.corpus)
            if (
                current != expected
                or target is None
                or target.record != target_ready
                or target_ready.state != "ready"
            ):
                return "conflict"
            self.active[replacement.corpus_id] = ActivePointerSnapshot(
                pointer=replacement,
                audit=audit,
                target_lifecycle=target,
            )
            return "applied"

    async def compare_and_remove(
        self,
        expected_ready: LifecycleSnapshot,
        replacement: CorpusLifecycleRecord,
        expected_active: ActivePointerSnapshot,
        audit: LifecycleAuditRecord,
        *,
        timeout_seconds: int,
    ) -> StoreMutationResult:
        assert timeout_seconds == 3
        self.calls.append(("remove", replacement.corpus))
        async with self._lock:
            current = self.lifecycle.get(replacement.corpus)
            active = self.active.get(
                replacement.corpus.corpus_id,
                ActivePointerSnapshot(pointer=None, audit=None, target_lifecycle=None),
            )
            if current != expected_ready or active != expected_active:
                return "conflict"
            if active.pointer is not None and active.pointer.target == replacement.corpus:
                return "conflict"
            self.lifecycle[replacement.corpus] = LifecycleSnapshot(
                record=replacement,
                audit=audit,
            )
            return "applied"

    async def aclose(self) -> None:
        self.close_calls += 1


def _route_row(version: str, label: str) -> FirestoreVectorRow:
    chunk_id = f"{label}-doc::chunk::0"
    return FirestoreVectorRow(
        document_id=firestore_chunk_document_id(chunk_id),
        fields={
            "schema_version": FIRESTORE_RECORD_SCHEMA_VERSION,
            "corpus_id": "public-docs",
            "corpus_version": version,
            "embedding_identity": "embedding-v1",
            "chunk_id": chunk_id,
            "document_id": f"{label}-doc",
            "source": f"{label}.md",
            "chunk_index": 0,
            "text": f"{label} exact eligible support",
            "citation_title": f"{label} reviewed title",
            "citation_url": f"https://docs.example/{label}",
        },
        distance=0.1,
    )


class _RouteCandidateVerifier:
    def __init__(self) -> None:
        self.calls: list[ExactCorpusReference] = []

    async def __call__(
        self,
        corpus: ExactCorpusReference,
        identity: AttestationIdentity,
        verifier: AttestationVerifier,
    ) -> VerifiedCandidateEvidence:
        assert (identity.algorithm_id, identity.key_id) == (
            verifier.algorithm_id,
            verifier.key_id,
        )
        self.calls.append(corpus)
        return VerifiedCandidateEvidence.model_validate(_route_evidence(corpus).model_dump())


class _RouteAdapterFactory:
    def __init__(
        self,
        bindings: dict[str, ExactRetrievalAdapterBinding],
    ) -> None:
        self.bindings = bindings
        self.calls: list[ExactCorpusReference] = []

    def adapter_for(
        self,
        scope: ExactCorpusReference,
        *,
        embedding_identity: str,
        embedding_dimensions: int,
    ) -> ExactRetrievalAdapterBinding:
        assert embedding_identity == "embedding-v1"
        assert embedding_dimensions == 2
        self.calls.append(scope)
        return self.bindings[scope.corpus_version]


async def test_chat_actual_lifecycle_route_keeps_inflight_a_bound_while_next_uses_b() -> None:
    client_a = _RouteVectorClient(rows=(_route_row("v1", "A"),), blocked=True)
    client_b = _RouteVectorClient(rows=(_route_row("v2", "B"),))
    route_a, embedding_a = _exact_route("v1", client_a)
    route_b, embedding_b = _exact_route("v2", client_b)
    scope_a = cast(ExactCorpusReference, route_a.scope)
    scope_b = cast(ExactCorpusReference, route_b.scope)
    lifecycle_store = _RouteLifecycleMemoryStore()
    lifecycle_candidate = _RouteCandidateVerifier()
    candidate = _RouteCandidateVerifier()
    policy = _RouteTrustPolicy()
    lifecycle = CorpusLifecycleService(
        store=lifecycle_store,
        verify_attested_candidate=lifecycle_candidate,
        expected_embedding_identity="embedding-v1",
        expected_embedding_dimensions=2,
        timeout_seconds=3,
        max_retries=0,
        sleep=asyncio.sleep,
        owns_store=False,
    )
    identity = AttestationIdentity(
        algorithm_id=policy.verifier.algorithm_id,
        key_id=policy.verifier.key_id,
    )
    ready_a = await lifecycle.mark_ready(
        MarkReadyRequest(
            contract_version="1.0",
            corpus=scope_a,
            trusted_identity=identity,
        ),
        policy,
    )
    ready_b = await lifecycle.mark_ready(
        MarkReadyRequest(
            contract_version="1.0",
            corpus=scope_b,
            trusted_identity=identity,
        ),
        policy,
    )
    promoted_a = await lifecycle.switch_active(
        SwitchActiveRequest(
            contract_version="1.0",
            action="promote",
            target=scope_a,
            expected=None,
        ),
        policy,
    )
    assert (ready_a.disposition, ready_b.disposition, promoted_a.disposition) == (
        "applied",
        "applied",
        "applied",
    )
    factory = _RouteAdapterFactory(
        {
            "v1": cast(ExactRetrievalAdapterBinding, route_a.exact_binding),
            "v2": cast(ExactRetrievalAdapterBinding, route_b.exact_binding),
        }
    )
    resolver = LifecycleRetrievalRouteResolver(
        corpus_id="public-docs",
        active_state_resolver=lifecycle,
        verify_attested_candidate=candidate,
        trust_policy_supplier=lambda: policy,
        expected_embedding_identity="embedding-v1",
        expected_embedding_dimensions=2,
        adapter_factory=factory,
    )
    provider = _CountingProvider()

    async def collect() -> list[ChatEvent]:
        return [
            event
            async for event in chat_event_stream(
                provider,
                ChatMessageRequest(session_id="s1", message="hi"),
                resolver,
                retrieval_distance_measure="cosine",
            )
        ]

    first_task = asyncio.create_task(collect())
    await client_a.entered.wait()
    assert client_b.calls == []
    promoted_b = await lifecycle.switch_active(
        SwitchActiveRequest(
            contract_version="1.0",
            action="promote",
            target=scope_b,
            expected=ExpectedActivePointer(target=scope_a, revision=0),
        ),
        policy,
    )
    removed_a = await lifecycle.remove_version(
        RemoveCorpusVersionRequest(
            contract_version="1.0",
            corpus=scope_a,
            expected_lifecycle_revision=0,
        )
    )
    second = await collect()
    assert not first_task.done()
    client_a.release.set()
    first = await first_task

    assert [event.type for event in first] == [
        "status",
        "citations",
        "status",
        "chunk",
        "done",
    ]
    assert [event.type for event in second] == [
        "status",
        "citations",
        "status",
        "chunk",
        "done",
    ]
    assert [source.id for source in first[1].sources] == ["A-doc"]  # type: ignore[union-attr]
    assert [source.id for source in second[1].sources] == ["B-doc"]  # type: ignore[union-attr]
    assert (promoted_b.disposition, promoted_b.resulting_revision) == ("applied", 1)
    assert (removed_a.disposition, removed_a.resulting_revision) == ("applied", 1)
    assert lifecycle_store.active["public-docs"].pointer is not None
    assert lifecycle_store.active["public-docs"].pointer.target == scope_b
    assert lifecycle_store.lifecycle[scope_a].record is not None
    assert lifecycle_store.lifecycle[scope_a].record.state == "logically_removed"  # type: ignore[union-attr]
    assert ("switch", scope_a) in lifecycle_store.calls
    assert ("switch", scope_b) in lifecycle_store.calls
    assert ("remove", scope_a) in lifecycle_store.calls
    assert lifecycle_store.close_calls == 0
    assert lifecycle_candidate.calls == [scope_a, scope_b, scope_a, scope_b]
    assert candidate.calls == [scope_a, scope_b]
    assert factory.calls == [scope_a, scope_b]
    assert len(client_a.calls) == len(client_b.calls) == 1
    assert embedding_a.calls == embedding_b.calls == 1
    assert ("corpus_version", "v1") in client_a.calls[0].filters
    assert ("corpus_version", "v2") in client_b.calls[0].filters
    assert provider.calls == 2
    assert len(provider.requests) == 2
    assert "B exact eligible support" in provider.requests[0].retrieved_context
    assert "A exact eligible support" in provider.requests[1].retrieved_context
    assert "A exact eligible support" not in provider.requests[0].retrieved_context
    assert "B exact eligible support" not in provider.requests[1].retrieved_context


async def test_chat_rechecks_exact_authority_immediately_before_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _RouteVectorClient()
    route, embedding = _exact_route("v1", client)
    resolver = _SequencedResolver([route])
    provider = _CountingProvider()
    validation_scopes: list[RetrievalScope | None] = []

    def validate_and_mutate(
        candidate: ResolvedRetrievalRoute,
        *,
        requested_scope: RetrievalScope | None = None,
    ) -> ResolvedRetrievalRoute:
        validation_scopes.append(requested_scope)
        validated = _validate_route_authority(
            candidate,
            requested_scope=requested_scope,
        )
        if len(validation_scopes) == 1:
            object.__setattr__(
                validated.adapter,
                "_embedding_identity",
                "mutated-after-first-validation",
            )
        return validated

    monkeypatch.setattr(
        "app.api.chat.validate_route_authority",
        validate_and_mutate,
    )

    events = [
        event
        async for event in chat_event_stream(
            provider,
            ChatMessageRequest(session_id="s1", message="hi"),
            resolver,
            retrieval_distance_measure="cosine",
        )
    ]

    assert [event.type for event in events] == ["status", "status", "chunk", "done"]
    assert validation_scopes == [None, route.scope]
    assert resolver.calls == 1
    assert client.calls == []
    assert embedding.calls == 0
    assert provider.calls == 0


class _FailingResolver:
    def __init__(self, failure: BaseException) -> None:
        self.failure = failure
        self.calls = 0

    async def resolve_route(self) -> ResolvedRetrievalRoute:
        self.calls += 1
        raise self.failure

    async def check_readiness(self) -> RetrievalProbe:
        raise AssertionError("chat must not call readiness")


async def test_route_failure_refuses_before_adapter_citation_or_provider() -> None:
    provider = _CountingProvider()
    resolver = _FailingResolver(RetrievalError("store_unavailable"))

    events = [
        event
        async for event in chat_event_stream(
            provider,
            ChatMessageRequest(session_id="s1", message="hi"),
            resolver,
        )
    ]

    assert [event.type for event in events] == ["status", "status", "chunk", "done"]
    assert resolver.calls == 1
    assert provider.calls == 0


async def test_route_resolution_cancellation_propagates_without_later_work() -> None:
    entered = asyncio.Event()

    class Resolver:
        async def resolve_route(self) -> ResolvedRetrievalRoute:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def check_readiness(self) -> RetrievalProbe:
            raise AssertionError("unreachable")

    provider = _CountingProvider()

    async def collect() -> list[ChatEvent]:
        return [
            event
            async for event in chat_event_stream(
                provider,
                ChatMessageRequest(session_id="s1", message="hi"),
                Resolver(),
            )
        ]

    task = asyncio.create_task(collect())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.calls == 0


async def test_adapter_retrieval_cancellation_propagates_before_citation_or_provider() -> None:
    client = _RouteVectorClient(blocked=True)
    route, embedding = _exact_route("v1", client)
    resolver = _SequencedResolver([route])
    provider = _CountingProvider()
    events: list[ChatEvent] = []

    async def collect() -> None:
        async for event in chat_event_stream(
            provider,
            ChatMessageRequest(session_id="s1", message="hi"),
            resolver,
            retrieval_distance_measure="cosine",
        ):
            events.append(event)

    task = asyncio.create_task(collect())
    await client.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [event.type for event in events] == ["status"]
    assert resolver.calls == 1
    assert len(client.calls) == 1
    assert embedding.calls == 1
    assert provider.calls == 0


class _CountingProvider(Provider):
    def __init__(self) -> None:
        self.calls = 0
        self.last_request: ProviderGenerationRequest | None = None
        self.requests: list[ProviderGenerationRequest] = []

    async def stream(  # type: ignore[override]
        self, request: ProviderGenerationRequest
    ) -> AsyncIterator[ProviderStreamEvent]:
        self.calls += 1
        self.last_request = request
        self.requests.append(request)
        yield ProviderTextChunk(delta="ok")


class _OneShotResolver:
    def __init__(self, route: ResolvedRetrievalRoute) -> None:
        self.route: ResolvedRetrievalRoute | None = route

    async def resolve_route(self) -> ResolvedRetrievalRoute:
        assert self.route is not None
        route = self.route
        self.route = None
        return route

    async def check_readiness(self) -> RetrievalProbe:
        raise AssertionError("chat must not call readiness")


class _RetentionProbeAdapter:
    def __init__(
        self,
        result: RetrievalResult,
        *,
        failure: bool = False,
        blocked: bool = False,
        close_calls: list[str],
    ) -> None:
        self.result = result
        self.failure = failure
        self.blocked = blocked
        self.close_calls = close_calls
        self.entered = asyncio.Event()

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        del request
        self.entered.set()
        if self.blocked:
            await asyncio.Event().wait()
        if self.failure:
            raise RuntimeError("private-adapter-error-canary")
        return self.result

    async def check_readiness(self, scope: RetrievalScope) -> RetrievalProbe:
        raise AssertionError(f"chat must not call readiness for {scope.kind}")

    def close(self) -> None:
        self.close_calls.append("close")

    async def aclose(self) -> None:
        self.close_calls.append("aclose")


@pytest.mark.parametrize("outcome", ["success", "refusal", "exception"])
async def test_chat_does_not_close_or_retain_route_adapter(outcome: str) -> None:
    request = RetrievalRequest(
        scope=LocalActiveScope(),
        query="hi",
        max_results=1,
        max_distance=DEFAULT_MAX_DISTANCE,
        distance_measure="squared_l2",
    )
    chunks = (
        ()
        if outcome == "refusal"
        else (
            RetrievedChunk(
                chunk_id="doc::chunk::0",
                document_id="doc",
                source="doc.md",
                chunk_index=0,
                text="eligible",
                distance=0.1,
            ),
        )
    )
    result = RetrievalResult(
        scope=request.scope,
        distance_measure=request.distance_measure,
        max_distance=request.max_distance,
        chunks=chunks,
    )
    close_calls: list[str] = []
    adapter = _RetentionProbeAdapter(
        result,
        failure=outcome == "exception",
        close_calls=close_calls,
    )
    adapter_reference = weakref.ref(adapter)
    resolver = _OneShotResolver(ResolvedRetrievalRoute(scope=LocalActiveScope(), adapter=adapter))

    events = [
        event
        async for event in chat_event_stream(
            _CountingProvider(),
            ChatMessageRequest(session_id="s1", message="hi"),
            resolver,
        )
    ]
    assert events[-1].type in {"done", "error"}
    assert close_calls == []
    del adapter, resolver
    gc.collect()
    assert adapter_reference() is None


async def test_cancellation_does_not_close_or_retain_route_adapter() -> None:
    request = RetrievalRequest(
        scope=LocalActiveScope(),
        query="hi",
        max_results=1,
        max_distance=DEFAULT_MAX_DISTANCE,
        distance_measure="squared_l2",
    )
    result = RetrievalResult(
        scope=request.scope,
        distance_measure=request.distance_measure,
        max_distance=request.max_distance,
        chunks=(),
    )
    close_calls: list[str] = []
    adapter = _RetentionProbeAdapter(result, blocked=True, close_calls=close_calls)
    adapter_reference = weakref.ref(adapter)
    resolver = _OneShotResolver(ResolvedRetrievalRoute(scope=LocalActiveScope(), adapter=adapter))

    async def collect(active_resolver: _OneShotResolver = resolver) -> None:
        async for _ in chat_event_stream(
            _CountingProvider(),
            ChatMessageRequest(session_id="s1", message="hi"),
            active_resolver,
        ):
            pass

    task = asyncio.create_task(collect())
    await adapter.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert close_calls == []
    del adapter, resolver, task, collect
    await asyncio.sleep(0)
    gc.collect()
    assert adapter_reference() is None


class _UsageAccountingProbeProvider(Provider):
    def __init__(self) -> None:
        self.calls = 0
        self.usage_events = 0

    async def stream(  # type: ignore[override]
        self, request: ProviderGenerationRequest
    ) -> AsyncIterator[ProviderStreamEvent]:
        del request
        self.calls += 1
        self.usage_events += 1
        yield ProviderUsageChunk(
            provider="gemini",
            model="gemini-3.8-flash",
            provider_attempt=1,
            usage=ProviderUsage(input_tokens=10, output_tokens=2),
        )
        yield ProviderTextChunk(delta="ok")


def _result_with_context_length(target: int) -> RetrievalResult:
    chunks = [
        RetrievedChunk(
            chunk_id=f"doc-{index}::chunk::0",
            document_id=f"doc-{index}",
            source=f"{index}.md",
            chunk_index=0,
            text="x",
            distance=float(index) / 10,
        )
        for index in range(4)
    ]
    request = RetrievalRequest(
        scope=LocalActiveScope(),
        query="hi",
        max_results=4,
        max_distance=DEFAULT_MAX_DISTANCE,
        distance_measure="squared_l2",
    )
    baseline = compile_grounding_bundle(
        request=request,
        adapter_result=RetrievalResult(
            scope=request.scope,
            distance_measure=request.distance_measure,
            max_distance=request.max_distance,
            chunks=tuple(chunks),
        ),
    )
    assert baseline is not None
    remaining = target - len(baseline.retrieved_context)
    assert remaining >= 0
    expanded: list[RetrievedChunk] = []
    for chunk in chunks:
        extra = min(remaining, 2_999)
        remaining -= extra
        expanded.append(chunk.model_copy(update={"text": chunk.text + "x" * extra}))
    assert remaining == 0
    return RetrievalResult(
        scope=LocalActiveScope(),
        distance_measure="squared_l2",
        max_distance=DEFAULT_MAX_DISTANCE,
        chunks=tuple(expanded),
    )


async def test_chat_filters_each_chunk_before_context_citations_and_provider() -> None:
    provider = _CountingProvider()
    result = RetrievalResult(
        scope=LocalActiveScope(),
        distance_measure="squared_l2",
        max_distance=DEFAULT_MAX_DISTANCE,
        chunks=(
            RetrievedChunk(
                chunk_id="eligible::chunk::0",
                document_id="eligible",
                source="eligible.md",
                chunk_index=0,
                text="eligible support",
                distance=DEFAULT_MAX_DISTANCE,
            ),
            RetrievedChunk(
                chunk_id="filtered::chunk::0",
                document_id="filtered",
                source="filtered.md",
                chunk_index=0,
                text="filtered private support",
                distance=DEFAULT_MAX_DISTANCE + 0.1,
            ),
        ),
    )
    resolver = _SequencedResolver(
        [
            ResolvedRetrievalRoute(
                scope=LocalActiveScope(),
                adapter=_StaticAdapter(result),
            )
        ]
    )

    events = [
        event
        async for event in chat_event_stream(
            provider,
            ChatMessageRequest(session_id="s1", message="hi"),
            resolver,
        )
    ]

    assert resolver.calls == 1
    assert [event.type for event in events] == [
        "status",
        "citations",
        "status",
        "chunk",
        "done",
    ]
    citation_event = events[1]
    assert [source.id for source in citation_event.sources] == ["eligible"]  # type: ignore[union-attr]
    assert provider.last_request is not None
    assert "eligible support" in provider.last_request.retrieved_context
    assert "filtered private support" not in provider.last_request.retrieved_context


async def test_success_exposes_only_authorized_citation_fields_in_public_events(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = _CountingProvider()
    result = RetrievalResult(
        scope=LocalActiveScope(),
        distance_measure="squared_l2",
        max_distance=DEFAULT_MAX_DISTANCE,
        chunks=(
            RetrievedChunk(
                chunk_id="private-chunk-id-canary",
                document_id="authorized-citation-id-canary",
                source="private-source-canary.md",
                chunk_index=0,
                text="private-retrieved-text-canary",
                distance=0.1,
                citation_title="Authorized citation title canary",
                citation_url="https://docs.example/authorized-citation-url-canary",
            ),
        ),
    )
    events = [
        event
        async for event in chat_event_stream(
            provider,
            ChatMessageRequest(
                session_id="private-session-canary",
                message="private-query-canary",
            ),
            _static_route(_StaticAdapter(result)),
        )
    ]

    public_payload = "".join(event.model_dump_json() for event in events)
    for authorized in (
        "authorized-citation-id-canary",
        "Authorized citation title canary",
        "https://docs.example/authorized-citation-url-canary",
    ):
        assert authorized in public_payload
    for private in (
        "private-chunk-id-canary",
        "private-source-canary",
        "private-retrieved-text-canary",
        "private-session-canary",
        "private-query-canary",
    ):
        assert private not in public_payload
        assert private not in caplog.text
    assert provider.last_request is not None
    assert "private-retrieved-text-canary" in provider.last_request.retrieved_context


async def test_retrieval_refusal_skips_provider_and_internal_usage_accounting() -> None:
    provider = _UsageAccountingProbeProvider()

    events = [
        event
        async for event in chat_event_stream(
            provider,
            ChatMessageRequest(session_id="s1", message="hi"),
            _static_route(
                _StaticAdapter(_result_with_context_length(RETRIEVED_CONTEXT_MAX_CHARS + 1))
            ),
        )
    ]

    assert [event.type for event in events] == ["status", "status", "chunk", "done"]
    assert events[-1].finish_reason == "refused"  # type: ignore[union-attr]
    assert provider.calls == 0
    assert provider.usage_events == 0


async def test_grounded_stream_hides_usage_without_shifting_absolute_ping_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    current_time = 0.0
    wait_count = 0
    observed_timeouts: list[float | None] = []

    async def deterministic_wait(
        tasks: set[asyncio.Task[ProviderStreamEvent]],
        timeout: float | None = None,
    ) -> tuple[
        set[asyncio.Task[ProviderStreamEvent]],
        set[asyncio.Task[ProviderStreamEvent]],
    ]:
        nonlocal current_time, wait_count
        wait_count += 1
        observed_timeouts.append(timeout)
        await asyncio.sleep(0)
        task = next(iter(tasks))
        assert task.done()
        if wait_count == 1:
            current_time = 0.9
        elif wait_count == 2:
            current_time = 1.1
        return {task}, set()

    monkeypatch.setattr(loop, "time", lambda: current_time)
    monkeypatch.setattr(asyncio, "wait", deterministic_wait)
    provider = _UsageAccountingProbeProvider()

    events = [
        event
        async for event in chat_event_stream(
            provider,
            ChatMessageRequest(session_id="s1", message="hi"),
            _static_route(_grounded_adapter()),
            ping_interval=1.0,
        )
    ]

    assert [event.type for event in events] == [
        "status",
        "citations",
        "status",
        "ping",
        "chunk",
        "done",
    ]
    assert provider.calls == 1
    assert provider.usage_events == 1
    assert observed_timeouts[:2] == pytest.approx([1.0, 0.1])


async def test_context_at_exact_limit_reaches_provider_after_citations() -> None:
    provider = _CountingProvider()
    adapter = _StaticAdapter(_result_with_context_length(RETRIEVED_CONTEXT_MAX_CHARS))

    events = [
        event
        async for event in chat_event_stream(
            provider,
            ChatMessageRequest(session_id="s1", message="hi"),
            _static_route(adapter),
        )
    ]

    assert [event.type for event in events] == [
        "status",
        "citations",
        "status",
        "chunk",
        "done",
    ]
    assert provider.calls == 1
    assert provider.last_request is not None
    assert len(provider.last_request.retrieved_context) == RETRIEVED_CONTEXT_MAX_CHARS
    assert adapter.requests[0].query == "hi"


async def test_oversized_context_refuses_before_citations_or_provider(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = _CountingProvider()
    adapter = _StaticAdapter(_result_with_context_length(RETRIEVED_CONTEXT_MAX_CHARS + 1))

    events = [
        event
        async for event in chat_event_stream(
            provider,
            ChatMessageRequest(session_id="session-secret", message="query-secret"),
            _static_route(adapter),
        )
    ]

    assert [event.type for event in events] == ["status", "status", "chunk", "done"]
    assert events[-1].finish_reason == "refused"  # type: ignore[union-attr]
    assert provider.calls == 0
    typed = [getattr(record, "_cairn_event", None) for record in caplog.records]
    assert typed == [RetrievalFailedLog(code="context_too_large")]
    assert "query-secret" not in caplog.text
    assert "session-secret" not in caplog.text


class _ExplodingAdapter(_StaticAdapter):
    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        raise RuntimeError("adapter-secret")


async def test_unexpected_adapter_failure_is_content_free_and_skips_provider(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = _CountingProvider()
    adapter = _ExplodingAdapter(_result_with_context_length(1_000))
    events = [
        event
        async for event in chat_event_stream(
            provider,
            ChatMessageRequest(session_id="s1", message="hi"),
            _static_route(adapter),
        )
    ]
    assert events[-1].finish_reason == "refused"  # type: ignore[union-attr]
    assert provider.calls == 0
    assert getattr(caplog.records[-1], "_cairn_event", None) == RetrievalFailedLog(
        code="malformed_result"
    )
    assert "adapter-secret" not in caplog.text


def _policy_result(case: str) -> RetrievalResult:
    first_distance = 5.0 if case == "threshold" else 0.1
    chunks: tuple[RetrievedChunk, ...] = (
        RetrievedChunk(
            chunk_id="policy-secret::chunk::0",
            document_id="policy-secret",
            source="policy-secret.md",
            chunk_index=0,
            text="chunk-secret",
            distance=first_distance,
        ),
    )
    if case == "count":
        chunks += (
            RetrievedChunk(
                chunk_id="second::chunk::0",
                document_id="second",
                source="second.md",
                chunk_index=0,
                text="second",
                distance=0.2,
            ),
        )
    return RetrievalResult(
        scope=(
            ExactCorpusReference(corpus_id="corpus-secret", corpus_version="v1-secret")
            if case == "scope"
            else LocalActiveScope()
        ),
        distance_measure="cosine" if case == "measure" else "squared_l2",
        max_distance=100.0 if case == "threshold" else 1.2,
        chunks=chunks,
    )


@pytest.mark.parametrize("case", ["scope", "measure", "count", "threshold"])
async def test_adapter_cannot_echo_different_application_policy(
    case: str, caplog: pytest.LogCaptureFixture
) -> None:
    provider = _CountingProvider()
    events = [
        event
        async for event in chat_event_stream(
            provider,
            ChatMessageRequest(session_id="session-secret", message="query-secret"),
            _static_route(_StaticAdapter(_policy_result(case))),
            top_k=1,
            max_distance=1.2,
        )
    ]

    assert [event.type for event in events] == ["status", "status", "chunk", "done"]
    assert events[-1].finish_reason == "refused"  # type: ignore[union-attr]
    assert provider.calls == 0
    typed = [getattr(record, "_cairn_event", None) for record in caplog.records]
    assert typed == [RetrievalFailedLog(code="malformed_result")]
    public_payload = "".join(event.model_dump_json() for event in events)
    for secret in (
        "session-secret",
        "query-secret",
        "policy-secret",
        "chunk-secret",
        "corpus-secret",
    ):
        assert secret not in caplog.text
        assert secret not in public_payload


class _MalformedResultAdapter(_StaticAdapter):
    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        return cast(
            RetrievalResult,
            {
                "scope": request.scope.model_dump(),
                "distance_measure": request.distance_measure,
                "max_distance": request.max_distance,
                "chunks": "malformed-secret",
            },
        )


async def test_structurally_malformed_adapter_result_uses_malformed_result_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = _CountingProvider()
    events = [
        event
        async for event in chat_event_stream(
            provider,
            ChatMessageRequest(session_id="s1", message="hi"),
            _static_route(_MalformedResultAdapter(_result_with_context_length(1_000))),
        )
    ]
    assert events[-1].finish_reason == "refused"  # type: ignore[union-attr]
    assert provider.calls == 0
    assert getattr(caplog.records[-1], "_cairn_event", None) == RetrievalFailedLog(
        code="malformed_result"
    )
    assert "malformed-secret" not in caplog.text


async def test_normalized_gemini_failure_preserves_safe_sse_and_log_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events = [
        event
        async for event in chat_event_stream(
            _NormalizedGeminiFailureProvider(),
            ChatMessageRequest(session_id="session-sentinel", message="prompt-sentinel"),
            _static_route(_grounded_adapter()),
        )
    ]

    error = events[-1]
    assert error.model_dump() == {
        "type": "error",
        "code": "rate_limited",
        "message": "The model provider is busy. Please try again.",
        "retryable": True,
    }
    typed = next(
        event
        for record in caplog.records
        if type(event := getattr(record, "_cairn_event", None)) is GeminiProviderStreamFailedLog
    )
    assert typed == GeminiProviderStreamFailedLog(code="rate_limited", retryable=True, attempt=2)
    assert "session-sentinel" not in caplog.text
    assert "prompt-sentinel" not in caplog.text
