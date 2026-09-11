"""Bounded local retrieval adapter and chat grounding helpers."""

import asyncio
import math
from collections.abc import Sequence
from typing import Protocol, cast

from pydantic import ValidationError

from app.api.contracts import CitationSource
from app.retrieval_contracts import (
    RETRIEVAL_CONTRACT_VERSION,
    LocalActiveScope,
    RetrievalError,
    RetrievalErrorCode,
    RetrievalProbe,
    RetrievalRequest,
    RetrievalResult,
    RetrievalScope,
)
from app.retrieval_contracts import (
    RetrievedChunk as RetrievedChunk,
)
from app.retrieval_integrity import (
    CONTEXT_HEADER,
    _compatibility_citations,
    _compatibility_context,
    _compatibility_eligible_chunks,
)

DEFAULT_TOP_K = 4
DEFAULT_MAX_DISTANCE = 1.2

REFUSAL_MESSAGE = (
    "I don't have enough information in the knowledge base to answer that "
    "confidently, so I don't want to guess. Try rephrasing your question, "
    "or ask about something covered in the documentation."
)

SYSTEM_PROMPT_HEADER = CONTEXT_HEADER


class RetrievalCollection(Protocol):
    def count(self) -> object: ...

    def query(self, *, query_texts: Sequence[str], n_results: int) -> object: ...


def _validated_local_request(request: object) -> RetrievalRequest:
    validated: RetrievalRequest | None = None
    try:
        if isinstance(request, RetrievalRequest):
            request = request.model_dump()
        validated = RetrievalRequest.model_validate(request)
    except (ValidationError, TypeError, ValueError):
        pass
    if validated is None:
        raise RetrievalError("invalid_request") from None
    if validated.contract_version != RETRIEVAL_CONTRACT_VERSION:
        raise RetrievalError("invalid_request") from None
    if not isinstance(validated.scope, LocalActiveScope):
        raise RetrievalError("unsupported_scope") from None
    if validated.distance_measure != "squared_l2":
        raise RetrievalError("unsupported_scope") from None
    return validated


def _count(collection: RetrievalCollection) -> int:
    count: object = None
    failed = False
    try:
        count = collection.count()
    except Exception:
        failed = True
    if failed:
        raise RetrievalError("store_unavailable") from None
    if type(count) is not int or count < 0:
        raise RetrievalError("malformed_result") from None
    return count


def _query(collection: RetrievalCollection, request: RetrievalRequest, result_count: int) -> object:
    raw: object = None
    failed = False
    try:
        raw = collection.query(query_texts=[request.query], n_results=result_count)
    except Exception:
        failed = True
    if failed:
        raise RetrievalError("store_unavailable") from None
    return raw


def _validated_metadata(raw: object) -> dict[str, object]:
    if type(raw) is not dict:
        raise RetrievalError("malformed_result") from None
    metadata: dict[str, object] = raw
    keys = set(metadata)
    base = {"document_id", "source", "chunk_index"}
    with_citation = base | {"citation_title", "citation_url"}
    if frozenset(keys) not in {frozenset(base), frozenset(with_citation)}:
        raise RetrievalError("malformed_result") from None
    if (
        type(metadata["document_id"]) is not str
        or type(metadata["source"]) is not str
        or type(metadata["chunk_index"]) is not int
    ):
        raise RetrievalError("malformed_result") from None
    if keys == with_citation and (
        type(metadata["citation_title"]) is not str or type(metadata["citation_url"]) is not str
    ):
        raise RetrievalError("malformed_result") from None
    return metadata


