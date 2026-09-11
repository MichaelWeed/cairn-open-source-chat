"""POST /api/v1/chat/message — see DEVELOPER_README.md §4 for the contract."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from starlette.types import Send

from app.api.contracts import (
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
from app.request_accounting import (
    ProviderAttemptObserver,
    RequestAccountingError,
    RequestAccountingSession,
    RequestAccountingSummary,
    RequestCompletion,
    controlled_provider_observer,
    observer_matches_session,
    validate_controlled_provider_binding,
)
from app.retrieval import (
    DEFAULT_MAX_DISTANCE,
    DEFAULT_TOP_K,
    REFUSAL_MESSAGE,
)
from app.retrieval_contracts import (
    DistanceMeasure,
    RetrievalError,
    RetrievalRequest,
)
from app.retrieval_integrity import compile_grounding_bundle
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
    accounting_session: RequestAccountingSession | None = None,
    provider_observer: ProviderAttemptObserver | None = None,
) -> AsyncIterator[ChatEvent]:
    if provider_observer is not None and (
        accounting_session is None
        or (
            provider_observer is not accounting_session
            and not observer_matches_session(provider_observer, accounting_session)
        )
    ):
        raise RequestAccountingError from None
    yield StatusEvent(state="retrieving", label="Searching the knowledge base")
    grounding_bundle = None
    try:
        route = validate_route_authority(await retrieval_route_resolver.resolve_route())
        retrieval_request = RetrievalRequest(
            scope=route.scope,
            query=body.message,
            max_results=top_k,
            max_distance=max_distance,
            distance_measure=retrieval_distance_measure,
        )
    except ValidationError:
        logger.warning("retrieval failed", extra={"retrieval_error_code": "invalid_request"})
        grounding_bundle = None
    except asyncio.CancelledError:
        raise
    except RetrievalError as error:
        logger.warning("retrieval failed", extra={"retrieval_error_code": error.code})
        grounding_bundle = None
    except Exception:
        logger.warning("retrieval failed", extra={"retrieval_error_code": "malformed_result"})
        grounding_bundle = None
    else:
        try:
            route = validate_route_authority(route, requested_scope=retrieval_request.scope)
            adapter_result = await route.adapter.retrieve(retrieval_request)
            grounding_bundle = compile_grounding_bundle(
                request=retrieval_request,
                adapter_result=adapter_result,
            )
        except RetrievalError as error:
            logger.warning("retrieval failed", extra={"retrieval_error_code": error.code})
            grounding_bundle = None
        except Exception:
            logger.warning("retrieval failed", extra={"retrieval_error_code": "malformed_result"})
            grounding_bundle = None

    if grounding_bundle is None:
        yield StatusEvent(state="refusing", label="No confident match found")
        yield ChunkEvent(delta=REFUSAL_MESSAGE)
        yield DoneEvent(finish_reason="refused")
        return

    citations = list(grounding_bundle.citations)
    if citations:
        yield CitationsEvent(sources=citations)

    yield StatusEvent(state="generating", label="Generating a reply")
    terminal_produced = False
    try:
        provider_request = ProviderGenerationRequest(
            system_instruction=system_instruction,
            message=body.message,
            history=body.history,
            retrieved_context=grounding_bundle.retrieved_context,
            max_output_tokens=max_output_tokens,
            max_output_chars=max_output_chars,
        )
        provider_stream = (
            provider.stream(provider_request)
            if provider_observer is None
            else provider.stream(provider_request, observer=provider_observer)
        )
        provider_events = stream_with_pings(
            provider_stream, ping_interval, max_output_chars=max_output_chars
        )
        try:
            async for event in provider_events:
                yield event
                if isinstance(event, DoneEvent):
                    terminal_produced = True
                    return
        finally:
            close = getattr(provider_events, "aclose", None)
            if close is not None:
                await close()
    except GeminiProviderError as exc:
        if terminal_produced:
            raise RequestAccountingError from None
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
    except Exception:
        if terminal_produced:
            raise RequestAccountingError from None
        logger.error(
            "provider stream failed",
            extra={
                "event": "provider_stream_failed",
                "code": "provider_unavailable",
            },
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


async def _empty_byte_stream() -> AsyncIterator[bytes]:
    if False:
        yield b""


class _DeliveryTracker:
    __slots__ = ("completion",)

    def __init__(self) -> None:
        self.completion: RequestCompletion | None = None


def _terminal_completion(event: ChatEvent) -> RequestCompletion | None:
    if isinstance(event, ErrorEvent):
        return "error"
    if isinstance(event, DoneEvent):
        return "completed" if event.finish_reason == "stop" else event.finish_reason
    return None


async def _formatted_sse_stream(
    events: AsyncIterator[ChatEvent], tracker: _DeliveryTracker | None = None
) -> AsyncIterator[str]:
    iterator = events.__aiter__()
    try:
        async for event in iterator:
            yield format_sse(event)
            if tracker is not None:
                completion = _terminal_completion(event)
                if completion is not None:
                    tracker.completion = completion
    finally:
        close = getattr(iterator, "aclose", None)
        if close is not None:
            await close()


class _ClosingStreamingResponse(StreamingResponse):
    def __init__(
        self,
        events: AsyncIterator[ChatEvent],
        *,
        accounting_session: RequestAccountingSession | None = None,
        summary_owner: Callable[[RequestAccountingSummary], Awaitable[None]] | None = None,
    ) -> None:
        self._delivery_tracker: _DeliveryTracker | None = _DeliveryTracker()
        self._accounting_session = accounting_session
        self._summary_owner = summary_owner
        self._disconnect_seen = False
        super().__init__(
            _formatted_sse_stream(events, self._delivery_tracker),
            media_type="text/event-stream",
        )

    async def listen_for_disconnect(self, receive: object) -> None:
        while True:
            message = await receive()  # type: ignore[operator]
            if message["type"] == "http.disconnect":
                self._disconnect_seen = True
                break

    async def stream_response(self, send: Send) -> None:
        primary: BaseException | None = None
        cleanup_failed = False
        try:
            await super().stream_response(send)
        except BaseException as error:
            primary = error
        finally:
            close = getattr(self.body_iterator, "aclose", None)
            if close is not None:
                try:
                    await close()
                except asyncio.CancelledError as error:
                    cleanup_failed = True
                    if primary is None:
                        primary = error
                except BaseException as error:
                    cleanup_failed = True
                    del error
            del close

        self.body_iterator = _empty_byte_stream()

        session = self._accounting_session
        owner = self._summary_owner
        tracker = self._delivery_tracker
        self._delivery_tracker = None
        self._accounting_session = None
        self._summary_owner = None
        summary: RequestAccountingSummary | None = None
        accounting_failed = False
        if session is not None:
            if cleanup_failed:
                try:
                    session.active_cleanup_uncertain()
                except RequestAccountingError:
                    pass
            if isinstance(primary, asyncio.CancelledError) and not self._disconnect_seen:
                completion: RequestCompletion = "cancelled"
            elif primary is not None or cleanup_failed:
                completion = "abandoned"
            else:
                completion = "abandoned" if tracker is None else tracker.completion or "abandoned"
            try:
                summary = session.finalize(completion)
            except asyncio.CancelledError as error:
                if primary is None:
                    primary = error
            except BaseException:
                accounting_failed = True
        callback_failed = False
        if summary is not None and owner is not None:
            try:
                await owner(summary)
            except asyncio.CancelledError as error:
                if primary is None:
                    primary = error
            except BaseException:
                callback_failed = True
        if primary is not None:
            failure = primary
            del self, send, session, owner, tracker, summary, primary
            raise failure.with_traceback(None)
        if accounting_failed or callback_failed or cleanup_failed:
            del self, send, session, owner, tracker, summary
            raise RequestAccountingError from None


def _sse_response(
    events: AsyncIterator[ChatEvent],
    *,
    accounting_session: RequestAccountingSession | None = None,
    summary_owner: Callable[[RequestAccountingSummary], Awaitable[None]] | None = None,
) -> StreamingResponse:
    return _ClosingStreamingResponse(
        events,
        accounting_session=accounting_session,
        summary_owner=summary_owner,
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
    retrieval_route_resolver: RetrievalRouteResolver = request.app.state.retrieval_route_resolver
    binding = request.app.state.provider_accounting_binding
    if binding is not None:
        validate_controlled_provider_binding(settings, provider, binding)
    accounting_session = request.app.state.request_accounting_factory.create()
    provider_observer = (
        None
        if binding is None
        else controlled_provider_observer(settings, provider, binding, accounting_session)
    )
    from app.request_accounting import discard_accounting_summary

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
            accounting_session=accounting_session,
            provider_observer=provider_observer,
        ),
        accounting_session=accounting_session,
        summary_owner=discard_accounting_summary,
    )
