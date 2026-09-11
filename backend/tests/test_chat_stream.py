import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from typing import cast

import pytest
from starlette.types import Message, Scope, Send

from app.api.chat import _sse_response, chat_event_stream, stream_with_pings
from app.api.contracts import (
    RETRIEVED_CONTEXT_MAX_CHARS,
    ChatEvent,
    ChatMessageRequest,
    ProviderGenerationRequest,
)
from app.corpus_lifecycle import (
    AttestationTrustPolicy,
    ResolvedActiveState,
    VerifiedLifecycleEvidence,
)
from app.ingest.candidate_persistence import (
    AttestationIdentity,
    AttestationVerifier,
    VerifiedCandidateEvidence,
)
from app.providers.base import Provider
from app.providers.contracts import (
    ProviderStreamEvent,
    ProviderTextChunk,
    ProviderUsage,
    ProviderUsageChunk,
)
from app.providers.gemini import GeminiProviderError
from app.retrieval import DEFAULT_MAX_DISTANCE, LocalRetrievalAdapter, build_context_block
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
    FirestoreRetrievalAdapter,
    FirestoreVectorQuery,
    FirestoreVectorRow,
)
from app.retrieval_route import (
    ExactRetrievalAdapterBinding,
    LifecycleRetrievalRouteResolver,
    ResolvedRetrievalRoute,
    StaticRetrievalRouteResolver,
)
from app.retrieval_route import (
    validate_route_authority as _validate_route_authority,
)


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

    async def stream(
        self, request: ProviderGenerationRequest
    ) -> AsyncIterator[ProviderStreamEvent]:
        try:
            yield ProviderTextChunk(delta=self.delta)
            await self.never_finishes.wait()
        finally:
            self.closed = True


class _SensitiveInvalidProvider(Provider):
    async def stream(
        self, request: ProviderGenerationRequest
    ) -> AsyncIterator[ProviderStreamEvent]:
        yield ProviderTextChunk(delta="provider-response-sentinel\x00")


class _NormalizedGeminiFailureProvider(Provider):
    async def stream(
        self, request: ProviderGenerationRequest
    ) -> AsyncIterator[ProviderStreamEvent]:
        if False:
            yield ProviderTextChunk(delta="unreachable")
        raise GeminiProviderError(
            code="rate_limited", retryable=True, attempt_count=2
        )


class _UsageThenGuardrailProvider(Provider):
    async def stream(
        self, request: ProviderGenerationRequest
    ) -> AsyncIterator[ProviderStreamEvent]:
        del request
        yield ProviderUsageChunk(
            provider="gemini",
            model="gemini-3.8-flash",
            provider_attempt=1,
            usage=ProviderUsage(input_tokens=17, total_tokens=17),
        )
        raise GeminiProviderError(
            code="guardrail_block", retryable=False, attempt_count=1
        )


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
    assert "provider stream failed" in caplog.text
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
    def __init__(self, *, blocked: bool = False) -> None:
        self.calls: list[FirestoreVectorQuery] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()

    async def vector_get(
        self, request: FirestoreVectorQuery
    ) -> tuple[FirestoreVectorRow, ...]:
        self.calls.append(request)
        self.entered.set()
        await self.release.wait()
        return ()

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

    def verifier_for(
        self, identity: AttestationIdentity
    ) -> _RouteAttestationVerifier | None:
        if (identity.algorithm_id, identity.key_id) != (
            self.verifier.algorithm_id,
            self.verifier.key_id,
        ):
            return None
        return self.verifier


