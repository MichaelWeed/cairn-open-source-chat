import json
from datetime import date

import httpx
import pytest

from app.api.contracts import ChatTurn, ProviderGenerationRequest
from app.providers.contracts import (
    ProviderStreamEvent,
    ProviderTextChunk,
    ProviderUsageChunk,
)
from app.providers.ollama import OllamaProvider
from app.request_accounting import (
    ProviderAttemptPolicy,
    RequestAccountingError,
    RequestAccountingSession,
)


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


async def _collect_events(provider: OllamaProvider) -> list[ProviderStreamEvent]:
    return [event async for event in provider.stream(request())]


async def _collect_text(
    provider: OllamaProvider,
    request_value: ProviderGenerationRequest | None = None,
) -> list[ProviderTextChunk]:
    events = [event async for event in provider.stream(request_value or request())]
    return [event for event in events if isinstance(event, ProviderTextChunk)]


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

    chunks = await _collect_text(provider)
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

    chunks = await _collect_text(provider)
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
    chunks = await _collect_text(provider, request(history=history))
    assert [chunk.delta for chunk in chunks] == ["ok"]
    assert captured["json"] == {
        "model": "test-model",
        "stream": True,
        "options": {"num_predict": 321},
        "messages": [{"role": "user", "content": "prior"}, {"role": "user", "content": "hi"}],
    }


