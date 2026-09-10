"""Optional Gemini generation adapter.

The Google SDK is imported only by ``create_gemini_provider`` so Cairn's
default local installation remains free of hosted-provider dependencies.
"""

import asyncio
import importlib
import json
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.api.contracts import (
    CHUNK_MAX_CHARS,
    ErrorCode,
    ProviderGenerationRequest,
)
from app.providers.base import Provider
from app.providers.contracts import (
    ProviderStreamEvent,
    ProviderTextChunk,
    ProviderUsage,
    ProviderUsageChunk,
    ProviderUsageValidationError,
    merge_cumulative_usage,
)

_RETRY_DELAY_SECONDS = 0.1
_SAFETY_REASONS = {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}
_PUBLIC_MESSAGES: dict[ErrorCode, str] = {
    "invalid_request": "The model request was rejected.",
    "rate_limited": "The model provider is busy. Please try again.",
    "provider_timeout": "The model provider timed out. Please try again.",
    "provider_unavailable": "The model provider is unavailable. Please try again.",
    "guardrail_block": "The model response was blocked by safety controls.",
}


class _ModelsClient(Protocol):
    async def generate_content_stream(self, **kwargs: object) -> AsyncIterator[object]: ...

    async def get(self, *, model: str) -> object: ...


class _AsyncClient(Protocol):
    @property
    def models(self) -> _ModelsClient: ...

    async def aclose(self) -> None: ...


class _RootClient(Protocol):
    @property
    def aio(self) -> _AsyncClient: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class GeminiReadiness:
    reachable: bool
    model_ready: bool


class GeminiProviderError(Exception):
    """Content-free normalized Gemini failure safe for the public SSE seam."""

    def __init__(
        self,
        *,
        code: ErrorCode,
        retryable: bool,
        attempt_count: int,
    ) -> None:
        message = _PUBLIC_MESSAGES[code]
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.attempt_count = attempt_count


class _NormalizedFailure(Exception):
    def __init__(self, code: ErrorCode, retryable: bool) -> None:
        super().__init__()
        self.code = code
        self.retryable = retryable


def _provider_chunk(delta: str) -> ProviderTextChunk:
    try:
        return ProviderTextChunk(delta=delta)
    except ValueError:
        raise _NormalizedFailure("provider_unavailable", False) from None


def _split_complete_chunks(content: str) -> tuple[list[ProviderTextChunk], str]:
    chunks: list[ProviderTextChunk] = []
    while len(content) > CHUNK_MAX_CHARS:
        split_at = CHUNK_MAX_CHARS
        if not content[CHUNK_MAX_CHARS:].strip():
            split_at = len(content[:CHUNK_MAX_CHARS].rstrip()) - 1
        delta = content[:split_at]
        if not delta.strip():
            break
        chunks.append(_provider_chunk(delta))
        content = content[split_at:]
    return chunks, content


def _usage_chunk(
    item: object,
    *,
    model: str,
    provider_attempt: int,
    current: ProviderUsageChunk | None,
) -> ProviderUsageChunk | None:
    metadata = getattr(item, "usage_metadata", None)
    if metadata is None:
        return None
    tier = getattr(metadata, "traffic_type", None)
    if tier is not None and not isinstance(tier, str):
        raise _NormalizedFailure("provider_unavailable", False)
    try:
        update = ProviderUsageChunk(
            provider="gemini",
            model=model,
            provider_attempt=provider_attempt,
            service_tier=tier,
            usage=ProviderUsage(
                input_tokens=getattr(metadata, "prompt_token_count", None),
                cached_input_tokens=getattr(
                    metadata, "cached_content_token_count", None
                ),
                output_tokens=getattr(metadata, "candidates_token_count", None),
                thinking_tokens=getattr(metadata, "thoughts_token_count", None),
                total_tokens=getattr(metadata, "total_token_count", None),
            ),
        )
        return merge_cumulative_usage(current, update)
    except (ValueError, ProviderUsageValidationError):
        raise _NormalizedFailure("provider_unavailable", False) from None


