import json

import httpx
import pytest

from app.api.contracts import ChatTurn
from app.providers.ollama import OllamaProvider


def _ndjson_transport(
    lines: list[dict[str, object]], status_code: int = 200
) -> httpx.MockTransport:
    body = "\n".join(json.dumps(line) for line in lines).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=body, request=request)

    return httpx.MockTransport(handler)


async def test_ollama_streams_content_chunks() -> None:
    transport = _ndjson_transport(
        [
            {"message": {"content": "Hello"}, "done": False},
            {"message": {"content": " there"}, "done": False},
            {"message": {"content": ""}, "done": True},
        ]
    )
    client = httpx.AsyncClient(transport=transport)
    provider = OllamaProvider(base_url="http://ollama:11434", model="test-model", client=client)

    chunks = [c async for c in provider.stream(message="hi", history=[])]
    assert chunks == ["Hello", " there"]


async def test_ollama_stops_at_done() -> None:
    transport = _ndjson_transport(
        [
            {"message": {"content": "partial"}, "done": True},
            {"message": {"content": "should not appear"}, "done": False},
        ]
    )
    client = httpx.AsyncClient(transport=transport)
    provider = OllamaProvider(base_url="http://ollama:11434", model="test-model", client=client)

    chunks = [c async for c in provider.stream(message="hi", history=[])]
    assert chunks == ["partial"]


async def test_ollama_raises_on_non_2xx() -> None:
    transport = _ndjson_transport([], status_code=503)
    client = httpx.AsyncClient(transport=transport)
    provider = OllamaProvider(base_url="http://ollama:11434", model="test-model", client=client)

    with pytest.raises(httpx.HTTPStatusError):
        async for _ in provider.stream(message="hi", history=[]):
            pass


async def test_ollama_sends_expected_request_body() -> None:
    captured: dict[str, object] = {}
    reply = json.dumps({"message": {"content": "ok"}, "done": True}).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, content=reply, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OllamaProvider(base_url="http://ollama:11434/", model="test-model", client=client)

    history = [ChatTurn(role="user", content="prior")]
    chunks = [c async for c in provider.stream(message="hi", history=history)]
    assert chunks == ["ok"]
    assert captured["json"] == {
        "model": "test-model",
        "stream": True,
        "messages": [{"role": "user", "content": "prior"}, {"role": "user", "content": "hi"}],
    }


async def test_ollama_prepends_context_as_system_message() -> None:
    captured: dict[str, object] = {}
    reply = json.dumps({"message": {"content": "ok"}, "done": True}).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, content=reply, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OllamaProvider(base_url="http://ollama:11434", model="test-model", client=client)

    context = "<retrieved-context>30 day returns</retrieved-context>"
    chunks = [c async for c in provider.stream(message="hi", history=[], context=context)]
    assert chunks == ["ok"]
    assert captured["json"] == {
        "model": "test-model",
        "stream": True,
        "messages": [
            {"role": "system", "content": context},
            {"role": "user", "content": "hi"},
        ],
    }
