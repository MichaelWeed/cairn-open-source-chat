"""Frozen wire contracts for the chat API and SSE stream.

Source of truth per DEVELOPER_README.md §4.
Change only via a PR that updates widget, tests, and docs together.
"""

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

MESSAGE_MAX_CHARS = 500
REQUEST_BODY_MAX_BYTES = 16_384
HISTORY_MAX_TURNS = 5
HISTORY_MAX_CHARS = 2_000
CONTEXT_VALUE_MAX_CHARS = 256
STATUS_LABEL_MAX_CHARS = 80
CHUNK_MAX_CHARS = 1_000
CITATIONS_MAX_COUNT = 6
CITATION_TITLE_MAX_CHARS = 160
PUBLIC_ERROR_MAX_CHARS = 240
SYSTEM_INSTRUCTION_MAX_CHARS = 4_000
RETRIEVED_CONTEXT_MAX_CHARS = 12_000
OUTPUT_TOKENS_MAX = 1_500
OUTPUT_CHARS_MAX = 6_000

_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_LOCALE_PATTERN = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_DISALLOWED_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _bounded_non_blank(value: str, *, label: str) -> str:
    if not value.strip():
        raise ValueError(f"{label} must not be blank")
    if _DISALLOWED_CONTROL_PATTERN.search(value):
        raise ValueError(f"{label} contains disallowed control characters")
    return value


def _is_none(value: object) -> bool:
    return value is None


class ContractModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ChatTurn(ContractModel):
    role: Literal["user", "assistant"]
    content: Annotated[str, Field(min_length=1, max_length=MESSAGE_MAX_CHARS)]

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        return _bounded_non_blank(value, label="content")


class ChatContext(ContractModel):
    locale: Annotated[str, Field(max_length=CONTEXT_VALUE_MAX_CHARS)] | None = Field(
        default=None, exclude_if=_is_none
    )
    page_path: Annotated[str, Field(max_length=CONTEXT_VALUE_MAX_CHARS)] | None = Field(
        default=None, exclude_if=_is_none
    )

    @field_validator("locale")
    @classmethod
    def validate_locale(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("locale must be a string when present")
        _bounded_non_blank(value, label="locale")
        if not _LOCALE_PATTERN.fullmatch(value):
            raise ValueError("locale is invalid")
        return value

    @field_validator("page_path")
    @classmethod
    def validate_page_path(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("page_path must be a string when present")
        _bounded_non_blank(value, label="page_path")
        if not value.startswith("/") or value.startswith("//"):
            raise ValueError("page_path must start with one / and may not be protocol-relative")
        return value


class ChatMessageRequest(ContractModel):
    """Body of POST /api/v1/chat/message."""

    session_id: Annotated[str, Field(min_length=1, max_length=96)]
    message: Annotated[str, Field(min_length=1, max_length=MESSAGE_MAX_CHARS)]
    history: Annotated[list[ChatTurn], Field(max_length=HISTORY_MAX_TURNS)] = []
    context: ChatContext | None = None

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        if not _SESSION_ID_PATTERN.fullmatch(value):
            raise ValueError("session_id contains invalid characters")
        return value

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        return _bounded_non_blank(value, label="message")

    @model_validator(mode="after")
    def validate_history_aggregate(self) -> "ChatMessageRequest":
        if sum(len(turn.content) for turn in self.history) > HISTORY_MAX_CHARS:
            raise ValueError(f"history exceeds {HISTORY_MAX_CHARS} total characters")
        return self


class StatusEvent(ContractModel):
    type: Literal["status"] = "status"
    state: str
    label: Annotated[str, Field(max_length=STATUS_LABEL_MAX_CHARS)]


class ChunkEvent(ContractModel):
    type: Literal["chunk"] = "chunk"
    delta: Annotated[str, Field(min_length=1, max_length=CHUNK_MAX_CHARS)]


class CitationSource(ContractModel):
    id: str
    title: Annotated[str, Field(max_length=CITATION_TITLE_MAX_CHARS)]
    url: str


class CitationsEvent(ContractModel):
    type: Literal["citations"] = "citations"
    sources: Annotated[list[CitationSource], Field(max_length=CITATIONS_MAX_COUNT)]


ErrorCode = Literal[
    "invalid_request",
    "rate_limited",
    "budget_exhausted",
    "concurrency_limited",
    "provider_timeout",
    "provider_unavailable",
    "retrieval_unavailable",
    "guardrail_block",
    "request_cancelled",
    "internal",
]


class ErrorEvent(ContractModel):
    type: Literal["error"] = "error"
    code: ErrorCode
    message: Annotated[str, Field(max_length=PUBLIC_ERROR_MAX_CHARS)]
    retryable: StrictBool


class PingEvent(ContractModel):
    type: Literal["ping"] = "ping"


class DoneEvent(ContractModel):
    type: Literal["done"] = "done"
    finish_reason: Literal["stop", "refused", "limit", "cancelled"]


ChatEvent = Annotated[
    StatusEvent | ChunkEvent | CitationsEvent | ErrorEvent | PingEvent | DoneEvent,
    Field(discriminator="type"),
]


class ProviderGenerationRequest(ContractModel):
    system_instruction: Annotated[str, Field(max_length=SYSTEM_INSTRUCTION_MAX_CHARS)]
    message: Annotated[str, Field(min_length=1, max_length=MESSAGE_MAX_CHARS)]
    history: Annotated[list[ChatTurn], Field(max_length=HISTORY_MAX_TURNS)]
    retrieved_context: Annotated[str, Field(max_length=RETRIEVED_CONTEXT_MAX_CHARS)]
    max_output_tokens: Annotated[int, Field(ge=1, le=OUTPUT_TOKENS_MAX)]
    max_output_chars: Annotated[int, Field(ge=1, le=OUTPUT_CHARS_MAX)]

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        return _bounded_non_blank(value, label="message")

    @model_validator(mode="after")
    def validate_history_aggregate(self) -> "ProviderGenerationRequest":
        if sum(len(turn.content) for turn in self.history) > HISTORY_MAX_CHARS:
            raise ValueError(f"history exceeds {HISTORY_MAX_CHARS} total characters")
        return self


class ProviderChunk(ContractModel):
    delta: Annotated[str, Field(min_length=1, max_length=CHUNK_MAX_CHARS)]

    @field_validator("delta")
    @classmethod
    def validate_delta(cls, value: str) -> str:
        return _bounded_non_blank(value, label="delta")


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
