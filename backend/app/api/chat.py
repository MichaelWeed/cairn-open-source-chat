"""POST /api/v1/chat/message — see DEVELOPER_README.md §4 for the contract."""

import asyncio
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from starlette.types import Message, Send

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
from app.endpoint_controls import (
    Admitted,
    ClientIdentityError,
    ClientPeerError,
    ControlLease,
    ControlStoreError,
    ControlTicket,
    Denied,
    EndpointController,
    EndpointControllerError,
    Lost,
    Pending,
    Queued,
    Renewed,
)
from app.logging_config import (
    ProviderStreamFailedLog,
    emit_app_log,
    emit_gemini_provider_stream_failed,
    emit_retrieval_failed,
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
from app.telemetry import ChatTelemetryUnit

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
        emit_retrieval_failed("invalid_request")
        grounding_bundle = None
    except asyncio.CancelledError:
        raise
    except RetrievalError as error:
        emit_retrieval_failed(error.code)
        grounding_bundle = None
    except Exception:
        emit_retrieval_failed("malformed_result")
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
            emit_retrieval_failed(error.code)
            grounding_bundle = None
        except Exception:
            emit_retrieval_failed("malformed_result")
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
        emit_gemini_provider_stream_failed(
            exc.code,
            exc.retryable,
            exc.attempt_count,
        )
        yield ErrorEvent(code=exc.code, message=exc.message, retryable=exc.retryable)
        return
    except Exception:
        if terminal_produced:
            raise RequestAccountingError from None
        emit_app_log(ProviderStreamFailedLog())
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


def _control_denial(reason: str) -> ErrorEvent:
    if reason == "ip_rate_limited":
        return ErrorEvent(
            code="rate_limited",
            message="Too many requests from this network.",
            retryable=True,
        )
    if reason == "session_rate_limited":
        return ErrorEvent(
            code="rate_limited",
            message="Too many requests for this session.",
            retryable=True,
        )
    if reason == "budget_exhausted":
        return ErrorEvent(
            code="budget_exhausted",
            message="The assistant is temporarily unavailable. Please try again later.",
            retryable=True,
        )
    return ErrorEvent(
        code="concurrency_limited",
        message="The assistant is busy. Please try again shortly.",
        retryable=True,
    )


def _control_internal() -> ErrorEvent:
    return ErrorEvent(
        code="internal",
        message="Cairn could not complete that answer. Please try again.",
        retryable=True,
    )


def _control_authority_remaining(
    controller: EndpointController, lease: ControlLease
) -> float | None:
    try:
        return controller.authority_remaining(lease)
    except asyncio.CancelledError:
        raise
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except Exception:
        return None


async def _close_iterator(iterator: AsyncIterator[object]) -> None:
    close = getattr(iterator, "aclose", None)
    if close is not None:
        await close()


async def _cancel_stream_tasks(
    primary: BaseException | None,
    *futures: asyncio.Future[object],
) -> tuple[BaseException | None, bool]:
    cleanup_failed = False
    current = asyncio.current_task()
    if primary is None and current is not None and current.cancelling() > 0:
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError as error:
            primary = error
    for future in futures:
        if not future.done():
            future.cancel()
    for future in futures:
        try:
            await future
        except asyncio.CancelledError as error:
            current = asyncio.current_task()
            if primary is None and current is not None and current.cancelling() > 0:
                primary = error
        except (KeyboardInterrupt, SystemExit, GeneratorExit) as error:
            cleanup_failed = True
            if primary is None:
                primary = error
        except BaseException:
            cleanup_failed = True
    return primary, cleanup_failed


class _ControlledResponseOwner:
    """Keep one lexical accounting session and lease through the final send."""

    __slots__ = ("_controller", "_ticket", "_lease", "_session", "_cleanup_uncertain")

    def __init__(self, controller: EndpointController) -> None:
        self._controller = controller
        self._ticket: ControlTicket | None = None
        self._lease: ControlLease | None = None
        self._session: RequestAccountingSession | None = None
        self._cleanup_uncertain = False

    def claim(self, lease: ControlLease) -> None:
        if self._ticket is not None or (self._lease is not None and self._lease != lease):
            raise EndpointControllerError from None
        self._lease = lease

    def claim_ticket(self, ticket: ControlTicket) -> None:
        if self._ticket is not None or self._lease is not None:
            raise EndpointControllerError from None
        self._ticket = ticket

    def release_ticket(self, ticket: ControlTicket) -> None:
        if self._ticket != ticket or self._lease is not None:
            raise EndpointControllerError from None
        self._ticket = None

    def promote(self, ticket: ControlTicket, lease: ControlLease) -> None:
        if self._ticket != ticket or self._lease is not None:
            raise EndpointControllerError from None
        self._ticket = None
        self._lease = lease

    def bind_session(self, session: RequestAccountingSession) -> None:
        if self._lease is None or self._session is not None:
            raise EndpointControllerError from None
        self._session = session

    def mark_cleanup_uncertain(self) -> None:
        self._cleanup_uncertain = True

    async def finalize(
        self,
        completion: RequestCompletion,
        *,
        cleanup_failed: bool,
    ) -> RequestAccountingSummary | None:
        ticket = self._ticket
        lease = self._lease
        session = self._session
        self._ticket = None
        self._lease = None
        self._session = None
        if ticket is not None:
            if lease is not None or session is not None:
                raise EndpointControllerError from None
            await self._controller.cancel(ticket)
            return None
        if lease is None and session is None:
            return None
        if lease is None:
            raise EndpointControllerError from None
        if session is None:
            await self._controller.finalize_without_provider(lease)
            return None
        if cleanup_failed or self._cleanup_uncertain:
            try:
                session.active_cleanup_uncertain()
            except RequestAccountingError:
                pass
        try:
            summary = session.finalize(completion)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit) as error:
            primary = error
            try:
                await self._controller.finalize_uncertain(lease)
            except BaseException:
                pass
            raise primary.with_traceback(None) from None
        except Exception:
            await self._controller.finalize_uncertain(lease)
            raise EndpointControllerError from None
        await self._controller.finalize(lease, summary)
        return summary


