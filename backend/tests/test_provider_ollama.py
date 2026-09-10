import json

import httpx
import pytest

from app.api.contracts import ChatTurn, ProviderGenerationRequest
from app.providers.ollama import OllamaProvider


def request(**changes: object) -> ProviderGenerationRequest:
    data: dict[str, object] = {
        "system_instruction": "",
        "message": "hi",
        "history": [],
        "retrieved_context": "",
        "max_output_tokens": 321,
        "max_output_chars": 6000,
    }
    data.update(changes)
    return ProviderGenerationRequest.model_validate(data)


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

    chunks = [c async for c in provider.stream(request())]
    assert [chunk.delta for chunk in chunks] == ["Hello", " there"]


async def test_ollama_stops_at_done() -> None:
    transport = _ndjson_transport(
        [
            {"message": {"content": "partial"}, "done": True},
            {"message": {"content": "should not appear"}, "done": False},
        ]
    )
    client = httpx.AsyncClient(transport=transport)
    provider = OllamaProvider(base_url="http://ollama:11434", model="test-model", client=client)

    chunks = [c async for c in provider.stream(request())]
    assert [chunk.delta for chunk in chunks] == ["partial"]


async def test_ollama_raises_on_non_2xx() -> None:
    transport = _ndjson_transport([], status_code=503)
    client = httpx.AsyncClient(transport=transport)
    provider = OllamaProvider(base_url="http://ollama:11434", model="test-model", client=client)

    with pytest.raises(httpx.HTTPStatusError):
        async for _ in provider.stream(request()):
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
    chunks = [c async for c in provider.stream(request(history=history))]
    assert [chunk.delta for chunk in chunks] == ["ok"]
    assert captured["json"] == {
        "model": "test-model",
        "stream": True,
        "options": {"num_predict": 321},
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
    chunks = [
        c
        async for c in provider.stream(
            request(system_instruction="Be concise.", retrieved_context=context)
        )
    ]
    assert [chunk.delta for chunk in chunks] == ["ok"]
    assert captured["json"] == {
        "model": "test-model",
        "stream": True,
        "options": {"num_predict": 321},
        "messages": [
            {"role": "system", "content": "Be concise."},
            {"role": "system", "content": context},
            {"role": "user", "content": "hi"},
        ],
    }


async def test_ollama_splits_large_provider_deltas_to_contract_size() -> None:
    transport = _ndjson_transport([{"message": {"content": "x" * 1001}, "done": True}])
    client = httpx.AsyncClient(transport=transport)
    provider = OllamaProvider(base_url="http://ollama:11434", model="test-model", client=client)

    chunks = [chunk async for chunk in provider.stream(request())]

    assert [len(chunk.delta) for chunk in chunks] == [1000, 1]


async def test_ollama_coalesces_whitespace_only_frames_without_losing_content() -> None:
    transport = _ndjson_transport(
        [
            {"message": {"content": "Hello"}, "done": False},
            {"message": {"content": " \n"}, "done": False},
            {"message": {"content": "world"}, "done": True},
        ]
    )
    client = httpx.AsyncClient(transport=transport)
    provider = OllamaProvider(base_url="http://ollama:11434", model="test-model", client=client)

    chunks = [chunk async for chunk in provider.stream(request())]

    assert "".join(chunk.delta for chunk in chunks) == "Hello \nworld"
    assert all(chunk.delta.strip() for chunk in chunks)
    assert all(len(chunk.delta) <= 1000 for chunk in chunks)


async def test_ollama_preserves_trailing_whitespace_in_non_blank_chunk() -> None:
    transport = _ndjson_transport(
        [
            {"message": {"content": "answer"}, "done": False},
            {"message": {"content": " \n"}, "done": True},
        ]
    )
    client = httpx.AsyncClient(transport=transport)
    provider = OllamaProvider(base_url="http://ollama:11434", model="test-model", client=client)

    chunks = [chunk async for chunk in provider.stream(request())]

    assert "".join(chunk.delta for chunk in chunks) == "answer \n"
    assert all(chunk.delta.strip() for chunk in chunks)


async def test_ollama_preserves_whitespace_at_chunk_split_boundary() -> None:
    expected = "x" * 1000 + " \n" + "y"
    transport = _ndjson_transport(
        [
            {"message": {"content": "x" * 1000}, "done": False},
            {"message": {"content": " \n"}, "done": False},
            {"message": {"content": "y"}, "done": True},
        ]
    )
    client = httpx.AsyncClient(transport=transport)
    provider = OllamaProvider(base_url="http://ollama:11434", model="test-model", client=client)

    chunks = [chunk async for chunk in provider.stream(request())]

    assert "".join(chunk.delta for chunk in chunks) == expected
    assert all(chunk.delta.strip() for chunk in chunks)
    assert all(len(chunk.delta) <= 1000 for chunk in chunks)
