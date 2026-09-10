"""Strict internal contracts for versioned retrieval adapters.

These models are not HTTP or SSE wire contracts. They define the bounded,
provider-independent handoff between chat orchestration and retrieval stores.
"""

import math
import re
from typing import Annotated, Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from app.api.contracts import MESSAGE_MAX_CHARS, RETRIEVED_CONTEXT_MAX_CHARS
from app.ingest.provenance import MAX_CITATION_TITLE_CHARS, MAX_MANIFEST_BYTES

RETRIEVAL_CONTRACT_VERSION = "1.0"
MAX_RETRIEVAL_RESULTS = 6
MAX_RETRIEVAL_QUERY_CHARS = MESSAGE_MAX_CHARS
MAX_RETRIEVED_CHUNK_CHARS = 3_000
MAX_RETRIEVAL_IDENTIFIER_CHARS = 8_192
MAX_RETRIEVAL_DOCUMENT_CHARS = 4_096
MAX_CORPUS_REFERENCE_CHARS = 96
MAX_CITATION_URL_CHARS = MAX_MANIFEST_BYTES
MAX_SAFE_INTEGER = 9_007_199_254_740_991

_DISALLOWED_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CORPUS_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_CORPUS_VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_MOVING_ALIASES = frozenset(
    {
        "active",
        "current",
        "default",
        "head",
        "latest",
        "main",
        "master",
        "prod",
        "production",
        "stable",
    }
)

RetrievalErrorCode = Literal[
    "invalid_request",
    "unsupported_scope",
    "store_unavailable",
    "malformed_result",
    "context_too_large",
]
DistanceMeasure = Literal["squared_l2", "euclidean", "cosine"]


def _non_blank_without_controls(value: str, label: str) -> str:
    if not value.strip():
        raise ValueError(f"{label} must not be blank")
    if _DISALLOWED_CONTROL_PATTERN.search(value):
        raise ValueError(f"{label} contains disallowed control characters")
    return value


def _finite_nonnegative(value: float, label: str) -> float:
    if isinstance(value, bool) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} must be a finite non-negative number")
    return value


class RetrievalContractModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LocalActiveScope(RetrievalContractModel):
    kind: Literal["local_active"] = "local_active"
    local_corpus_compatibility: Literal["2"] = "2"


class ExactCorpusReference(RetrievalContractModel):
    kind: Literal["exact"] = "exact"
    corpus_id: Annotated[str, Field(min_length=1, max_length=MAX_CORPUS_REFERENCE_CHARS)]
    corpus_version: Annotated[str, Field(min_length=1, max_length=MAX_CORPUS_REFERENCE_CHARS)]

    @field_validator("corpus_id")
    @classmethod
    def validate_corpus_id(cls, value: str) -> str:
        if value.lower() in _MOVING_ALIASES or _CORPUS_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("corpus_id must be a stable lowercase kebab-case identifier")
        return value

    @field_validator("corpus_version")
    @classmethod
    def validate_corpus_version(cls, value: str) -> str:
        if value.lower() in _MOVING_ALIASES or _CORPUS_VERSION_PATTERN.fullmatch(value) is None:
            raise ValueError("corpus_version must be a stable version identifier")
        return value


RetrievalScope = Annotated[
    LocalActiveScope | ExactCorpusReference,
    Field(discriminator="kind"),
]


class RetrievalRequest(RetrievalContractModel):
    contract_version: Literal["1.0"] = "1.0"
    scope: RetrievalScope
    query: Annotated[str, Field(min_length=1, max_length=MAX_RETRIEVAL_QUERY_CHARS)]
    max_results: Annotated[StrictInt, Field(ge=1, le=MAX_RETRIEVAL_RESULTS)]
    max_distance: Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
    distance_measure: DistanceMeasure

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        return _non_blank_without_controls(value, "query")

    @field_validator("max_distance")
    @classmethod
    def validate_max_distance(cls, value: float) -> float:
        return _finite_nonnegative(value, "max_distance")