async def _admitted_controlled_stream(
    *,
    request: Request,
    body: ChatMessageRequest,
    controller: EndpointController,
    lease: ControlLease,
    response_owner: _ControlledResponseOwner,
) -> AsyncIterator[ChatEvent]:
    settings = request.app.state.settings
    provider: Provider = request.app.state.provider
    binding = request.app.state.provider_accounting_binding
    response_owner.claim(lease)
    remaining_authority = _control_authority_remaining(controller, lease)
    if remaining_authority is None or remaining_authority <= 0:
        yield _control_internal()
        return
    if binding is None:
        yield _control_internal()
        return
    try:
        validate_controlled_provider_binding(settings, provider, binding)
        accounting_session = request.app.state.request_accounting_factory.create(
            attempt_date=lease.attempt_date
        )
        provider_observer = controlled_provider_observer(
            settings,
            provider,
            binding,
            accounting_session,
        )
        response_owner.bind_session(accounting_session)
    except asyncio.CancelledError:
        raise
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except Exception:
        yield _control_internal()
        return

    remaining_authority = _control_authority_remaining(controller, lease)
    if remaining_authority is None or remaining_authority <= 0:
        yield _control_internal()
        return
    renewal_interval = controller.lease_renew_seconds
    source = chat_event_stream(
        provider,
        body,
        request.app.state.retrieval_route_resolver,
        top_k=settings.retrieval_top_k,
        max_distance=request.app.state.retrieval_max_distance,
        retrieval_distance_measure=request.app.state.retrieval_distance_measure,
        system_instruction=settings.system_instruction,
        max_output_tokens=settings.max_output_tokens,
        max_output_chars=settings.max_output_chars,
        accounting_session=accounting_session,
        provider_observer=provider_observer,
    )
    iterator = source.__aiter__()
    next_event: asyncio.Future[ChatEvent] | None = None
    renewal: asyncio.Task[None] | None = None
    terminal_sent = False
    cleanup_uncertain = False
    internal_failure = False
    primary: BaseException | None = None
    ticks = 0
    try:
        remaining_authority = _control_authority_remaining(controller, lease)
        if remaining_authority is None or remaining_authority <= 0:
            yield _control_internal()
            return
        next_event = asyncio.ensure_future(iterator.__anext__())
        renewal = asyncio.create_task(
            controller.sleep(min(renewal_interval, remaining_authority))
        )
        while next_event is not None and renewal is not None:
            admitted_waiters: tuple[asyncio.Future[Any], ...] = (next_event, renewal)
            done, _ = await asyncio.wait(
                admitted_waiters,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if renewal in done:
                renewal_outcome: Renewed | Lost
                timer_failed = False
                try:
                    renewal.result()
                except asyncio.CancelledError:
                    raise
                except EndpointControllerError:
                    timer_failed = True
                if timer_failed or ticks >= 60 or controller.authority_remaining(lease) <= 0:
                    renewal_outcome = Lost("lease_expired")
                else:
                    ticks += 1
                    try:
                        renewal_outcome = await controller.renew(lease)
                    except asyncio.CancelledError:
                        raise
                    except (ControlStoreError, EndpointControllerError):
                        renewal_outcome = Lost("fencing_lost")
                if type(renewal_outcome) is not Renewed:
                    next_event.cancel()
                    try:
                        await next_event
                    except asyncio.CancelledError:
                        current = asyncio.current_task()
                        if current is not None and current.cancelling() > 0:
                            raise
                    except StopAsyncIteration:
                        pass
                    except BaseException:
                        cleanup_uncertain = True
                    next_event = None
                    cleanup_uncertain = True
                    if not terminal_sent:
                        yield _control_internal()
                        terminal_sent = True
                    break
                renewal = asyncio.create_task(
                    controller.sleep(min(renewal_interval, controller.authority_remaining(lease)))
                )
            if next_event is not None and next_event in done:
                remaining_authority = _control_authority_remaining(controller, lease)
                if remaining_authority is None or remaining_authority <= 0:
                    next_event = None
                    cleanup_uncertain = True
                    if not terminal_sent:
                        yield _control_internal()
                        terminal_sent = True
                    break
                try:
                    event = next_event.result()
                except StopAsyncIteration:
                    next_event = None
                    if not terminal_sent:
                        yield _control_internal()
                        terminal_sent = True
                    break
                yield event
                terminal = _terminal_completion(event)
                if terminal is not None:
                    terminal_sent = True
                    next_event = None
                    break
                remaining_authority = _control_authority_remaining(controller, lease)
                if remaining_authority is None or remaining_authority <= 0:
                    next_event = None
                    cleanup_uncertain = True
                    if not terminal_sent:
                        yield _control_internal()
                        terminal_sent = True
                    break
                next_event = asyncio.ensure_future(iterator.__anext__())
    except asyncio.CancelledError as error:
        primary = error
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as error:
        primary = error
    except Exception:
        internal_failure = True
        cleanup_uncertain = True
    finally:
        cleanup_futures: list[asyncio.Future[object]] = []
        if next_event is not None:
            cleanup_futures.append(cast(asyncio.Future[object], next_event))
        if renewal is not None:
            cleanup_futures.append(cast(asyncio.Future[object], renewal))
        primary, task_cleanup_failed = await _cancel_stream_tasks(
            primary, *cleanup_futures
        )
        cleanup_uncertain = cleanup_uncertain or task_cleanup_failed
        del cleanup_futures, task_cleanup_failed
        try:
            await _close_iterator(iterator)
        except asyncio.CancelledError as error:
            cleanup_uncertain = True
            current = asyncio.current_task()
            if primary is None and current is not None and current.cancelling() > 0:
                primary = error
        except BaseException:
            cleanup_uncertain = True

    if cleanup_uncertain:
        response_owner.mark_cleanup_uncertain()
    if primary is not None:
        failure = primary
        del primary
        raise failure.with_traceback(None)
    if (cleanup_uncertain or internal_failure) and not terminal_sent:
        yield _control_internal()


async def _queued_controlled_stream(
    *,
    request: Request,
    body: ChatMessageRequest,
    controller: EndpointController,
    queued: Queued,
    response_owner: _ControlledResponseOwner,
) -> AsyncIterator[ChatEvent]:
    poll_task: asyncio.Task[Pending | Admitted | Denied] | None = None
    timer: asyncio.Task[None] | None = None
    primary_failure: BaseException | None = None
    internal_failure = False
    try:
        start = controller.monotonic()
        deadline = controller.deadline_from(start, controller.queue_wait_seconds)
        next_poll = start
        next_ping = controller.deadline_from(start, PING_INTERVAL_SECONDS)
        while True:
            now = controller.monotonic()
            if now >= deadline:
                break
            if poll_task is None and now >= next_poll:
                poll_task = asyncio.create_task(
                    controller.poll(queued.ticket, overall_deadline=deadline)
                )
            wake = min(
                next_ping,
                deadline,
                next_poll if poll_task is None else float("inf"),
            )
            timer = asyncio.create_task(controller.sleep(max(0.0, wake - now)))
            waiters: set[asyncio.Task[object]] = {timer}
            if poll_task is not None:
                waiters.add(poll_task)
            done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if timer not in done:
                timer.cancel()
                try:
                    await timer
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling() > 0:
                        raise
                except (KeyboardInterrupt, SystemExit, GeneratorExit):
                    raise
                except BaseException:
                    yield _control_internal()
                    return
            else:
                try:
                    timer.result()
                except asyncio.CancelledError:
                    raise
                except EndpointControllerError:
                    yield _control_internal()
                    return
            timer = None
            now = controller.monotonic()
            if now >= next_ping and now < deadline:
                yield PingEvent()
                while next_ping <= now:
                    next_ping = controller.deadline_from(next_ping, PING_INTERVAL_SECONDS)
            if poll_task is not None and poll_task in done:
                try:
                    outcome = poll_task.result()
                except asyncio.CancelledError:
                    raise
                except (ControlStoreError, EndpointControllerError):
                    if controller.monotonic() >= deadline:
                        break
                    yield _control_internal()
                    return
                poll_task = None
                if type(outcome) is Pending:
                    elapsed = max(0.0, controller.monotonic() - start)
                    next_poll = max(
                        controller.deadline_from(next_poll, 1.0),
                        controller.deadline_from(start, float(math.ceil(elapsed))),
                    )
                    continue
                if type(outcome) is Denied:
                    response_owner.release_ticket(queued.ticket)
                    yield _control_denial(outcome.reason)
                    return
                if type(outcome) is Admitted:
                    response_owner.promote(queued.ticket, outcome.lease)
                    async for event in _admitted_controlled_stream(
                        request=request,
                        body=body,
                        controller=controller,
                        lease=outcome.lease,
                        response_owner=response_owner,
                    ):
                        yield event
                    return
                yield _control_internal()
                return
        yield _control_denial("concurrency_limited")
    except asyncio.CancelledError as error:
        primary_failure = error
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as error:
        primary_failure = error
    except Exception:
        internal_failure = True
    finally:
        cleanup_futures = []
        if poll_task is not None:
            cleanup_futures.append(cast(asyncio.Future[object], poll_task))
        if timer is not None:
            cleanup_futures.append(cast(asyncio.Future[object], timer))
        primary_failure, _ = await _cancel_stream_tasks(
            primary_failure, *cleanup_futures
        )
    if primary_failure is not None:
        failure = primary_failure
        del primary_failure
        raise failure.with_traceback(None)
    if internal_failure:
        yield _control_internal()


async def _controlled_stream(
    *,
    request: Request,
    body: ChatMessageRequest,
    controller: EndpointController,
    outcome: Admitted | Queued,
    response_owner: _ControlledResponseOwner,
) -> AsyncIterator[ChatEvent]:
    if isinstance(outcome, Admitted):
        async for event in _admitted_controlled_stream(
            request=request,
            body=body,
            controller=controller,
            lease=outcome.lease,
            response_owner=response_owner,
        ):
            yield event
        return
    async for event in _queued_controlled_stream(
        request=request,
        body=body,
        controller=controller,
        queued=outcome,
        response_owner=response_owner,
    ):
        yield event


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
        controlled_owner: _ControlledResponseOwner | None = None,
        telemetry_unit: ChatTelemetryUnit | None = None,
    ) -> None:
        self._delivery_tracker: _DeliveryTracker | None = _DeliveryTracker()
        self._accounting_session = accounting_session
        self._summary_owner = summary_owner
        self._controlled_owner = controlled_owner
        self._telemetry_unit = telemetry_unit
        self._disconnect_seen = False
        self._final_send_complete = False
        super().__init__(
            _formatted_sse_stream(events, self._delivery_tracker),
            media_type="text/event-stream",
        )

    async def listen_for_disconnect(self, receive: object) -> None:
        while True:
            message = await receive()  # type: ignore[operator]
            if message["type"] == "http.disconnect":
                self._disconnect_seen = True
                if not self._final_send_complete:
                    break
                await asyncio.Event().wait()

    async def stream_response(self, send: Send) -> None:
        primary: BaseException | None = None
        cleanup_failed = False

        async def tracked_send(message: Message) -> None:
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                self._final_send_complete = True

        try:
            await super().stream_response(tracked_send)
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
        controlled_owner = self._controlled_owner
        self._controlled_owner = None
        telemetry = self._telemetry_unit
        self._telemetry_unit = None
        summary: RequestAccountingSummary | None = None
        accounting_failed = False
        if isinstance(primary, asyncio.CancelledError) and not self._disconnect_seen:
            completion: RequestCompletion = "cancelled"
        elif primary is not None or cleanup_failed:
            completion = "abandoned"
        else:
            completion = "abandoned" if tracker is None else tracker.completion or "abandoned"
        if session is not None:
            if cleanup_failed:
                try:
                    session.active_cleanup_uncertain()
                except RequestAccountingError:
                    pass
            try:
                summary = session.finalize(completion)
            except asyncio.CancelledError as error:
                if primary is None:
                    primary = error
            except BaseException:
                accounting_failed = True
        callback_failed = False
        controlled_summary: RequestAccountingSummary | None = None
        if summary is not None and owner is not None:
            try:
                await owner(summary)
            except asyncio.CancelledError as error:
                if primary is None:
                    primary = error
            except BaseException:
                callback_failed = True
        if controlled_owner is not None:
            try:
                controlled_summary = await controlled_owner.finalize(
                    completion,
                    cleanup_failed=cleanup_failed,
                )
                if controlled_summary is not None:
                    summary = controlled_summary
            except asyncio.CancelledError as error:
                if primary is None:
                    primary = error
            except (KeyboardInterrupt, SystemExit, GeneratorExit) as error:
                if primary is None:
                    primary = error
            except Exception:
                callback_failed = True
        telemetry_failure: BaseException | None = None
        if telemetry is not None:
            try:
                if summary is None:
                    telemetry.summary_missing()
                else:
                    telemetry.complete(summary)
            except BaseException as error:
                telemetry_failure = error
        if primary is None and telemetry_failure is not None:
            primary = telemetry_failure
        if primary is not None:
            failure = primary
            del self, send, session, owner, controlled_owner, telemetry, tracker
            del summary, controlled_summary, completion, primary, telemetry_failure
            raise failure.with_traceback(None)
        if accounting_failed or callback_failed or cleanup_failed:
            del self, send, session, owner, controlled_owner, telemetry, tracker
            del summary, controlled_summary, completion, telemetry_failure
            raise RequestAccountingError from None


