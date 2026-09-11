from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, cast

import httpx

from app.api.contracts import CHUNK_MAX_CHARS, ProviderGenerationRequest
from app.providers.base import Provider
from app.providers.contracts import (
    ProviderStreamEvent,
    ProviderTextChunk,
    ProviderUsage,
    ProviderUsageChunk,
    ProviderUsageValidationError,
    merge_cumulative_usage,
)

if TYPE_CHECKING:
    from app.request_accounting import ProviderAttemptObserver


class _InvalidProviderOutput(ValueError):
    """Content-free failure for output that cannot satisfy the provider seam."""


class _TrackedResponseContext:
    def __init__(self, context: object, cleanup_state: list[bool]) -> None:
        self._context = context
        self._cleanup_state = cleanup_state

    async def __aenter__(self) -> httpx.Response:
        return cast(httpx.Response, await self._context.__aenter__())  # type: ignore[attr-defined]

    async def __aexit__(self, *args: object) -> object:
        try:
            return await self._context.__aexit__(*args)  # type: ignore[attr-defined]
        except BaseException:
            self._cleanup_state[0] = True
            raise


def _provider_chunk(delta: str) -> ProviderTextChunk:
    try:
        return ProviderTextChunk(delta=delta)
    except ValueError:
        raise _InvalidProviderOutput("Ollama returned invalid response content") from None


def _split_complete_chunks(content: str) -> tuple[list[ProviderTextChunk], str]:
    chunks: list[ProviderTextChunk] = []
    while len(content) > CHUNK_MAX_CHARS:
        split_at = CHUNK_MAX_CHARS
        if not content[CHUNK_MAX_CHARS:].strip():
            # Keep one non-whitespace character with a trailing whitespace run so
            # the remainder remains a valid, lossless provider chunk.
            split_at = len(content[:CHUNK_MAX_CHARS].rstrip()) - 1
        delta = content[:split_at]
        if not delta.strip():
            break
        chunks.append(_provider_chunk(delta))
        content = content[split_at:]
    return chunks, content


def _usage_chunk(
    data: dict[str, object],
    *,
    model: str,
    current: ProviderUsageChunk | None,
) -> ProviderUsageChunk | None:
    if "prompt_eval_count" not in data and "eval_count" not in data:
        return None

    def optional_count(value: object) -> int | None:
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool):
            raise _InvalidProviderOutput("Ollama returned invalid usage metadata")
        return value

    input_tokens = optional_count(data.get("prompt_eval_count"))
    output_tokens = optional_count(data.get("eval_count"))
    total_tokens: int | None = None
    if input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    try:
        update = ProviderUsageChunk(
            provider="ollama",
            model=model,
            provider_attempt=1,
            service_tier=None,
            usage=ProviderUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
            ),
        )
        return merge_cumulative_usage(current, update)
    except (ValueError, ProviderUsageValidationError):
        raise _InvalidProviderOutput("Ollama returned invalid usage metadata") from None