def _enum_name(value: object) -> str:
    raw = getattr(value, "name", value)
    return str(raw).rsplit(".", maxsplit=1)[-1].upper()


def _is_safety_block(item: object) -> bool:
    feedback = getattr(item, "prompt_feedback", None)
    if feedback is not None:
        reason = getattr(feedback, "block_reason", None)
        if reason is not None and _enum_name(reason) in _SAFETY_REASONS:
            return True
    candidates = getattr(item, "candidates", None) or []
    return any(
        (reason := getattr(candidate, "finish_reason", None)) is not None
        and _enum_name(reason) in _SAFETY_REASONS
        for candidate in candidates
    )


def _status_code(exc: Exception) -> int | None:
    for attribute in ("code", "status_code"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _normalize_exception(exc: Exception) -> _NormalizedFailure:
    if isinstance(exc, _NormalizedFailure):
        return exc
    if isinstance(exc, TimeoutError):
        return _NormalizedFailure("provider_timeout", True)
    status = _status_code(exc)
    if status == 408:
        return _NormalizedFailure("provider_timeout", True)
    if status == 429:
        return _NormalizedFailure("rate_limited", True)
    if status is not None and 400 <= status <= 499:
        return _NormalizedFailure("invalid_request", False)
    if status is not None and 500 <= status <= 599:
        return _NormalizedFailure("provider_unavailable", True)
    if isinstance(exc, (httpx.TransportError, ConnectionError, OSError)):
        return _NormalizedFailure("provider_unavailable", True)
    return _NormalizedFailure("provider_unavailable", False)


async def _close_iterator(iterator: object | None) -> None:
    close = getattr(iterator, "aclose", None)
    if close is not None:
        await close()


class GeminiProvider(Provider):
    def __init__(
        self,
        *,
        client: _AsyncClient,
        root_client: _RootClient | None = None,
        types_module: Any,
        model: str,
        timeout_seconds: float,
        max_retries: int,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._root_client = root_client
        self._types = types_module
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._timeout_milliseconds = math.ceil(timeout_seconds * 1000)
        self._max_retries = max_retries
        self._sleep = sleep
        self._closed = False

    def _contents(self, request: ProviderGenerationRequest) -> list[object]:
        contents = [
            self._types.Content(
                role="model" if turn.role == "assistant" else "user",
                parts=[self._types.Part.from_text(text=turn.content)],
            )
            for turn in request.history
        ]
        current = json.dumps(
            {
                "retrieved_support_context": request.retrieved_context,
                "visitor_question": request.message,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        contents.append(
            self._types.Content(
                role="user", parts=[self._types.Part.from_text(text=current)]
            )
        )
        return contents

    def _config(self, request: ProviderGenerationRequest) -> object:
        return self._types.GenerateContentConfig(
            system_instruction=request.system_instruction,
            temperature=0.2,
            max_output_tokens=request.max_output_tokens,
            thinking_config=self._types.ThinkingConfig(thinking_level="low"),
            http_options=self._types.HttpOptions(timeout=self._timeout_milliseconds),
        )

    async def stream(
        self, request: ProviderGenerationRequest
    ) -> AsyncIterator[ProviderStreamEvent]:
        for attempt_count in range(1, self._max_retries + 2):
            observed_item = False
            cumulative_usage: ProviderUsageChunk | None = None
            iterator: object | None = None
            failure: _NormalizedFailure | None = None
            caller_exit = False
            try:
                async with asyncio.timeout(self._timeout_seconds):
                    iterator = await self._client.models.generate_content_stream(
                        model=self._model,
                        contents=self._contents(request),
                        config=self._config(request),
                    )
                    pending_content = ""
                    async for item in iterator:
                        observed_item = True
                        usage = _usage_chunk(
                            item,
                            model=self._model,
                            provider_attempt=attempt_count,
                            current=cumulative_usage,
                        )
                        if usage is not None:
                            cumulative_usage = usage
                            yield usage
                        if _is_safety_block(item):
                            raise _NormalizedFailure("guardrail_block", False)
                        content = getattr(item, "text", None)
                        if content is not None and not isinstance(content, str):
                            raise _NormalizedFailure("provider_unavailable", False)
                        if content:
                            if (
                                content.strip()
                                and pending_content.strip()
                                and len(pending_content) <= CHUNK_MAX_CHARS
                            ):
                                ready, pending_content = _split_complete_chunks(
                                    pending_content
                                )
                                for chunk in ready:
                                    yield chunk
                                if pending_content.strip():
                                    yield _provider_chunk(pending_content)
                                    pending_content = ""
                            pending_content += content
                            ready, pending_content = _split_complete_chunks(
                                pending_content
                            )
                            for chunk in ready:
                                yield chunk

                    ready, pending_content = _split_complete_chunks(pending_content)
                    for chunk in ready:
                        yield chunk
                    if not pending_content.strip():
                        raise _NormalizedFailure("provider_unavailable", False)
                    yield _provider_chunk(pending_content)
            except (asyncio.CancelledError, GeneratorExit):
                caller_exit = True
                raise
            except Exception as exc:
                failure = _normalize_exception(exc)
            finally:
                cleanup_task = asyncio.current_task()
                cancelling_before_cleanup = (
                    cleanup_task.cancelling() if cleanup_task is not None else 0
                )
                try:
                    await _close_iterator(iterator)
                except asyncio.CancelledError:
                    cancellation_arrived = (
                        cleanup_task is not None
                        and cleanup_task.cancelling() > cancelling_before_cleanup
                    )
                    if cancellation_arrived:
                        raise
                    if failure is None and not caller_exit:
                        failure = _NormalizedFailure("provider_unavailable", False)
                except Exception:
                    if failure is None and not caller_exit:
                        failure = _NormalizedFailure("provider_unavailable", False)

            if failure is None:
                return

            if failure.retryable and not observed_item and attempt_count <= self._max_retries:
                await self._sleep(_RETRY_DELAY_SECONDS)
                continue
            raise GeminiProviderError(
                code=failure.code,
                retryable=failure.retryable,
                attempt_count=attempt_count,
            ) from None

    async def check_readiness(self) -> GeminiReadiness:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                model = await self._client.models.get(model=self._model)
        except asyncio.CancelledError:
            raise
        except Exception:
            return GeminiReadiness(reachable=False, model_ready=False)
        name = getattr(model, "name", "")
        if isinstance(name, str) and name.startswith("models/"):
            name = name.removeprefix("models/")
        methods = getattr(model, "supported_generation_methods", None) or []
        return GeminiReadiness(
            reachable=True,
            model_ready=name == self._model and "generateContent" in methods,
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._root_client is None:
            await self._client.aclose()
            return
        try:
            await self._client.aclose()
        finally:
            self._root_client.close()


def create_gemini_provider(
    *,
    api_key: str,
    model: str,
    timeout_seconds: float,
    max_retries: int,
) -> GeminiProvider:
    """Create an application-owned adapter from the optional Google SDK."""

    try:
        genai = importlib.import_module("google.genai")
        types_module = importlib.import_module("google.genai.types")
    except ModuleNotFoundError:
        raise RuntimeError(
            "Gemini provider requires the optional 'gemini' dependency profile"
        ) from None
    http_options = types_module.HttpOptions(
        base_url="https://generativelanguage.googleapis.com/",
        api_version="v1beta",
        timeout=math.ceil(timeout_seconds * 1000),
    )
    root_client = genai.Client(
        enterprise=False,
        api_key=api_key,
        http_options=http_options,
    )
    return GeminiProvider(
        client=root_client.aio,
        root_client=root_client,
        types_module=types_module,
        model=model,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
    )
