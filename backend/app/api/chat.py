"""POST /api/v1/chat/message — see DEVELOPER_README.md §4 for the contract."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import suppress

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from starlette.types import Send

from app.api.contracts import (
    CITATION_TITLE_MAX_CHARS,
    CITATIONS_MAX_COUNT,
    ChatEvent,
    ChatMessageRequest,
    ChunkEvent,
    CitationsEvent,
    DoneEvent,
    ErrorEvent,
    PingEvent,
    ProviderGenerationRequest,
    StatusEvent,
)
from app.providers.base import Provider
from app.providers.contracts import ProviderStreamEvent
from app.providers.gemini import GeminiProviderError
from app.retrieval import (
    DEFAULT_MAX_DISTANCE,
    DEFAULT_TOP_K,
    REFUSAL_MESSAGE,
    build_citations,
    build_context_block,
)
from app.retrieval_contracts import (
    DistanceMeasure,
    RetrievalError,
    RetrievalRequest,
    RetrievalResult,
)
from app.retrieval_route import RetrievalRouteResolver, validate_route_authority

logger = logging.getLogger("app")

router = APIRouter()

PING_INTERVAL_SECONDS = 15.0


def format_sse(event: ChatEvent) -> str:
    return f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"


async def stream_with_pings(
    source: AsyncIterator[ProviderStreamEvent],
    ping_interval: float = PING_INTERVAL_SECONDS,
    max_output_chars: int | None = None,
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
    loop = asyncio.get_running_loop()
    next_ping_deadline = loop.time() + ping_interval
    emitted_chars = 0
    try:
        while True:
            remaining_to_ping = max(0.0, next_ping_deadline - loop.time())
            done, _ = await asyncio.wait({next_item}, timeout=remaining_to_ping)
            if not done:
                next_ping_deadline = loop.time() + ping_interval
                yield PingEvent()
                continue
            try:
                provider_event = next_item.result()
            except StopAsyncIteration:
                return
            if loop.time() >= next_ping_deadline:
                next_ping_deadline = loop.time() + ping_interval
                yield PingEvent()
            if provider_event.kind == "usage":
                next_item = asyncio.ensure_future(iterator.__anext__())
                continue
            chunk = provider_event
            remaining = (
                len(chunk.delta) if max_output_chars is None else max_output_chars - emitted_chars
            )
            if remaining <= 0:
                yield DoneEvent(finish_reason="limit")
                return
            delta = chunk.delta[:remaining]
            if delta:
                next_ping_deadline = loop.time() + ping_interval
                yield ChunkEvent(delta=delta)
                emitted_chars += len(delta)
            if len(delta) < len(chunk.delta):
                yield DoneEvent(finish_reason="limit")
                return
            next_item = asyncio.ensure_future(iterator.__anext__())
    finally:
        if not next_item.done():
            next_item.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration):
                await next_item
        close = getattr(iterator, "aclose", None)
        if close is not None:
            await close()


async def chat_event_stream(
    provider: Provider,
    body: ChatMessageRequest,
    retrieval_route_resolver: RetrievalRouteResolver,
    top_k: int = DEFAULT_TOP_K,
    max_distance: float = DEFAULT_MAX_DISTANCE,
    retrieval_distance_measure: DistanceMeasure = "squared_l2",
    ping_interval: float = PING_INTERVAL_SECONDS,
    system_instruction: str = "",
    max_output_tokens: int = 1500,
    max_output_chars: int = 6000,
) -> AsyncIterator[ChatEvent]:
    yield StatusEvent(state="retrieving", label="Searching the knowledge base")
    route = None
    try:
        route = validate_route_authority(
            await retrieval_route_resolver.resolve_route()
        )
        retrieval_request = RetrievalRequest(
            scope=route.scope,
            query=body.message,
            max_results=top_k,
            max_distance=max_distance,
            distance_measure=retrieval_distance_measure,
        )
    except ValidationError:
        logger.warning("retrieval failed", extra={"retrieval_error_code": "invalid_request"})
        retrieval_result = None
    except asyncio.CancelledError:
        raise
    except RetrievalError as error:
        logger.warning("retrieval failed", extra={"retrieval_error_code": error.code})
        retrieval_result = None
    except Exception:
        logger.warning("retrieval failed", extra={"retrieval_error_code": "malformed_result"})
        retrieval_result = None
    else:
        try:
            route = validate_route_authority(route, requested_scope=retrieval_request.scope)
            adapter_result = await route.adapter.retrieve(retrieval_request)
            result_payload = (
                adapter_result.model_dump()
                if isinstance(adapter_result, RetrievalResult)
                else adapter_result
            )
            retrieval_result = RetrievalResult.model_validate(result_payload)
            if (
                retrieval_result.scope != retrieval_request.scope
                or retrieval_result.distance_measure != retrieval_request.distance_measure
                or retrieval_result.max_distance != retrieval_request.max_distance
                or len(retrieval_result.chunks) > retrieval_request.max_results
            ):
                raise RetrievalError("malformed_result") from None
        except ValidationError:
            logger.warning(
                "retrieval failed", extra={"retrieval_error_code": "malformed_result"}
            )
            retrieval_result = None
        except RetrievalError as error:
            logger.warning("retrieval failed", extra={"retrieval_error_code": error.code})
            retrieval_result = None
        except Exception:
            logger.warning(
                "retrieval failed", extra={"retrieval_error_code": "malformed_result"}
            )
            retrieval_result = None

    if retrieval_result is None or retrieval_result.refused:
        yield StatusEvent(state="refusing", label="No confident match found")
        yield ChunkEvent(delta=REFUSAL_MESSAGE)
        yield DoneEvent(finish_reason="refused")
        return

    chunks = retrieval_result.chunks
    try:
        retrieved_context = build_context_block(chunks)
    except RetrievalError as error:
        logger.warning("retrieval failed", extra={"retrieval_error_code": error.code})
        yield StatusEvent(state="refusing", label="No confident match found")
        yield ChunkEvent(delta=REFUSAL_MESSAGE)
        yield DoneEvent(finish_reason="refused")
        return

    citation_chunks = [
        chunk.model_copy(update={"source": chunk.source[:CITATION_TITLE_MAX_CHARS]})
        for chunk in chunks
    ]
    citations = build_citations(citation_chunks)[:CITATIONS_MAX_COUNT]
    if citations:
        yield CitationsEvent(sources=citations)

    yield StatusEvent(state="generating", label="Generating a reply")
    try:
        provider_request = ProviderGenerationRequest(
            system_instruction=system_instruction,
            message=body.message,
            history=body.history,
            retrieved_context=retrieved_context,
            max_output_tokens=max_output_tokens,
            max_output_chars=max_output_chars,
        )
        provider_stream = provider.stream(provider_request)
        provider_events = stream_with_pings(
            provider_stream, ping_interval, max_output_chars=max_output_chars
        )
        try:
            async for event in provider_events:
                yield event
                if isinstance(event, DoneEvent):
                    return
        finally:
            close = getattr(provider_events, "aclose", None)
            if close is not None:
                await close()
    except GeminiProviderError as exc:
        logger.error(
            "provider stream failed",
            extra={
                "event": "provider_stream_failed",
                "provider": "gemini",
                "code": exc.code,
                "retryable": exc.retryable,
                "attempt_count": exc.attempt_count,
            },
        )
        yield ErrorEvent(code=exc.code, message=exc.message, retryable=exc.retryable)
        return
    except Exception as exc:
        logger.error(
            "provider stream failed",
            extra={"session_id": body.session_id, "error_type": type(exc).__name__},
        )
        yield ErrorEvent(
            code="provider_unavailable",
            message="The model provider is unavailable. Please try again.",
            retryable=True,
        )
        return
    yield DoneEvent(finish_reason="stop")


async def _single_event_stream(event: ChatEvent) -> AsyncIterator[ChatEvent]:
    yield event


async def _formatted_sse_stream(events: AsyncIterator[ChatEvent]) -> AsyncIterator[str]:
    iterator = events.__aiter__()
    try:
        async for event in iterator:
            yield format_sse(event)
    finally:
        close = getattr(iterator, "aclose", None)
        if close is not None:
            await close()


class _ClosingStreamingResponse(StreamingResponse):
    async def stream_response(self, send: Send) -> None:
        try:
            await super().stream_response(send)
        finally:
            close = getattr(self.body_iterator, "aclose", None)
            if close is not None:
                await close()


def _sse_response(events: AsyncIterator[ChatEvent]) -> StreamingResponse:
    return _ClosingStreamingResponse(
        _formatted_sse_stream(events),
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
    retrieval_route_resolver: RetrievalRouteResolver = (
        request.app.state.retrieval_route_resolver
    )
    return _sse_response(
        chat_event_stream(
            provider,
            body,
            retrieval_route_resolver,
            top_k=settings.retrieval_top_k,
            max_distance=request.app.state.retrieval_max_distance,
            retrieval_distance_measure=request.app.state.retrieval_distance_measure,
            system_instruction=settings.system_instruction,
            max_output_tokens=settings.max_output_tokens,
            max_output_chars=settings.max_output_chars,
        )
    )
