import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import cast

import httpx
import pytest

from app.api.contracts import ProviderChunk, ProviderGenerationRequest
from app.providers.base import Provider
from app.providers.gemini import GeminiProvider
from app.providers.ollama import OllamaProvider


class _Types:
    class Content:
        def __init__(self, **_: object) -> None:
            pass

    class Part:
        @classmethod
        def from_text(cls, *, text: str) -> object:
            return text

    class GenerateContentConfig:
        def __init__(self, **_: object) -> None:
            pass

    class ThinkingConfig:
        def __init__(self, **_: object) -> None:
            pass

    class HttpOptions:
        def __init__(self, **_: object) -> None:
            pass


types = _Types()


def _request() -> ProviderGenerationRequest:
    return ProviderGenerationRequest(
        system_instruction="",
        message="question",
        history=[],
        retrieved_context="",
        max_output_tokens=100,
        max_output_chars=6000,
    )


class _GeminiStream(AsyncIterator[object]):
    def __init__(self, texts: list[str]) -> None:
        self._items = iter(SimpleNamespace(text=text) for text in texts)
        self.closed = False

    def __aiter__(self) -> "_GeminiStream":
        return self

    async def __anext__(self) -> object:
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        self.closed = True


class _GeminiModels:
    def __init__(self, stream: AsyncIterator[object]) -> None:
        self.stream = stream

    async def generate_content_stream(self, **_: object) -> AsyncIterator[object]:
        return self.stream

    async def get(self, *, model: str) -> object:
        return SimpleNamespace(name=model, supported_generation_methods=["generateContent"])


class _GeminiClient:
    def __init__(self, models: _GeminiModels) -> None:
        self.models = models

    async def aclose(self) -> None:
        pass


class _TrackingBytes(httpx.AsyncByteStream):
    def __init__(self, texts: list[str]) -> None:
        lines = [
            json.dumps({"message": {"content": text}, "done": False}).encode() + b"\n"
            for text in texts
        ]
        lines.append(json.dumps({"message": {"content": ""}, "done": True}).encode())
        self._lines = lines
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for line in self._lines:
            yield line

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class _Harness:
    provider: Provider
    is_closed: Callable[[], bool]


def _harness(kind: str, texts: list[str]) -> _Harness:
    provider: Provider
    if kind == "gemini":
        gemini_stream = _GeminiStream(texts)
        client = _GeminiClient(_GeminiModels(gemini_stream))
        provider = GeminiProvider(
            client=client,
            types_module=types,
            model="gemini-3.8-flash",
            timeout_seconds=1,
            max_retries=0,
        )
        return _Harness(provider, lambda: gemini_stream.closed)

    byte_stream = _TrackingBytes(texts)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=byte_stream, request=request)

    provider = OllamaProvider(
        base_url="http://ollama:11434",
        model="test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return _Harness(provider, lambda: byte_stream.closed)


@pytest.mark.parametrize("kind", ["ollama", "gemini"])
@pytest.mark.parametrize(
    "texts,expected",
    [
        ([" leading", " \n", "middle", " trailing "], " leading \nmiddle trailing "),
        (["x" * 1000, " "], "x" * 1000 + " "),
        (["😀" * 1001], "😀" * 1001),
    ],
)
async def test_provider_contract_reconstructs_ordered_bounded_text(
    kind: str, texts: list[str], expected: str
) -> None:
    harness = _harness(kind, texts)
    chunks = [chunk async for chunk in harness.provider.stream(_request())]

    assert "".join(chunk.delta for chunk in chunks) == expected
    assert all(isinstance(chunk, ProviderChunk) for chunk in chunks)
    assert all(chunk.delta.strip() for chunk in chunks)
    assert all(1 <= len(chunk.delta) <= 1000 for chunk in chunks)
    assert harness.is_closed() is True


@pytest.mark.parametrize("kind", ["ollama", "gemini"])
async def test_provider_source_closes_when_consumer_stops_early(kind: str) -> None:
    harness = _harness(kind, ["first", "second"])
    iterator = cast(AsyncGenerator[ProviderChunk, None], harness.provider.stream(_request()))
    assert (await anext(iterator)).delta == "first"
    await iterator.aclose()
    assert harness.is_closed() is True


@pytest.mark.parametrize("kind", ["ollama", "gemini"])
async def test_provider_caller_cancellation_propagates_and_closes_source(kind: str) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    if kind == "gemini":
        class _BlockingGemini(_GeminiStream):
            async def __anext__(self) -> object:
                started.set()
                await release.wait()
                return SimpleNamespace(text="late")

        stream = _BlockingGemini([])
        provider: Provider = GeminiProvider(
            client=_GeminiClient(_GeminiModels(stream)),
            types_module=types,
            model="gemini-3.8-flash",
            timeout_seconds=1,
            max_retries=0,
        )
        def closed() -> bool:
            return stream.closed
    else:
        class _BlockingBytes(_TrackingBytes):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                started.set()
                await release.wait()
                yield b'unreachable'

        byte_stream = _BlockingBytes([])

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=byte_stream, request=request)

        provider = OllamaProvider(
            base_url="http://ollama:11434",
            model="test",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        def closed() -> bool:
            return byte_stream.closed

    async def collect() -> list[ProviderChunk]:
        return [chunk async for chunk in provider.stream(_request())]

    task = asyncio.create_task(collect())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed() is True
