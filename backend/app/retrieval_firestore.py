"""Optional, exact-version Firestore retrieval adapter.

The public classes in this module deliberately expose only Cairn-owned data
shapes.  SDK objects are contained by ``FirestoreSdkVectorClient`` so the
adapter and its deterministic tests never need credentials or network access.
"""

import asyncio
import hashlib
import importlib
import inspect
import math
import numbers
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from pydantic import ValidationError

from app.embedding_types import EmbeddingFunction
from app.retrieval_contracts import (
    RETRIEVAL_CONTRACT_VERSION,
    ExactCorpusReference,
    RetrievalError,
    RetrievalProbe,
    RetrievalRequest,
    RetrievalResult,
    RetrievalScope,
    RetrievedChunk,
)

FIRESTORE_RECORD_SCHEMA_VERSION = "1.0"
FIRESTORE_DATABASE_ID = "(default)"
FIRESTORE_CHUNKS_COLLECTION = "cairn_corpus_chunks_v1"
FIRESTORE_VECTOR_FIELD = "embedding"
FIRESTORE_DISTANCE_FIELD = "_cairn_distance"

FirestoreDistanceMeasure = Literal["cosine", "euclidean"]
FirestoreFilter = tuple[str, str]
Sleeper = Callable[[float], Awaitable[None]]

_PROJECTED_FIELDS = (
    "schema_version",
    "corpus_id",
    "corpus_version",
    "embedding_identity",
    "chunk_id",
    "document_id",
    "source",
    "chunk_index",
    "text",
    "citation_title",
    "citation_url",
)


def firestore_chunk_document_id(chunk_id: str) -> str:
    digest_input = b"cairn-firestore-chunk-v1\0" + chunk_id.encode("utf-8")
    return "c1-" + hashlib.sha256(digest_input).hexdigest()


@dataclass(frozen=True, slots=True)
class FirestoreVectorQuery:
    collection: str
    filters: tuple[FirestoreFilter, ...]
    projection: tuple[str, ...]
    vector_field: str
    query_vector: tuple[float, ...]
    distance_measure: FirestoreDistanceMeasure
    distance_field: str
    limit: int
    timeout_seconds: int
    retry: None = None


@dataclass(frozen=True, slots=True)
class FirestoreReadinessQuery:
    collection: str
    filters: tuple[FirestoreFilter, ...]
    projection: tuple[str, ...]
    limit: Literal[1]
    timeout_seconds: int
    retry: None = None


@dataclass(frozen=True, slots=True)
class FirestoreVectorRow:
    document_id: str
    fields: Mapping[str, object]
    distance: object


class FirestoreClientError(Exception):
    """Content-free transport classification returned by the SDK wrapper."""

    def __init__(self, *, retryable: bool) -> None:
        self.retryable = retryable
        super().__init__("Firestore request failed")

    def __repr__(self) -> str:
        return f"FirestoreClientError(retryable={self.retryable!r})"


class FirestoreVectorClient(Protocol):
    async def vector_get(
        self, request: FirestoreVectorQuery
    ) -> Sequence[FirestoreVectorRow]: ...

    async def readiness_get(self, request: FirestoreReadinessQuery) -> None: ...

    async def aclose(self) -> None: ...


class FirestoreSdkVectorClient:
    """Translate Cairn-owned bounded requests into the optional SDK."""

    def __init__(self, client: object, *, sdk: object) -> None:
        self._client = client
        self._sdk = sdk
        self._closed = False

    def _filtered_query(self, request: FirestoreVectorQuery | FirestoreReadinessQuery) -> Any:
        query = cast(Any, self._client).collection(request.collection)
        field_filter = cast(Any, self._sdk).base_query.FieldFilter
        and_filter = cast(Any, self._sdk).base_query.And
        filters = [field_filter(field, "==", value) for field, value in request.filters]
        return query.where(filter=and_filter(filters))

    async def vector_get(
        self, request: FirestoreVectorQuery
    ) -> Sequence[FirestoreVectorRow]:
        failure: FirestoreClientError | None = None
        try:
            query = self._filtered_query(request).select(request.projection)
            distance_measure = cast(Any, self._sdk).base_vector_query.DistanceMeasure
            vector = cast(Any, self._sdk).vector.Vector(list(request.query_vector))
            sdk_measure = (
                distance_measure.COSINE
                if request.distance_measure == "cosine"
                else distance_measure.EUCLIDEAN
            )
            query = query.find_nearest(
                vector_field=request.vector_field,
                query_vector=vector,
                distance_measure=sdk_measure,
                limit=request.limit,
                distance_result_field=request.distance_field,
            )
            snapshots = await query.get(retry=request.retry, timeout=request.timeout_seconds)
            rows: list[FirestoreVectorRow] = []
            for snapshot in snapshots:
                raw = snapshot.to_dict()
                if type(raw) is not dict:
                    raise FirestoreClientError(retryable=False) from None
                fields = dict(raw)
                distance = fields.pop(request.distance_field, None)
                rows.append(
                    FirestoreVectorRow(
                        document_id=snapshot.id,
                        fields=fields,
                        distance=distance,
                    )
                )
            return tuple(rows)
        except asyncio.CancelledError:
            raise
        except FirestoreClientError:
            raise
        except Exception as error:
            failure = _normalized_sdk_error(error)
        assert failure is not None
        raise failure from None

    async def readiness_get(self, request: FirestoreReadinessQuery) -> None:
        failure: FirestoreClientError | None = None
        try:
            query = self._filtered_query(request).select(request.projection).limit(request.limit)
            await query.get(retry=request.retry, timeout=request.timeout_seconds)
            return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failure = _normalized_sdk_error(error)
        assert failure is not None
        raise failure from None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        result = cast(Any, self._client).close()
        if inspect.isawaitable(result):
            await result