def _validated_result(
    raw: object, request: RetrievalRequest, expected_count: int
) -> RetrievalResult:
    if type(raw) is not dict:
        raise RetrievalError("malformed_result") from None
    result: dict[object, object] = raw
    required_keys = {"ids", "documents", "metadatas", "distances"}
    if set(result) != required_keys:
        raise RetrievalError("malformed_result") from None

    rows: list[list[object]] = []
    for key in ("ids", "documents", "metadatas", "distances"):
        outer = result[key]
        if type(outer) is not list or len(outer) != 1 or type(outer[0]) is not list:
            raise RetrievalError("malformed_result") from None
        rows.append(outer[0])
    ids, documents, metadatas, distances = rows
    if len({len(ids), len(documents), len(metadatas), len(distances)}) != 1:
        raise RetrievalError("malformed_result") from None
    if len(ids) > expected_count:
        raise RetrievalError("malformed_result") from None

    chunks: list[RetrievedChunk] = []
    previous_order: tuple[float, str] | None = None
    for raw_id, text, raw_metadata, distance in zip(
        ids, documents, metadatas, distances, strict=True
    ):
        if type(raw_id) is not str or type(text) is not str:
            raise RetrievalError("malformed_result") from None
        if type(distance) is not float:
            raise RetrievalError("malformed_result") from None
        numeric_distance = distance
        if not math.isfinite(numeric_distance) or numeric_distance < 0:
            raise RetrievalError("malformed_result") from None
        metadata = _validated_metadata(raw_metadata)
        document_id = cast(str, metadata["document_id"])
        source = cast(str, metadata["source"])
        chunk_index = cast(int, metadata["chunk_index"])
        citation_title = cast(str | None, metadata.get("citation_title"))
        citation_url = cast(str | None, metadata.get("citation_url"))
        expected_id = f"{document_id}::chunk::{chunk_index}"
        if raw_id != expected_id:
            raise RetrievalError("malformed_result") from None
        order = (numeric_distance, raw_id)
        if previous_order is not None and order < previous_order:
            raise RetrievalError("malformed_result") from None
        previous_order = order
        chunk: RetrievedChunk | None = None
        try:
            chunk = RetrievedChunk(
                chunk_id=raw_id,
                document_id=document_id,
                source=source,
                chunk_index=chunk_index,
                text=text,
                distance=numeric_distance,
                citation_title=citation_title,
                citation_url=citation_url,
            )
        except (ValidationError, TypeError, ValueError):
            pass
        if chunk is None:
            raise RetrievalError("malformed_result") from None
        chunks.append(chunk)
    validated_result: RetrievalResult | None = None
    try:
        validated_result = RetrievalResult(
            scope=request.scope,
            distance_measure=request.distance_measure,
            max_distance=request.max_distance,
            chunks=tuple(chunks),
        )
    except (ValidationError, TypeError, ValueError):
        pass
    if validated_result is None:
        raise RetrievalError("malformed_result") from None
    return validated_result


def _retrieve(collection: RetrievalCollection, request: object) -> RetrievalResult:
    validated = _validated_local_request(request)
    count = _count(collection)
    if count == 0:
        return RetrievalResult(
            scope=validated.scope,
            distance_measure=validated.distance_measure,
            max_distance=validated.max_distance,
            chunks=(),
        )
    result_count = min(validated.max_results, count)
    return _validated_result(_query(collection, validated, result_count), validated, result_count)


class LocalRetrievalAdapter:
    """Retrieval protocol adapter for Cairn's active local SQLite collection."""

    def __init__(self, collection: RetrievalCollection) -> None:
        self._collection = collection

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        await asyncio.sleep(0)
        return _retrieve(self._collection, request)

    async def check_readiness(self, scope: RetrievalScope) -> RetrievalProbe:
        await asyncio.sleep(0)
        if not isinstance(scope, LocalActiveScope):
            raise RetrievalError("unsupported_scope") from None
        validated_scope: LocalActiveScope | None = None
        try:
            validated_scope = LocalActiveScope.model_validate(scope.model_dump())
        except (ValidationError, TypeError, ValueError):
            pass
        if validated_scope is None:
            raise RetrievalError("invalid_request") from None
        _count(self._collection)
        return RetrievalProbe(
            scope=validated_scope,
            reachable=True,
            store_ready=True,
            exact_version_ready=False,
        )


def retrieve_chunks(
    collection: RetrievalCollection, query: str, top_k: int = DEFAULT_TOP_K
) -> list[RetrievedChunk]:
    """Compatibility facade over the same strict local retrieval core."""
    request: RetrievalRequest | None = None
    try:
        request = RetrievalRequest(
            scope=LocalActiveScope(),
            query=query,
            max_results=top_k,
            max_distance=DEFAULT_MAX_DISTANCE,
            distance_measure="squared_l2",
        )
    except (ValidationError, TypeError, ValueError):
        pass
    if request is None:
        raise RetrievalError("invalid_request") from None
    return list(_retrieve(collection, request).chunks)


def should_refuse(
    chunks: Sequence[RetrievedChunk], max_distance: float = DEFAULT_MAX_DISTANCE
) -> bool:
    eligible: tuple[RetrievedChunk, ...] | None = None
    failure_code: RetrievalErrorCode | None = None
    try:
        eligible = _compatibility_eligible_chunks(chunks, max_distance)
    except RetrievalError as error:
        failure_code = error.code
    del chunks, max_distance
    if failure_code is not None:
        raise RetrievalError(failure_code) from None
    assert eligible is not None
    return not eligible


def build_citations(chunks: Sequence[RetrievedChunk]) -> list[CitationSource]:
    citations: tuple[CitationSource, ...] | None = None
    failure_code: RetrievalErrorCode | None = None
    try:
        citations = _compatibility_citations(chunks)
    except RetrievalError as error:
        failure_code = error.code
    del chunks
    if failure_code is not None:
        raise RetrievalError(failure_code) from None
    assert citations is not None
    return list(citations)


def build_context_block(chunks: Sequence[RetrievedChunk]) -> str:
    context: str | None = None
    failure_code: RetrievalErrorCode | None = None
    try:
        context = _compatibility_context(chunks)
    except RetrievalError as error:
        failure_code = error.code
    del chunks
    if failure_code is not None:
        raise RetrievalError(failure_code) from None
    assert context is not None
    return context