class OllamaProvider(Provider):
    """Local-model provider (default), talking to Ollama's /api/chat.

    Connection errors and non-2xx responses propagate to the caller — see
    Provider.stream's docstring.
    """

    # httpx's unconfigured default is a flat 5s across connect/read/write —
    # nowhere near enough for a local model's first token, let alone a full
    # reply. A real 8B model on ordinary hardware routinely takes 10-20+
    # seconds; a "thinking"/reasoning-style model longer still, since it
    # generates hidden reasoning tokens before anything user-visible.
    # Confirmed live: this was a 100%-reproducible false "provider
    # unavailable" on every single request, not an edge case. Keep the
    # connect timeout tight (fail fast if Ollama itself isn't reachable)
    # but give read/write room for genuine local-model latency.
    _DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=180.0, write=10.0, pool=5.0)

    def __init__(self, base_url: str, model: str, client: httpx.AsyncClient | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._client = client or httpx.AsyncClient(timeout=self._DEFAULT_TIMEOUT)

    async def stream(
        self,
        request: ProviderGenerationRequest,
        *,
        observer: ProviderAttemptObserver | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        from app.request_accounting import AttemptIdentity, RequestAccountingError

        messages: list[dict[str, str]] = []
        if request.system_instruction:
            messages.append({"role": "system", "content": request.system_instruction})
        if request.retrieved_context:
            messages.append({"role": "system", "content": request.retrieved_context})
        messages.extend({"role": turn.role, "content": turn.content} for turn in request.history)
        messages.append({"role": "user", "content": request.message})

        base_url = self._base_url
        model = self._model
        client = self._client
        identity = AttemptIdentity(provider="ollama", model=model, provider_attempt=1)
        started = False
        authoritative = False
        cleanup_state = [False]
        try:
            if observer is not None:
                try:
                    observer.attempt_started(identity)
                except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                    raise
                except BaseException:
                    raise RequestAccountingError from None
                started = True
            response_context = client.stream(
                "POST",
                f"{base_url}/api/chat",
                json={
                    "model": model,
                    "messages": messages,
                    "stream": True,
                    "options": {"num_predict": request.max_output_tokens},
                },
            )
            async with _TrackedResponseContext(response_context, cleanup_state) as response:
                response.raise_for_status()
                pending_content = ""
                cumulative_usage: ProviderUsageChunk | None = None
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    if not isinstance(data, dict):
                        raise _InvalidProviderOutput("Ollama returned invalid response content")
                    if data.get("done"):
                        usage = _usage_chunk(
                            data,
                            model=model,
                            current=cumulative_usage,
                        )
                        if usage is not None:
                            cumulative_usage = usage
                            if observer is not None:
                                try:
                                    observer.usage_observed(usage)
                                except (
                                    KeyboardInterrupt,
                                    SystemExit,
                                    asyncio.CancelledError,
                                    RequestAccountingError,
                                ):
                                    raise
                                except BaseException:
                                    raise RequestAccountingError from None
                            yield usage
                    content = data.get("message", {}).get("content")
                    if content is not None and not isinstance(content, str):
                        raise _InvalidProviderOutput("Ollama returned invalid response content")
                    if content:
                        if (
                            content.strip()
                            and pending_content.strip()
                            and len(pending_content) <= CHUNK_MAX_CHARS
                        ):
                            ready, pending_content = _split_complete_chunks(pending_content)
                            for chunk in ready:
                                yield chunk
                            if pending_content.strip():
                                yield _provider_chunk(pending_content)
                                pending_content = ""
                        pending_content += content
                        ready, pending_content = _split_complete_chunks(pending_content)
                        for chunk in ready:
                            yield chunk
                    if data.get("done"):
                        break

                if pending_content:
                    ready, pending_content = _split_complete_chunks(pending_content)
                    for chunk in ready:
                        yield chunk
                    if not pending_content.strip():
                        raise _InvalidProviderOutput(
                            "Ollama response whitespace cannot satisfy chunk bounds"
                        )
                    yield _provider_chunk(pending_content)
            authoritative = True
        except asyncio.CancelledError:
            if observer is not None and started:
                try:
                    if cleanup_state[0]:
                        observer.attempt_uncertain(identity, "cleanup_uncertain")
                    else:
                        observer.attempt_finished(identity, "cancelled")
                except BaseException:
                    pass
            raise
        except GeneratorExit:
            if observer is not None and started:
                try:
                    observer.attempt_uncertain(
                        identity,
                        "cleanup_uncertain" if cleanup_state[0] else "finish_missing",
                    )
                except BaseException:
                    pass
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            if observer is not None and started:
                try:
                    if cleanup_state[0]:
                        observer.attempt_uncertain(identity, "cleanup_uncertain")
                    else:
                        observer.attempt_finished(identity, "error")
                except BaseException:
                    pass
            raise
        finally:
            if observer is not None and started and authoritative:
                try:
                    observer.attempt_finished(identity, "completed")
                except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                    raise
                except BaseException:
                    raise RequestAccountingError from None
