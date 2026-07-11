import pytest
from pydantic import TypeAdapter, ValidationError

from app.api.contracts import (
    ChatEvent,
    ChatMessageRequest,
    ChatTurn,
    ChunkEvent,
    CitationsEvent,
    CitationSource,
    DoneEvent,
    ErrorEvent,
    PingEvent,
    StatusEvent,
    ToolCall,
    ToolResult,
)

CHAT_EVENT_ADAPTER: TypeAdapter[ChatEvent] = TypeAdapter(ChatEvent)


def round_trip(model: object) -> None:
    dumped = model.model_dump_json()  # type: ignore[attr-defined]
    restored = type(model).model_validate_json(dumped)  # type: ignore[attr-defined]
    assert restored == model


def test_chat_message_request_round_trip() -> None:
    req = ChatMessageRequest(
        session_id="sess-1",
        message="Where is my order?",
        history=[ChatTurn(role="user", content="hi"), ChatTurn(role="assistant", content="hello")],
        context={"page": "/orders"},
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