def _normalized_sdk_error(error: Exception) -> FirestoreClientError:
    transient: tuple[type[BaseException], ...]
    try:
        exceptions = importlib.import_module("google.api_core.exceptions")
        transient = (
            exceptions.ServiceUnavailable,
            exceptions.DeadlineExceeded,
            exceptions.Aborted,
        )
    except (ImportError, AttributeError):
        transient = ()
    return FirestoreClientError(retryable=isinstance(error, transient))


def create_firestore_vector_client(project_id: str) -> FirestoreSdkVectorClient:
    """Create the application-owned production transport without making a request."""
    try:
        sdk = importlib.import_module("google.cloud.firestore_v1")
    except ModuleNotFoundError:
        raise RuntimeError(
            "Firestore retrieval requires the optional 'firestore' dependency profile"
        ) from None
    client = sdk.AsyncClient(project=project_id, database=FIRESTORE_DATABASE_ID)
    return FirestoreSdkVectorClient(client, sdk=sdk)


class FirestoreRetrievalAdapter:
    """Bounded exact-corpus adapter conforming to retrieval contract 1.0."""

    def __init__(
        self,
        *,
        client: FirestoreVectorClient,
        embedding_function: EmbeddingFunction,
        scope: ExactCorpusReference,
        embedding_identity: str,
        embedding_dimensions: int,
        distance_measure: FirestoreDistanceMeasure,
        timeout_seconds: int,
        max_retries: Literal[0, 1],
        owns_client: bool,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        self._client = client
        self._embedding_function = embedding_function
        self._scope = scope
        self._embedding_identity = embedding_identity
        self._embedding_dimensions = embedding_dimensions
        self._distance_measure = distance_measure
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._owns_client = owns_client
        self._sleeper = sleeper
        self._close_lock = asyncio.Lock()
        self._closed = False

    def _filters(self) -> tuple[FirestoreFilter, ...]:
        return (
            ("schema_version", FIRESTORE_RECORD_SCHEMA_VERSION),
            ("corpus_id", self._scope.corpus_id),
            ("corpus_version", self._scope.corpus_version),
            ("embedding_identity", self._embedding_identity),
        )

    def _validate_request(self, request: object) -> RetrievalRequest:
        if self._closed:
            raise RetrievalError("store_unavailable") from None
        validated: RetrievalRequest | None = None
        try:
            if isinstance(request, RetrievalRequest):
                request = request.model_dump()
            validated = RetrievalRequest.model_validate(request)
        except (ValidationError, TypeError, ValueError):
            pass
        if validated is None or validated.contract_version != RETRIEVAL_CONTRACT_VERSION:
            raise RetrievalError("invalid_request") from None
        if not isinstance(validated.scope, ExactCorpusReference):
            raise RetrievalError("unsupported_scope") from None
        if validated.scope != self._scope or validated.distance_measure != self._distance_measure:
            raise RetrievalError("unsupported_scope") from None
        return validated

    def _query_vector(self, query: str) -> tuple[float, ...]:
        vectors: object | None = None
        embedding_failed = False
        try:
            vectors = self._embedding_function([query])
        except asyncio.CancelledError:
            raise
        except Exception:
            embedding_failed = True
        if embedding_failed:
            raise RetrievalError("store_unavailable") from None
        if type(vectors) is not list or len(vectors) != 1 or type(vectors[0]) is not list:
            raise RetrievalError("malformed_result") from None
        vector = vectors[0]
        if len(vector) != self._embedding_dimensions:
            raise RetrievalError("malformed_result") from None
        normalized: list[float] = []
        for value in vector:
            if isinstance(value, bool) or not isinstance(value, numbers.Real):
                raise RetrievalError("malformed_result") from None
            normalized_value = float(value)
            if not math.isfinite(normalized_value):
                raise RetrievalError("malformed_result") from None
            normalized.append(normalized_value)
        return tuple(normalized)

    async def _attempt(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        for attempt in range(self._max_retries + 1):
            failed = False
            try:
                return await operation()
            except asyncio.CancelledError:
                raise
            except FirestoreClientError as error:
                if not error.retryable or attempt == self._max_retries:
                    failed = True
                else:
                    await self._sleeper(0.1)
            except Exception:
                failed = True
            if failed:
                raise RetrievalError("store_unavailable") from None
        raise AssertionError("unreachable")

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        validated = self._validate_request(request)
        query_vector = self._query_vector(validated.query)
        transport_request = FirestoreVectorQuery(
            collection=FIRESTORE_CHUNKS_COLLECTION,
            filters=self._filters(),
            projection=_PROJECTED_FIELDS,
            vector_field=FIRESTORE_VECTOR_FIELD,
            query_vector=query_vector,
            distance_measure=self._distance_measure,
            distance_field=FIRESTORE_DISTANCE_FIELD,
            limit=validated.max_results + 1,
            timeout_seconds=self._timeout_seconds,
        )
        rows = await self._attempt(lambda: self._client.vector_get(transport_request))
        return self._validated_result(rows, validated)

    def _validated_result(self, raw_rows: object, request: RetrievalRequest) -> RetrievalResult:
        if (
            not isinstance(raw_rows, Sequence)
            or isinstance(raw_rows, (str, bytes, bytearray, Mapping))
            or len(raw_rows) > request.max_results + 1
        ):
            raise RetrievalError("malformed_result") from None
        chunks: list[RetrievedChunk] = []
        for raw_row in raw_rows:
            if not isinstance(raw_row, FirestoreVectorRow):
                raise RetrievalError("malformed_result") from None
            fields = raw_row.fields
            if (
                type(fields) is not dict
                or len(fields) != len(_PROJECTED_FIELDS)
                or set(fields) != set(_PROJECTED_FIELDS)
            ):
                raise RetrievalError("malformed_result") from None
            if (
                fields["schema_version"] != FIRESTORE_RECORD_SCHEMA_VERSION
                or fields["corpus_id"] != self._scope.corpus_id
                or fields["corpus_version"] != self._scope.corpus_version
                or fields["embedding_identity"] != self._embedding_identity
            ):
                raise RetrievalError("malformed_result") from None
            chunk_id = fields["chunk_id"]
            if type(chunk_id) is not str or raw_row.document_id != firestore_chunk_document_id(
                chunk_id
            ):
                raise RetrievalError("malformed_result") from None
            if (
                type(raw_row.distance) is not float
                or not math.isfinite(raw_row.distance)
                or raw_row.distance < 0
            ):
                raise RetrievalError("malformed_result") from None
            chunk: RetrievedChunk | None = None
            try:
                chunk = RetrievedChunk(
                    chunk_id=chunk_id,
                    document_id=fields["document_id"],
                    source=fields["source"],
                    chunk_index=fields["chunk_index"],
                    text=fields["text"],
                    distance=raw_row.distance,
                    citation_title=fields["citation_title"],
                    citation_url=fields["citation_url"],
                )
            except (ValidationError, TypeError, ValueError):
                pass
            if chunk is None:
                raise RetrievalError("malformed_result") from None
            chunks.append(chunk)
        chunks.sort(key=lambda chunk: (chunk.distance, chunk.chunk_id))
        if len(chunks) == request.max_results + 1:
            if chunks[-2].distance == chunks[-1].distance:
                raise RetrievalError("malformed_result") from None
            chunks.pop()
        result: RetrievalResult | None = None
        try:
            result = RetrievalResult(
                scope=request.scope,
                distance_measure=request.distance_measure,
                max_distance=request.max_distance,
                chunks=tuple(chunks),
            )
        except (ValidationError, TypeError, ValueError):
            pass
        if result is None:
            raise RetrievalError("malformed_result") from None
        return result

    async def check_readiness(self, scope: RetrievalScope) -> RetrievalProbe:
        if self._closed:
            raise RetrievalError("store_unavailable") from None
        if not isinstance(scope, ExactCorpusReference):
            raise RetrievalError("unsupported_scope") from None
        validated: ExactCorpusReference | None = None
        try:
            validated = ExactCorpusReference.model_validate(scope.model_dump())
        except (ValidationError, TypeError, ValueError):
            pass
        if validated is None:
            raise RetrievalError("invalid_request") from None
        if validated != self._scope:
            raise RetrievalError("unsupported_scope") from None
        transport_request = FirestoreReadinessQuery(
            collection=FIRESTORE_CHUNKS_COLLECTION,
            filters=self._filters(),
            projection=("schema_version",),
            limit=1,
            timeout_seconds=self._timeout_seconds,
        )
        await self._attempt(lambda: self._client.readiness_get(transport_request))
        return RetrievalProbe(
            scope=validated,
            reachable=True,
            store_ready=True,
            exact_version_ready=False,
        )

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            if self._owns_client:
                try:
                    await self._client.aclose()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
