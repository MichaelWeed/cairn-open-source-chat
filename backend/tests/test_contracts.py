import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from app.api.contracts import (
    ChatContext,
    ChatEvent,
    ChatMessageRequest,
    ChatTurn,
    ChunkEvent,
    CitationsEvent,
    CitationSource,
    DoneEvent,
    ErrorEvent,
    PingEvent,
    ProviderChunk,
    ProviderGenerationRequest,
    StatusEvent,
    ToolCall,
    ToolResult,
)

CHAT_EVENT_ADAPTER: TypeAdapter[ChatEvent] = TypeAdapter(ChatEvent)
FIXTURE_PATH = Path(__file__).parents[2] / "tests" / "fixtures" / "chat-contract-v1.json"


def round_trip(model: object) -> None:
    dumped = model.model_dump_json()  # type: ignore[attr-defined]
    restored = type(model).model_validate_json(dumped)  # type: ignore[attr-defined]
    assert restored == model


def test_chat_message_request_round_trip() -> None:
    req = ChatMessageRequest(
        session_id="sess-1",
        message="Where is my order?",
        history=[ChatTurn(role="user", content="hi"), ChatTurn(role="assistant", content="hello")],
        context=ChatContext(page_path="/orders"),
    )
    round_trip(req)


def test_chat_message_request_defaults() -> None:
    req = ChatMessageRequest(session_id="sess-1", message="hi")
    assert req.history == []
    assert req.context is None


def test_message_max_length_enforced() -> None:
    with pytest.raises(ValidationError):
        ChatMessageRequest(session_id="sess-1", message="x" * 501)


def test_message_min_length_enforced() -> None:
    with pytest.raises(ValidationError):
        ChatMessageRequest(session_id="sess-1", message="")


def test_history_max_turns_enforced() -> None:
    with pytest.raises(ValidationError):
        ChatMessageRequest(
            session_id="sess-1",
            message="hi",
            history=[ChatTurn(role="user", content=str(i)) for i in range(6)],
        )


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        ChatMessageRequest(session_id="sess-1", message="hi", unexpected="nope")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "event",
    [
        StatusEvent(state="retrieving", label="Searching the knowledge base"),
        ChunkEvent(delta="Hello"),
        CitationsEvent(sources=[CitationSource(id="doc-1", title="FAQ", url="https://example.com/faq")]),
        ErrorEvent(code="rate_limited", message="Slow down", retryable=True),
        PingEvent(),
        DoneEvent(finish_reason="stop"),
    ],
)
def test_sse_event_round_trip(event: ChatEvent) -> None:
    round_trip(event)


def test_chat_event_discriminated_union_round_trip() -> None:
    events: list[ChatEvent] = [
        StatusEvent(state="retrieving", label="Searching"),
        ChunkEvent(delta="Hi"),
        DoneEvent(finish_reason="stop"),
    ]
    for event in events:
        dumped = CHAT_EVENT_ADAPTER.dump_json(event)
        restored = CHAT_EVENT_ADAPTER.validate_json(dumped)
        assert restored == event


def test_error_event_rejects_unknown_code() -> None:
    with pytest.raises(ValidationError):
        ErrorEvent(code="teapot", message="nope", retryable=False)  # type: ignore[arg-type]


def test_tool_call_and_result_round_trip() -> None:
    call = ToolCall(id="call-1", name="lookup_order_status", arguments={"order_id": "123"})
    round_trip(call)

    result = ToolResult(
        tool_call_id="call-1",
        name="lookup_order_status",
        output={"url": "https://carrier.example/track/123"},
        mode="deep_link",
    )
    round_trip(result)


def test_contract_models_are_frozen() -> None:
    req = ChatMessageRequest(session_id="sess-1", message="hi")
    with pytest.raises(ValidationError):
        req.message = "changed"


def test_shared_request_and_event_fixtures() -> None:
    fixture = json.loads(FIXTURE_PATH.read_text())
    for case in fixture["valid_requests"]:
        ChatMessageRequest.model_validate(case["value"])
    for case in fixture["invalid_requests"]:
        with pytest.raises(ValidationError):
            ChatMessageRequest.model_validate(case["value"])
    for case in fixture["valid_events"]:
        CHAT_EVENT_ADAPTER.validate_python(case["event"])
    for case in fixture["invalid_events"]:
        with pytest.raises(ValidationError):
            CHAT_EVENT_ADAPTER.validate_python(case["event"])


@pytest.mark.parametrize(
    "request_data",
    [
        {"session_id": "", "message": "ok"},
        {"session_id": "x" * 97, "message": "ok"},
        {"session_id": "not valid", "message": "ok"},
        {"session_id": "s1", "message": "x" * 501},
        {"session_id": "s1", "message": "ok", "history": [{"role": "user", "content": "x" * 501}]},
        {
            "session_id": "s1",
            "message": "ok",
            "history": [
                {"role": "user", "content": "x" * 500},
                {"role": "assistant", "content": "x" * 500},
                {"role": "user", "content": "x" * 500},
                {"role": "assistant", "content": "x" * 500},
                {"role": "user", "content": "x"},
            ],
        },
        {"session_id": "s1", "message": "ok", "context": {"locale": "x" * 257}},
        {"session_id": "s1", "message": "ok", "context": {"locale": None}},
        {"session_id": "s1", "message": "ok", "context": {"page_path": None}},
        {"session_id": "s1", "message": "ok", "context": {"page_path": "relative"}},
    ],
)
def test_every_public_request_bound_is_enforced(request_data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ChatMessageRequest.model_validate(request_data)


def test_provider_generation_request_and_chunk_contract() -> None:
    request = ProviderGenerationRequest(
        system_instruction="Answer from the supplied context.",
        message="Where is my order?",
        history=[ChatTurn(role="user", content="I ordered yesterday")],
        retrieved_context="<retrieved-context>Shipment details</retrieved-context>",
        max_output_tokens=1500,
        max_output_chars=6000,
    )
    assert set(request.model_dump()) == {
        "system_instruction",
        "message",
        "history",
        "retrieved_context",
        "max_output_tokens",
        "max_output_chars",
    }
    round_trip(request)
    round_trip(ProviderChunk(delta="answer"))


@pytest.mark.parametrize(
    "changes",
    [
        {"system_instruction": "x" * 4001},
        {"retrieved_context": "x" * 12001},
        {"max_output_tokens": 0},
        {"max_output_tokens": 1501},
        {"max_output_chars": 0},
        {"max_output_chars": 6001},
        {"unexpected": True},
    ],
)
def test_provider_generation_request_bounds(changes: dict[str, object]) -> None:
    data: dict[str, object] = {
        "system_instruction": "",
        "message": "hello",
        "history": [],
        "retrieved_context": "context",
        "max_output_tokens": 1500,
        "max_output_chars": 6000,
    }
    data.update(changes)
    with pytest.raises(ValidationError):
        ProviderGenerationRequest.model_validate(data)


@pytest.mark.parametrize("delta", ["", "   ", "x" * 1001])
def test_provider_chunk_bounds(delta: str) -> None:
    with pytest.raises(ValidationError):
        ProviderChunk(delta=delta)
