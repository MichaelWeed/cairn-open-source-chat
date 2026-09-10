import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from typing import cast

import pytest
from starlette.types import Message, Scope, Send

from app.api.chat import _sse_response, chat_event_stream, stream_with_pings
from app.api.contracts import (
    RETRIEVED_CONTEXT_MAX_CHARS,
    ChatEvent,
    ChatMessageRequest,
    ProviderChunk,
    ProviderGenerationRequest,
)
from app.providers.base import Provider
from app.retrieval import LocalRetrievalAdapter, build_context_block
from app.retrieval_contracts import (
    LocalActiveScope,
    RetrievalProbe,
    RetrievalRequest,
    RetrievalResult,
    RetrievalScope,
    RetrievedChunk,
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


class _LifecycleProvider(Provider):
    def __init__(self, delta: str) -> None:
        self.delta = delta
        self.closed = False
        self.never_finishes = asyncio.Event()

    async def stream(self, request: ProviderGenerationRequest) -> AsyncIterator[ProviderChunk]:
        try:
            yield ProviderChunk(delta=self.delta)
            await self.never_finishes.wait()
        finally:
            self.closed = True


class _SensitiveInvalidProvider(Provider):
    async def stream(self, request: ProviderGenerationRequest) -> AsyncIterator[ProviderChunk]:
        yield ProviderChunk(delta="provider-response-sentinel\x00")


async def _slow_source() -> AsyncIterator[ProviderChunk]:
    yield ProviderChunk(delta="fast")
    await asyncio.sleep(0.05)
    yield ProviderChunk(delta="after a pause")


async def test_ping_interleaved_on_quiet_source() -> None:
    events = [event async for event in stream_with_pings(_slow_source(), ping_interval=0.01)]
    types = [event.type for event in events]
    assert types[0] == "chunk"
    assert "ping" in types
    assert types[-1] == "chunk"


async def _fast_source() -> AsyncIterator[ProviderChunk]:
    yield ProviderChunk(delta="a")
    yield ProviderChunk(delta="b")


async def test_no_pings_when_source_is_fast() -> None:
    events = [event async for event in stream_with_pings(_fast_source(), ping_interval=1.0)]
    assert [event.type for event in events] == ["chunk", "chunk"]


async def test_output_stops_at_exact_character_budget_and_closes_source() -> None:
    closed = False

    async def source() -> AsyncIterator[ProviderChunk]:
        nonlocal closed
        try:
            yield ProviderChunk(delta="abc")
            yield ProviderChunk(delta="def")
            yield ProviderChunk(delta="must not be consumed")
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

    async def source() -> AsyncIterator[ProviderChunk]:
        nonlocal closed
        try:
            yield ProviderChunk(delta="first")
            await never_finishes.wait()
            yield ProviderChunk(delta="unreachable")
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
            _grounded_adapter(),
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
            _grounded_adapter(),
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
            _grounded_adapter(),
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
            _grounded_adapter(),
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
            _grounded_adapter(),
        )
    ]

    assert events[-1].type == "error"
    assert "provider stream failed" in caplog.text
    assert "provider-response-sentinel" not in caplog.text


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


class _CountingProvider(Provider):
    def __init__(self) -> None:
        self.calls = 0
        self.last_request: ProviderGenerationRequest | None = None

    async def stream(self, request: ProviderGenerationRequest) -> AsyncIterator[ProviderChunk]:
        self.calls += 1
        self.last_request = request
        yield ProviderChunk(delta="ok")


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
        max_distance=1.0,
        chunks=tuple(expanded),
    )


async def test_context_at_exact_limit_reaches_provider_after_citations() -> None:
    provider = _CountingProvider()
    adapter = _StaticAdapter(_result_with_context_length(RETRIEVED_CONTEXT_MAX_CHARS))

    events = [
        event
        async for event in chat_event_stream(
            provider,
            ChatMessageRequest(session_id="s1", message="hi"),
            adapter,
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
            adapter,
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
            adapter,
        )
    ]
    assert events[-1].finish_reason == "refused"  # type: ignore[union-attr]
    assert provider.calls == 0
    assert caplog.records[-1].retrieval_error_code == "malformed_result"  # type: ignore[attr-defined]
    assert "adapter-secret" not in caplog.text
