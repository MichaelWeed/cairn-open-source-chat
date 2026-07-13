"""POST /api/v1/chat/message — see DEVELOPER_README.md §4 for the contract."""

import asyncio
import logging
from collections.abc import AsyncIterator

from chromadb.api.models.Collection import Collection
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.contracts import (
    ChatEvent,
    ChatMessageRequest,
    ChunkEvent,
    CitationsEvent,
    DoneEvent,
    ErrorEvent,
    PingEvent,
    StatusEvent,
)
from app.providers.base import Provider
from app.retrieval import DEFAULT_TOP_K, build_citations, build_context_block, retrieve_chunks

logger = logging.getLogger("app")

router = APIRouter()

PING_INTERVAL_SECONDS = 15.0


def format_sse(event: ChatEvent) -> str:
    return f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"


async def stream_with_pings(
    source: AsyncIterator[str], ping_interval: float = PING_INTERVAL_SECONDS
) -> AsyncIterator[ChatEvent]:
    """Interleave PingEvent()s into `source` whenever it goes quiet for
    `ping_interval` seconds, without cancelling or restarting `source`.

    Deliberately not `asyncio.wait_for(iterator.__anext__(), ...)`: wait_for
    cancels its inner awaitable on timeout, which would tear down the
    in-flight fetch and permanently lose whatever token the provider was
    about to yield. Instead we keep re-checking the same pending task across
    as many ping cycles as it takes.
    """
    iterator = source.__aiter__()
    next_item = asyncio.ensure_future(iterator.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({next_item}, timeout=ping_interval)
            if not done:
                yield PingEvent()
                continue
            try:
                delta = next_item.result()
            except StopAsyncIteration:
                return
            yield ChunkEvent(delta=delta)
            next_item = asyncio.ensure_future(iterator.__anext__())
    finally:
        next_item.cancel()


async def chat_event_stream(
    provider: Provider,
    body: ChatMessageRequest,
    collection: Collection,
    top_k: int = DEFAULT_TOP_K,
    ping_interval: float = PING_INTERVAL_SECONDS,
) -> AsyncIterator[ChatEvent]:
    yield StatusEvent(state="retrieving", label="Searching the knowledge base")
    try:
        chunks = retrieve_chunks(collection, body.message, top_k=top_k)
    except Exception:
        logger.exception("retrieval failed", extra={"session_id": body.session_id})
        chunks = []

    citations = build_citations(chunks)
    if citations:
        yield CitationsEvent(sources=citations)

    yield StatusEvent(state="generating", label="Generating a reply")
    try:
        provider_stream = provider.stream(
            message=body.message, history=body.history, context=build_context_block(chunks)
        )
        async for event in stream_with_pings(provider_stream, ping_interval):
            yield event
    except Exception:
        logger.exception("provider stream failed", extra={"session_id": body.session_id})
        yield ErrorEvent(
            code="provider_unavailable",
            message="The model provider is unavailable. Please try again.",
            retryable=True,
        )
        return
    yield DoneEvent(finish_reason="stop")


async def _single_event_stream(event: ChatEvent) -> AsyncIterator[ChatEvent]:
    yield event


def _sse_response(events: AsyncIterator[ChatEvent]) -> StreamingResponse:
    return StreamingResponse(
        (format_sse(event) async for event in events),
        media_type="text/event-stream",
    )


@router.post("/api/v1/chat/message")
async def chat_message(request: Request, body: ChatMessageRequest) -> StreamingResponse:
    settings = request.app.state.settings

    origin = request.headers.get("origin")
    if origin is not None and origin not in settings.origins:
        raise HTTPException(status_code=403, detail="Origin not allowed")

    client_host = request.client.host if request.client else "unknown"
    if not request.app.state.ip_rate_limiter.allow(client_host):
        event = ErrorEvent(
            code="rate_limited", message="Too many requests from this network.", retryable=True
        )
        return _sse_response(_single_event_stream(event))

    if not request.app.state.session_rate_limiter.allow(body.session_id):
        event = ErrorEvent(
            code="rate_limited", message="Too many requests for this session.", retryable=True
        )
        return _sse_response(_single_event_stream(event))

    provider: Provider = request.app.state.provider
    collection: Collection = request.app.state.document_collection
    return _sse_response(
        chat_event_stream(provider, body, collection, top_k=settings.retrieval_top_k)
    )