class _PromotingActiveStateResolver:
    def __init__(self, states: list[ResolvedActiveState]) -> None:
        self.states = states
        self.calls = 0

    async def resolve_active_state(
        self,
        corpus_id: str,
        trust_policy: AttestationTrustPolicy,
    ) -> ResolvedActiveState:
        del trust_policy
        state = self.states[self.calls]
        self.calls += 1
        assert corpus_id == state.target.corpus_id
        return state


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
        return VerifiedCandidateEvidence.model_validate(
            _route_evidence(corpus).model_dump()
        )


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
    client_a = _RouteVectorClient(blocked=True)
    client_b = _RouteVectorClient()
    route_a, embedding_a = _exact_route("v1", client_a)
    route_b, embedding_b = _exact_route("v2", client_b)
    state_a = ResolvedActiveState(
        target=cast(ExactCorpusReference, route_a.scope),
        pointer_revision=0,
        lifecycle_revision=0,
        evidence=_route_evidence(cast(ExactCorpusReference, route_a.scope)),
    )
    state_b = ResolvedActiveState(
        target=cast(ExactCorpusReference, route_b.scope),
        pointer_revision=1,
        lifecycle_revision=0,
        evidence=_route_evidence(cast(ExactCorpusReference, route_b.scope)),
    )
    active = _PromotingActiveStateResolver([state_a, state_b])
    candidate = _RouteCandidateVerifier()
    policy = _RouteTrustPolicy()
    factory = _RouteAdapterFactory(
        {
            "v1": cast(ExactRetrievalAdapterBinding, route_a.exact_binding),
            "v2": cast(ExactRetrievalAdapterBinding, route_b.exact_binding),
        }
    )
    resolver = LifecycleRetrievalRouteResolver(
        corpus_id="public-docs",
        active_state_resolver=active,
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
    assert active.calls == 1
    assert client_b.calls == []
    second = await collect()
    assert not first_task.done()
    client_a.release.set()
    first = await first_task

    assert [event.type for event in first] == ["status", "status", "chunk", "done"]
    assert [event.type for event in second] == ["status", "status", "chunk", "done"]
    assert active.calls == 2
    assert candidate.calls == [state_a.target, state_b.target]
    assert factory.calls == [state_a.target, state_b.target]
    assert len(client_a.calls) == len(client_b.calls) == 1
    assert embedding_a.calls == embedding_b.calls == 1
    assert ("corpus_version", "v1") in client_a.calls[0].filters
    assert ("corpus_version", "v2") in client_b.calls[0].filters
    assert provider.calls == 0


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

    async def stream(
        self, request: ProviderGenerationRequest
    ) -> AsyncIterator[ProviderStreamEvent]:
        self.calls += 1
        self.last_request = request
        yield ProviderTextChunk(delta="ok")


class _UsageAccountingProbeProvider(Provider):
    def __init__(self) -> None:
        self.calls = 0
        self.usage_events = 0

    async def stream(
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
    remaining = target - len(build_context_block(chunks))
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


async def test_retrieval_refusal_skips_provider_and_internal_usage_accounting() -> None:
    provider = _UsageAccountingProbeProvider()

    events = [
        event
        async for event in chat_event_stream(
            provider,
            ChatMessageRequest(session_id="s1", message="hi"),
            _static_route(
                _StaticAdapter(
                    _result_with_context_length(RETRIEVED_CONTEXT_MAX_CHARS + 1)
                )
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
    assert len([record for record in caplog.records if record.message == "retrieval failed"]) == 1
    assert caplog.records[-1].retrieval_error_code == "context_too_large"  # type: ignore[attr-defined]
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
    assert caplog.records[-1].retrieval_error_code == "malformed_result"  # type: ignore[attr-defined]
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
    assert len([record for record in caplog.records if record.message == "retrieval failed"]) == 1
    assert caplog.records[-1].retrieval_error_code == "malformed_result"  # type: ignore[attr-defined]
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
            _static_route(
                _MalformedResultAdapter(_result_with_context_length(1_000))
            ),
        )
    ]
    assert events[-1].finish_reason == "refused"  # type: ignore[union-attr]
    assert provider.calls == 0
    assert caplog.records[-1].retrieval_error_code == "malformed_result"  # type: ignore[attr-defined]
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
    record = next(record for record in caplog.records if hasattr(record, "attempt_count"))
    record_fields = vars(record)
    assert record_fields["event"] == "provider_stream_failed"
    assert record_fields["provider"] == "gemini"
    assert record_fields["code"] == "rate_limited"
    assert record_fields["retryable"] is True
    assert record_fields["attempt_count"] == 2
    assert not hasattr(record, "session_id")
    assert "session-sentinel" not in caplog.text
    assert "prompt-sentinel" not in caplog.text
