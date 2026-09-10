"""Strict internal provider stream and usage-accounting contracts."""

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from app.api.contracts import CHUNK_MAX_CHARS, ProviderChunk

PROVIDER_ACCOUNTING_SCHEMA_VERSION = "1.0"
MAX_SAFE_TOKEN_COUNT = 9_007_199_254_740_991
MAX_PROVIDER_IDENTIFIER_CHARS = 64
MAX_MODEL_IDENTIFIER_CHARS = 256
MAX_SERVICE_TIER_CHARS = 128
SNAPSHOT_ID_CHARS = 64
PRICE_SOURCE_URL_MAX_CHARS = 2_048
MAX_RATE_TOTAL_DIGITS = 38
MAX_RATE_DECIMAL_PLACES = 18

_PROVIDER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SERVICE_TIER_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
_DISALLOWED_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]")


class ProviderContractModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        hide_input_in_errors=True,
    )


def validate_provider_identifier(value: str) -> str:
    if (
        not 1 <= len(value) <= MAX_PROVIDER_IDENTIFIER_CHARS
        or _PROVIDER_PATTERN.fullmatch(value) is None
    ):
        raise ValueError("provider identifier is invalid")
    return value


def validate_model_identifier(value: str) -> str:
    if (
        not 1 <= len(value) <= MAX_MODEL_IDENTIFIER_CHARS
        or value != value.strip()
        or _DISALLOWED_CONTROL_PATTERN.search(value) is not None
    ):
        raise ValueError("model identifier is invalid")
    return value


def validate_service_tier(value: str) -> str:
    if (
        not 1 <= len(value) <= MAX_SERVICE_TIER_CHARS
        or value != value.strip()
        or _SERVICE_TIER_PATTERN.fullmatch(value) is None
    ):
        raise ValueError("service tier is invalid")
    return value


class ProviderTextChunk(ProviderContractModel):
    kind: Literal["text"] = "text"
    schema_version: Literal["1.0"] = "1.0"
    delta: Annotated[str, Field(min_length=1, max_length=CHUNK_MAX_CHARS)]

    @field_validator("delta")
    @classmethod
    def validate_delta(cls, value: str) -> str:
        return ProviderChunk(delta=value).delta


class ProviderUsage(ProviderContractModel):
    input_tokens: Annotated[
        StrictInt, Field(ge=0, le=MAX_SAFE_TOKEN_COUNT)
    ] | None = None
    cached_input_tokens: Annotated[
        StrictInt, Field(ge=0, le=MAX_SAFE_TOKEN_COUNT)
    ] | None = None
    output_tokens: Annotated[
        StrictInt, Field(ge=0, le=MAX_SAFE_TOKEN_COUNT)
    ] | None = None
    thinking_tokens: Annotated[
        StrictInt, Field(ge=0, le=MAX_SAFE_TOKEN_COUNT)
    ] | None = None
    total_tokens: Annotated[
        StrictInt, Field(ge=0, le=MAX_SAFE_TOKEN_COUNT)
    ] | None = None

    @model_validator(mode="after")
    def validate_counts(self) -> "ProviderUsage":
        values = (
            self.input_tokens,
            self.cached_input_tokens,
            self.output_tokens,
            self.thinking_tokens,
            self.total_tokens,
        )
        if all(value is None for value in values):
            raise ValueError("at least one usage count is required")
        if (
            self.cached_input_tokens is not None
            and self.input_tokens is not None
            and self.cached_input_tokens > self.input_tokens
        ):
            raise ValueError("cached input exceeds input")
        if all(
            value is not None
            for value in (
                self.input_tokens,
                self.output_tokens,
                self.thinking_tokens,
                self.total_tokens,
            )
        ):
            assert self.input_tokens is not None
            assert self.output_tokens is not None
            assert self.thinking_tokens is not None
            assert self.total_tokens is not None
            if (
                self.input_tokens + self.output_tokens + self.thinking_tokens
                != self.total_tokens
            ):
                raise ValueError("total token count is inconsistent")
        return self


class ProviderUsageChunk(ProviderContractModel):
    kind: Literal["usage"] = "usage"
    schema_version: Literal["1.0"] = "1.0"
    provider: str
    model: str
    provider_attempt: Annotated[StrictInt, Field(ge=1, le=2)]
    service_tier: str | None = None
    usage: ProviderUsage

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        return validate_provider_identifier(value)

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        return validate_model_identifier(value)

    @field_validator("service_tier")
    @classmethod
    def validate_tier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_service_tier(value)


ProviderStreamEvent = Annotated[
    ProviderTextChunk | ProviderUsageChunk,
    Field(discriminator="kind"),
]


class ProviderUsageValidationError(ValueError):
    """Content-free usage metadata failure safe for provider normalization."""

    def __init__(self) -> None:
        super().__init__("Provider usage metadata is invalid.")


def merge_cumulative_usage(
    current: ProviderUsageChunk | None,
    update: ProviderUsageChunk,
) -> ProviderUsageChunk:
    """Merge one cumulative provider usage snapshot without summing it."""

    if current is None:
        return update
    if (
        current.provider != update.provider
        or current.model != update.model
        or current.provider_attempt != update.provider_attempt
    ):
        raise ProviderUsageValidationError from None
    if (
        current.service_tier is not None
        and update.service_tier is not None
        and current.service_tier != update.service_tier
    ):
        raise ProviderUsageValidationError from None

    merged_counts: dict[str, int | None] = {}
    for field_name in ProviderUsage.model_fields:
        previous = getattr(current.usage, field_name)
        incoming = getattr(update.usage, field_name)
        if previous is not None and incoming is not None and incoming < previous:
            raise ProviderUsageValidationError from None
        merged_counts[field_name] = previous if incoming is None else incoming

    try:
        merged_usage = ProviderUsage.model_validate(merged_counts)
        return ProviderUsageChunk(
            provider=current.provider,
            model=current.model,
            provider_attempt=current.provider_attempt,
            service_tier=current.service_tier or update.service_tier,
            usage=merged_usage,
        )
    except ValueError:
        raise ProviderUsageValidationError from None