async def test_ollama_observer_records_start_before_http_and_completion_after_close() -> None:
    order: list[str] = []

    def handler(request_value: httpx.Request) -> httpx.Response:
        order.append("http")
        body = json.dumps(
            {
                "message": {"content": "ok"},
                "done": True,
                "prompt_eval_count": 2,
                "eval_count": 1,
            }
        ).encode()
        return httpx.Response(200, content=body, request=request_value)

    class RecordingSession(RequestAccountingSession):
        def attempt_started(self, identity):  # type: ignore[no-untyped-def]
            order.append("start")
            super().attempt_started(identity)

        def usage_observed(self, usage):  # type: ignore[no-untyped-def]
            order.append("usage")
            super().usage_observed(usage)

        def attempt_finished(self, identity, completion):  # type: ignore[no-untyped-def]
            order.append(completion)
            super().attempt_finished(identity, completion)

    session = RecordingSession(
        attempt_date=date(2026, 9, 10),
        price_snapshots=(),
        policy=ProviderAttemptPolicy(provider="ollama", model="test-model", max_attempts=1),
    )
    provider = OllamaProvider(
        base_url="http://ollama:11434",
        model="test-model",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    assert [event.kind async for event in provider.stream(request(), observer=session)] == [
        "usage",
        "text",
    ]
    assert order == ["start", "http", "usage", "completed"]
    assert session.finalize("completed").attempts[0].kind == "settled"


async def test_ollama_observer_start_failure_prevents_http() -> None:
    called = False

    def handler(request_value: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, content=b"", request=request_value)

    class FailedObserver:
        def attempt_started(self, identity: object) -> None:
            del identity
            raise RuntimeError("OBSERVER-CANARY")

    provider = OllamaProvider(
        base_url="http://ollama:11434",
        model="test-model",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(RequestAccountingError) as caught:
        async for _ in provider.stream(request(), observer=FailedObserver()):  # type: ignore[arg-type]
            pass
    assert called is False
    assert "OBSERVER-CANARY" not in str(caught.value)


async def test_ollama_prepends_context_as_system_message() -> None:
    captured: dict[str, object] = {}
    reply = json.dumps({"message": {"content": "ok"}, "done": True}).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, content=reply, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OllamaProvider(base_url="http://ollama:11434", model="test-model", client=client)

    context = "<retrieved-context>30 day returns</retrieved-context>"
    chunks = await _collect_text(
        provider,
        request(system_instruction="Be concise.", retrieved_context=context),
    )
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

    chunks = await _collect_text(provider)

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

    chunks = await _collect_text(provider)

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

    chunks = await _collect_text(provider)

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

    chunks = await _collect_text(provider)

    assert "".join(chunk.delta for chunk in chunks) == expected
    assert all(chunk.delta.strip() for chunk in chunks)
    assert all(len(chunk.delta) <= 1000 for chunk in chunks)


@pytest.mark.parametrize("trailing", [" ", " \n"])
async def test_ollama_preserves_trailing_whitespace_at_exact_chunk_boundary(
    trailing: str,
) -> None:
    expected = "x" * 1000 + trailing
    transport = _ndjson_transport(
        [
            {"message": {"content": "x" * 1000}, "done": False},
            {"message": {"content": trailing}, "done": True},
        ]
    )
    client = httpx.AsyncClient(transport=transport)
    provider = OllamaProvider(base_url="http://ollama:11434", model="test-model", client=client)

    chunks = await _collect_text(provider)

    assert "".join(chunk.delta for chunk in chunks) == expected
    assert all(chunk.delta.strip() for chunk in chunks)
    assert all(len(chunk.delta) <= 1000 for chunk in chunks)


async def test_ollama_emits_self_identifying_usage_before_same_item_text() -> None:
    transport = _ndjson_transport(
        [
            {
                "message": {"content": "answer"},
                "done": True,
                "prompt_eval_count": 12,
                "eval_count": 5,
            }
        ]
    )
    provider = OllamaProvider(
        base_url="http://ollama:11434",
        model="test-model",
        client=httpx.AsyncClient(transport=transport),
    )

    events = await _collect_events(provider)

    assert [event.kind for event in events] == ["usage", "text"]
    usage = events[0]
    assert isinstance(usage, ProviderUsageChunk)
    assert usage.provider == "ollama"
    assert usage.model == "test-model"
    assert usage.provider_attempt == 1
    assert usage.service_tier is None
    assert usage.usage.input_tokens == 12
    assert usage.usage.output_tokens == 5
    assert usage.usage.total_tokens == 17
    assert usage.usage.cached_input_tokens is None
    assert usage.usage.thinking_tokens is None


async def test_ollama_retains_partial_usage_as_missing_not_zero() -> None:
    transport = _ndjson_transport(
        [{"message": {"content": "answer"}, "done": True, "eval_count": 5}]
    )
    provider = OllamaProvider(
        base_url="http://ollama:11434",
        model="test-model",
        client=httpx.AsyncClient(transport=transport),
    )

    events = await _collect_events(provider)
    usage = events[0]
    assert isinstance(usage, ProviderUsageChunk)
    assert usage.usage.input_tokens is None
    assert usage.usage.output_tokens == 5
    assert usage.usage.total_tokens is None


async def test_ollama_emits_usage_from_textless_terminal_object() -> None:
    transport = _ndjson_transport(
        [
            {"message": {"content": "answer"}, "done": False},
            {"done": True, "prompt_eval_count": 8, "eval_count": 2},
        ]
    )
    provider = OllamaProvider(
        base_url="http://ollama:11434",
        model="test-model",
        client=httpx.AsyncClient(transport=transport),
    )

    events = await _collect_events(provider)

    assert [event.kind for event in events] == ["usage", "text"]
    usage = events[0]
    assert isinstance(usage, ProviderUsageChunk)
    assert usage.usage.total_tokens == 10


@pytest.mark.parametrize("bad", [True, -1, "5"])
async def test_ollama_rejects_malformed_usage_before_same_item_text(bad: object) -> None:
    transport = _ndjson_transport(
        [
            {
                "message": {"content": "must-not-escape"},
                "done": True,
                "prompt_eval_count": bad,
                "eval_count": 1,
            }
        ]
    )
    provider = OllamaProvider(
        base_url="http://ollama:11434",
        model="test-model",
        client=httpx.AsyncClient(transport=transport),
    )

    with pytest.raises(ValueError) as caught:
        await _collect_events(provider)

    assert "must-not-escape" not in str(caught.value)