class RetrievedChunk(RetrievalContractModel):
    chunk_id: Annotated[str, Field(min_length=1, max_length=MAX_RETRIEVAL_IDENTIFIER_CHARS)]
    document_id: Annotated[str, Field(min_length=1, max_length=MAX_RETRIEVAL_DOCUMENT_CHARS)]
    source: Annotated[str, Field(min_length=1, max_length=MAX_RETRIEVAL_DOCUMENT_CHARS)]
    chunk_index: Annotated[StrictInt, Field(ge=0, le=MAX_SAFE_INTEGER)]
    text: Annotated[str, Field(min_length=1, max_length=MAX_RETRIEVED_CHUNK_CHARS)]
    distance: Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
    citation_title: Annotated[str, Field(max_length=MAX_CITATION_TITLE_CHARS)] | None = None
    citation_url: Annotated[str, Field(max_length=MAX_CITATION_URL_CHARS)] | None = None

    @field_validator("chunk_id", "document_id", "source", "text", "citation_title")
    @classmethod
    def validate_text_fields(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        field_name = getattr(info, "field_name", "value")
        return _non_blank_without_controls(value, field_name)

    @field_validator("distance")
    @classmethod
    def validate_distance(cls, value: float) -> float:
        return _finite_nonnegative(value, "distance")

    @field_validator("citation_url")
    @classmethod
    def validate_citation_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if _DISALLOWED_CONTROL_PATTERN.search(value) or any(char.isspace() for char in value):
            raise ValueError("citation_url must be an absolute HTTP(S) URL")
        try:
            parsed = urlsplit(value)
            _ = parsed.port
        except ValueError as error:
            raise ValueError("citation_url must be an absolute HTTP(S) URL") from error
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("citation_url must be an absolute HTTP(S) URL")
        return value

    @model_validator(mode="after")
    def validate_citation_pair(self) -> "RetrievedChunk":
        if (self.citation_title is None) != (self.citation_url is None):
            raise ValueError("citation_title and citation_url must be present together")
        return self


class RetrievalResult(RetrievalContractModel):
    contract_version: Literal["1.0"] = "1.0"
    scope: RetrievalScope
    distance_measure: DistanceMeasure
    max_distance: Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
    chunks: Annotated[tuple[RetrievedChunk, ...], Field(max_length=MAX_RETRIEVAL_RESULTS)]

    @field_validator("max_distance")
    @classmethod
    def validate_max_distance(cls, value: float) -> float:
        return _finite_nonnegative(value, "max_distance")

    @model_validator(mode="after")
    def validate_chunk_identities(self) -> "RetrievalResult":
        chunk_ids: set[str] = set()
        positions: set[tuple[str, int]] = set()
        document_metadata: dict[str, tuple[str, str | None, str | None]] = {}
        for chunk in self.chunks:
            position = (chunk.document_id, chunk.chunk_index)
            if chunk.chunk_id in chunk_ids or position in positions:
                raise ValueError("retrieval result contains duplicate chunk identity")
            chunk_ids.add(chunk.chunk_id)
            positions.add(position)
            identity = (chunk.source, chunk.citation_title, chunk.citation_url)
            previous = document_metadata.setdefault(chunk.document_id, identity)
            if previous != identity:
                raise ValueError("retrieval result has inconsistent document metadata")
        return self

    @property
    def best_distance(self) -> float | None:
        if not self.chunks:
            return None
        return min(chunk.distance for chunk in self.chunks)

    @property
    def refused(self) -> bool:
        best_distance = self.best_distance
        return best_distance is None or best_distance > self.max_distance


class RetrievalProbe(RetrievalContractModel):
    contract_version: Literal["1.0"] = "1.0"
    scope: RetrievalScope
    reachable: StrictBool
    store_ready: StrictBool
    exact_version_ready: StrictBool

    @model_validator(mode="after")
    def validate_readiness(self) -> "RetrievalProbe":
        if not self.reachable and (self.store_ready or self.exact_version_ready):
            raise ValueError("an unreachable store cannot claim readiness")
        if isinstance(self.scope, LocalActiveScope) and self.exact_version_ready:
            raise ValueError("local_active scope cannot claim exact version readiness")
        return self


@runtime_checkable
class RetrievalAdapter(Protocol):
    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult: ...

    async def check_readiness(self, scope: RetrievalScope) -> RetrievalProbe: ...


_ERROR_MESSAGES: dict[RetrievalErrorCode, str] = {
    "invalid_request": "The retrieval request is invalid.",
    "unsupported_scope": "The requested retrieval scope is not supported.",
    "store_unavailable": "The retrieval store is unavailable.",
    "malformed_result": "The retrieval store returned an invalid result.",
    "context_too_large": "The retrieved context exceeds the supported limit.",
}


class RetrievalError(Exception):
    """Sanitized retrieval failure safe for logs and refusal control flow."""

    def __init__(self, code: RetrievalErrorCode) -> None:
        if not isinstance(code, str) or code not in _ERROR_MESSAGES:
            raise ValueError("unsupported retrieval error code") from None
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])

    def __repr__(self) -> str:
        return f"RetrievalError(code={self.code!r})"


assert RETRIEVED_CONTEXT_MAX_CHARS == 12_000
