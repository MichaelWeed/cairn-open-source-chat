"""Frozen wire contracts for the chat API and SSE stream.

Source of truth per DEVELOPER_README.md §4.
Change only via a PR that updates widget, tests, and docs together.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MESSAGE_MAX_CHARS = 500
HISTORY_MAX_TURNS = 5


class ContractModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ChatTurn(ContractModel):
    role: Literal["user", "assistant"]
    content: str


class ChatMessageRequest(ContractModel):
    """Body of POST /api/v1/chat/message."""

    session_id: str
    message: Annotated[str, Field(min_length=1, max_length=MESSAGE_MAX_CHARS)]
    history: Annotated[list[ChatTurn], Field(max_length=HISTORY_MAX_TURNS)] = []
    context: dict[str, Any] | None = None


class StatusEvent(ContractModel):
    type: Literal["status"] = "status"
    state: str
    label: str


class ChunkEvent(ContractModel):
    type: Literal["chunk"] = "chunk"
    delta: str


class CitationSource(ContractModel):
    id: str
    title: str
    url: str


class CitationsEvent(ContractModel):
    type: Literal["citations"] = "citations"
    sources: list[CitationSource]


ErrorCode = Literal["rate_limited", "provider_unavailable", "guardrail_block", "internal"]


class ErrorEvent(ContractModel):
    type: Literal["error"] = "error"
    code: ErrorCode
    message: str
    retryable: bool


class PingEvent(ContractModel):
    type: Literal["ping"] = "ping"


class DoneEvent(ContractModel):
    type: Literal["done"] = "done"
    finish_reason: str


ChatEvent = Annotated[
    StatusEvent | ChunkEvent | CitationsEvent | ErrorEvent | PingEvent | DoneEvent,
    Field(discriminator="type"),
]


class ToolCall(ContractModel):
    """A tool invocation requested by the provider, ahead of execution."""

    id: str
    name: str
    arguments: dict[str, Any]


class ToolResult(ContractModel):
    """The executed result of a ToolCall, fed back to the provider.

    `mode` carries tool-specific result variants (e.g. WISMO's
    "deep_link" | "api") without the wire contract needing to know about
    every tool the registry will eventually hold.
    """

    tool_call_id: str
    name: str
    output: dict[str, Any]
    mode: str | None = None