def _sse_response(
    events: AsyncIterator[ChatEvent],
    *,
    accounting_session: RequestAccountingSession | None = None,
    summary_owner: Callable[[RequestAccountingSummary], Awaitable[None]] | None = None,
    controlled_owner: _ControlledResponseOwner | None = None,
    telemetry_unit: ChatTelemetryUnit | None = None,
) -> StreamingResponse:
    return _ClosingStreamingResponse(
        events,
        accounting_session=accounting_session,
        summary_owner=summary_owner,
        controlled_owner=controlled_owner,
        telemetry_unit=telemetry_unit,
    )


@router.post("/api/v1/chat/message")
async def chat_message(request: Request, body: ChatMessageRequest) -> StreamingResponse:
    settings = request.app.state.settings

    origin = request.headers.get("origin")
    if origin is not None and origin not in settings.origins:
        raise HTTPException(status_code=403, detail="Origin not allowed")

    controller: EndpointController | None = request.app.state.endpoint_controller
    if controller is not None:
        binding = request.app.state.provider_accounting_binding
        try:
            if binding is None:
                raise EndpointControllerError
            policy = validate_controlled_provider_binding(
                settings,
                request.app.state.provider,
                binding,
            )
            outcome = await controller.admit(
                request.client.host if request.client else None,
                tuple(request.scope.get("headers", ())),
                body.session_id,
                policy.max_attempts,
            )
        except ClientPeerError:
            return _sse_response(_single_event_stream(_control_internal()))
        except ClientIdentityError:
            event = ErrorEvent(
                code="invalid_request",
                message="The request could not be processed.",
                retryable=False,
            )
            return _sse_response(_single_event_stream(event))
        except asyncio.CancelledError:
            raise
        except (ControlStoreError, EndpointControllerError, RequestAccountingError):
            return _sse_response(_single_event_stream(_control_internal()))
        except Exception:
            return _sse_response(_single_event_stream(_control_internal()))
        if isinstance(outcome, Denied):
            return _sse_response(_single_event_stream(_control_denial(outcome.reason)))
        if not isinstance(outcome, (Admitted, Queued)):
            return _sse_response(_single_event_stream(_control_internal()))
        response_owner = _ControlledResponseOwner(controller)
        if isinstance(outcome, Admitted):
            response_owner.claim(outcome.lease)
        else:
            response_owner.claim_ticket(outcome.ticket)
        try:
            telemetry_unit = ChatTelemetryUnit(
                request.app.state.telemetry_projector,
                request.app.state.telemetry_sink,
                request.app.state.telemetry_monotonic_ns,
            )
        except asyncio.CancelledError as error:
            primary: BaseException = error
            try:
                await response_owner.finalize("cancelled", cleanup_failed=True)
            except BaseException:
                pass
            del request, body, controller, binding, policy, outcome, response_owner
            del settings, origin
            raise primary.with_traceback(None) from None
        except (KeyboardInterrupt, SystemExit, GeneratorExit) as error:
            primary = error
            try:
                await response_owner.finalize("abandoned", cleanup_failed=True)
            except BaseException:
                pass
            del request, body, controller, binding, policy, outcome, response_owner
            del settings, origin
            raise primary.with_traceback(None) from None
        except Exception:
            return _sse_response(
                _single_event_stream(_control_internal()),
                controlled_owner=response_owner,
            )
        return _sse_response(
            _controlled_stream(
                request=request,
                body=body,
                controller=controller,
                outcome=outcome,
                response_owner=response_owner,
            ),
            controlled_owner=response_owner,
            telemetry_unit=telemetry_unit,
        )

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

    telemetry_unit = ChatTelemetryUnit(
        request.app.state.telemetry_projector,
        request.app.state.telemetry_sink,
        request.app.state.telemetry_monotonic_ns,
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
            accounting_session=accounting_session,
            provider_observer=provider_observer,
        ),
        accounting_session=accounting_session,
        summary_owner=discard_accounting_summary,
        telemetry_unit=telemetry_unit,
    )
