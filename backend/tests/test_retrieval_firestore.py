import asyncio
import importlib
import importlib.util
import logging
import math
import socket
from collections.abc import MutableMapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.providers.echo import EchoProvider
from app.retrieval_contracts import (
    ExactCorpusReference,
    LocalActiveScope,
    RetrievalError,
    RetrievalRequest,
)
from app.retrieval_firestore import (
    FIRESTORE_CHUNKS_COLLECTION,
    FIRESTORE_DISTANCE_FIELD,
    FIRESTORE_RECORD_SCHEMA_VERSION,
    FIRESTORE_VECTOR_FIELD,
    FirestoreClientError,
    FirestoreReadinessQuery,
    FirestoreRetrievalAdapter,
    FirestoreSdkVectorClient,
    FirestoreVectorQuery,
    FirestoreVectorRow,
    firestore_chunk_document_id,
)

SCOPE = ExactCorpusReference(corpus_id="docs", corpus_version="v1")


def _google_auth_available() -> bool:
    try:
        return importlib.util.find_spec("google.auth") is not None
    except ModuleNotFoundError:
        return False


@pytest.fixture(autouse=True)
def no_external_firestore_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_: object, **__: object) -> object:
        raise AssertionError("external call forbidden in Firestore retrieval tests")

    for name in (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "FIRESTORE_EMULATOR_HOST",
        "OLLAMA_HOST",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    if _google_auth_available():
        monkeypatch.setattr("google.auth.default", forbidden)
    monkeypatch.setattr("app.providers.gemini.GeminiProvider.stream", forbidden)
    monkeypatch.setattr("app.providers.ollama.OllamaProvider.stream", forbidden)
    monkeypatch.setattr("app.main._default_firestore_adapter", forbidden)


def test_firestore_external_guard_denies_providers_and_factory() -> None:
    from app.main import _default_firestore_adapter
    from app.providers.gemini import GeminiProvider
    from app.providers.ollama import OllamaProvider

    operations = (
        lambda: GeminiProvider.stream(cast(GeminiProvider, object()), cast(Any, object())),
        lambda: OllamaProvider.stream(cast(OllamaProvider, object()), cast(Any, object())),
        lambda: _default_firestore_adapter(cast(Any, object())),
    )
    for operation in operations:
        with pytest.raises(
            AssertionError,
            match="external call forbidden in Firestore retrieval tests",
        ):
            operation()
    if _google_auth_available():
        with pytest.raises(
            AssertionError,
            match="external call forbidden in Firestore retrieval tests",
        ):
            __import__("google.auth").auth.default()


class FakeClient:
    def __init__(self, rows: Sequence[FirestoreVectorRow] = ()) -> None:
        self.rows = rows
        self.vector_requests: list[FirestoreVectorQuery] = []
        self.readiness_requests: list[FirestoreReadinessQuery] = []
        self.failures: list[BaseException] = []
        self.close_calls = 0

    async def vector_get(self, request: FirestoreVectorQuery) -> Sequence[FirestoreVectorRow]:
        self.vector_requests.append(request)
        if self.failures:
            raise self.failures.pop(0)
        return self.rows

    async def readiness_get(self, request: FirestoreReadinessQuery) -> None:
        self.readiness_requests.append(request)
        if self.failures:
            raise self.failures.pop(0)

    async def aclose(self) -> None:
        self.close_calls += 1


class Embeddings:
    def __init__(self, result: list[list[float]] | None = None) -> None:
        self.result = [[0.25, 0.75]] if result is None else result
        self.calls: list[list[str]] = []

    def __call__(self, values: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(values))
        return self.result


def row(
    chunk_id: str = "doc::chunk::0",
    *,
    distance: float = 0.1,
    document_id: str = "doc",
    chunk_index: int = 0,
) -> FirestoreVectorRow:
    return FirestoreVectorRow(
        document_id=firestore_chunk_document_id(chunk_id),
        fields={
            "schema_version": FIRESTORE_RECORD_SCHEMA_VERSION,
            "corpus_id": "docs",
            "corpus_version": "v1",
            "embedding_identity": "embed-v1",
            "chunk_id": chunk_id,
            "document_id": document_id,
            "source": "guide.md",
            "chunk_index": chunk_index,
            "text": " exact text ",
            "citation_title": " Exact title ",
            "citation_url": "https://example.com/guide",
        },
        distance=distance,
    )


def request(**updates: object) -> RetrievalRequest:
    values: dict[str, object] = {
        "scope": SCOPE,
        "query": " exact query ",
        "max_results": 2,
        "max_distance": 0.8,
        "distance_measure": "cosine",
    }
    values.update(updates)
    return RetrievalRequest.model_validate(values)


def adapter(
    client: FakeClient,
    embeddings: Embeddings | None = None,
    **updates: object,
) -> FirestoreRetrievalAdapter:
    values: dict[str, object] = {
        "client": client,
        "embedding_function": embeddings or Embeddings(),
        "scope": SCOPE,
        "embedding_identity": "embed-v1",
        "embedding_dimensions": 2,
        "distance_measure": "cosine",
        "timeout_seconds": 7,
        "max_retries": 0,
        "owns_client": False,
    }
    values.update(updates)
    return FirestoreRetrievalAdapter(**values)  # type: ignore[arg-type]


def firestore_settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "database_path": tmp_path / "test.db",
            "chroma_path": tmp_path / "chroma",
            "retrieval_backend": "firestore",
            "firestore_project_id": "cairn1",
            "firestore_corpus_id": "docs",
            "firestore_corpus_version": "v1",
            "firestore_embedding_identity": "embed-v1",
            "firestore_embedding_dimensions": 2,
            "firestore_distance_measure": "cosine",
            "firestore_max_distance": 0.8,
            "firestore_query_timeout_seconds": 7,
            "firestore_max_retries": 0,
        }
    )


async def test_exact_query_mapping_and_lossless_result() -> None:
    client = FakeClient([row()])
    embeddings = Embeddings()

    result = await adapter(client, embeddings).retrieve(request())

    assert embeddings.calls == [[" exact query "]]
    query = client.vector_requests[0]
    assert query.collection == FIRESTORE_CHUNKS_COLLECTION
    assert query.filters == (
        ("schema_version", FIRESTORE_RECORD_SCHEMA_VERSION),
        ("corpus_id", "docs"),
        ("corpus_version", "v1"),
        ("embedding_identity", "embed-v1"),
    )
    assert query.projection == (
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
    assert query.vector_field == FIRESTORE_VECTOR_FIELD
    assert query.distance_field == FIRESTORE_DISTANCE_FIELD
    assert query.query_vector == (0.25, 0.75)
    assert query.distance_measure == "cosine"
    assert query.limit == 3
    assert query.timeout_seconds == 7
    assert query.retry is None
    assert result.scope == SCOPE
    assert result.max_distance == 0.8
    assert result.chunks[0].text == " exact text "
    assert result.chunks[0].citation_title == " Exact title "


def test_application_selection_supplies_exact_policy_without_provider_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    transport = FakeClient()
    selected = adapter(transport, owns_client=True)
    monkeypatch.setattr("app.main._default_firestore_adapter", lambda settings: selected)
    app = create_app(firestore_settings(tmp_path), provider=EchoProvider())

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/message",
            json={"session_id": "s1", "message": " exact query "},
        )
        assert app.state.retrieval_scope == SCOPE
        assert app.state.retrieval_distance_measure == "cosine"
        assert app.state.retrieval_max_distance == 0.8

    assert response.status_code == 200
    assert len(transport.vector_requests) == 1
    assert transport.vector_requests[0].distance_measure == "cosine"
    assert transport.close_calls == 1
    assert '"finish_reason":"refused"' in response.text


def test_local_selection_never_constructs_firestore_client(tmp_path: Path) -> None:
    settings = Settings(database_path=tmp_path / "test.db", chroma_path=tmp_path / "chroma")
    app = create_app(settings, provider=EchoProvider())
    with TestClient(app):
        assert app.state.retrieval_scope == LocalActiveScope()


def test_missing_firestore_extra_has_fixed_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.retrieval_firestore as module

    def missing(name: str) -> object:
        assert name == "google.cloud.firestore_v1"
        raise ModuleNotFoundError

    monkeypatch.setattr(importlib, "import_module", missing)
    with pytest.raises(RuntimeError) as caught:
        module.create_firestore_vector_client("cairn1")
    assert str(caught.value) == (
        "Firestore retrieval requires the optional 'firestore' dependency profile"
    )


async def test_projection_is_an_exact_set_not_mapping_insertion_order() -> None:
    candidate = row()
    reordered = replace(candidate, fields=dict(reversed(tuple(candidate.fields.items()))))
    result = await adapter(FakeClient([reordered])).retrieve(request())
    assert result.chunks[0].chunk_id == "doc::chunk::0"


@pytest.mark.parametrize(
    ("bad_request", "code"),
    [
        (request(scope=LocalActiveScope()), "unsupported_scope"),
        (
            request(scope=ExactCorpusReference(corpus_id="other", corpus_version="v1")),
            "unsupported_scope",
        ),
        (request(distance_measure="euclidean"), "unsupported_scope"),
    ],
)
async def test_scope_and_measure_fail_before_embedding_or_transport(
    bad_request: RetrievalRequest, code: str
) -> None:
    client = FakeClient()
    embeddings = Embeddings()
    with pytest.raises(RetrievalError) as caught:
        await adapter(client, embeddings).retrieve(bad_request)
    assert caught.value.code == code
    assert embeddings.calls == []
    assert client.vector_requests == []


@pytest.mark.parametrize(
    "vectors",
    [[], [[1.0], [2.0]], [[1.0]], [[True, 1.0]], [[math.nan, 1.0]], [[math.inf, 1.0]]],
)
async def test_invalid_embedding_fails_before_transport(vectors: list[list[float]]) -> None:
    client = FakeClient()
    with pytest.raises(RetrievalError) as caught:
        await adapter(client, Embeddings(vectors)).retrieve(request())
    assert caught.value.code == "malformed_result"
    assert client.vector_requests == []


async def test_k_plus_one_strict_cutoff_sorts_and_truncates() -> None:
    client = FakeClient(
        [
            row("c::chunk::0", distance=0.3, document_id="c"),
            row("a::chunk::0", distance=0.1, document_id="a"),
            row("b::chunk::0", distance=0.2, document_id="b"),
        ]
    )
    result = await adapter(client).retrieve(request())
    assert [chunk.chunk_id for chunk in result.chunks] == ["a::chunk::0", "b::chunk::0"]


async def test_k_plus_one_equal_cutoff_rejects_atomically() -> None:
    client = FakeClient(
        [
            row("a::chunk::0", distance=0.1, document_id="a"),
            row("b::chunk::0", distance=0.2, document_id="b"),
            row("c::chunk::0", distance=0.2, document_id="c"),
        ]
    )
    with pytest.raises(RetrievalError) as caught:
        await adapter(client).retrieve(request())
    assert caught.value.code == "malformed_result"


async def test_equal_distances_within_k_sort_by_chunk_id() -> None:
    client = FakeClient(
        [
            row("b::chunk::0", distance=0.1, document_id="b"),
            row("a::chunk::0", distance=0.1, document_id="a"),
        ]
    )
    result = await adapter(client).retrieve(request())
    assert [chunk.chunk_id for chunk in result.chunks] == ["a::chunk::0", "b::chunk::0"]


async def test_cardinality_above_k_plus_one_rejects_atomically() -> None:
    rows = [
        row(f"doc-{index}::chunk::0", distance=float(index), document_id=f"doc-{index}")
        for index in range(4)
    ]
    with pytest.raises(RetrievalError) as caught:
        await adapter(FakeClient(rows)).retrieve(request())
    assert caught.value.code == "malformed_result"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda candidate: candidate.fields.__setitem__("extra", "x"),
        lambda candidate: candidate.fields.__delitem__("source"),
        lambda candidate: candidate.fields.__setitem__("schema_version", "2.0"),
        lambda candidate: candidate.fields.__setitem__("corpus_id", "other"),
        lambda candidate: candidate.fields.__setitem__("corpus_version", "v2"),
        lambda candidate: candidate.fields.__setitem__("embedding_identity", "other"),
        lambda candidate: candidate.fields.__setitem__("chunk_index", True),
        lambda candidate: candidate.fields.__setitem__("citation_url", None),
        lambda candidate: candidate.fields.__setitem__("text", "x" * 3_001),
    ],
)
async def test_malformed_rows_reject_without_partial_result(mutate: object) -> None:
    candidate = row()
    mutate(candidate)  # type: ignore[operator]
    with pytest.raises(RetrievalError) as caught:
        await adapter(FakeClient([candidate])).retrieve(request())
    assert caught.value.code == "malformed_result"


@pytest.mark.parametrize(
    "candidate",
    [
        replace(row(), distance=-0.1),
        replace(row(), distance=math.nan),
        replace(row(), distance=math.inf),
        replace(row(), distance=True),
        replace(row(), document_id="wrong-key"),
    ],
)
async def test_invalid_distance_and_stable_key_reject_atomically(
    candidate: FirestoreVectorRow,
) -> None:
    with pytest.raises(RetrievalError) as caught:
        await adapter(FakeClient([candidate])).retrieve(request())
    assert caught.value.code == "malformed_result"


@pytest.mark.parametrize(
    "rows",
    [
        [row("doc::chunk::0"), row("doc::chunk::0", distance=0.2)],
        [row("a::chunk::0", document_id="doc"), row("b::chunk::0", document_id="doc")],
    ],
)
async def test_duplicate_physical_or_logical_identity_rejects_atomically(
    rows: list[FirestoreVectorRow],
) -> None:
    with pytest.raises(RetrievalError) as caught:
        await adapter(FakeClient(rows)).retrieve(request())
    assert caught.value.code == "malformed_result"


async def test_conflicting_repeated_document_metadata_rejects_atomically() -> None:
    first = row("doc::chunk::0")
    second = row("doc::chunk::1", distance=0.2, chunk_index=1)
    mutable_fields = cast(MutableMapping[str, object], second.fields)
    mutable_fields["source"] = "conflict.md"
    with pytest.raises(RetrievalError) as caught:
        await adapter(FakeClient([first, second])).retrieve(request())
    assert caught.value.code == "malformed_result"


async def test_retry_is_bounded_and_uses_exact_delay() -> None:
    client = FakeClient([row()])
    client.failures.append(FirestoreClientError(retryable=True))
    sleeps: list[float] = []

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    result = await adapter(client, max_retries=1, sleeper=sleeper).retrieve(request())
    assert len(result.chunks) == 1
    assert len(client.vector_requests) == 2
    assert sleeps == [0.1]


async def test_final_transient_and_malformed_complete_result_are_not_over_retried() -> None:
    exhausted = FakeClient()
    exhausted.failures.extend(
        [FirestoreClientError(retryable=True), FirestoreClientError(retryable=True)]
    )
    sleeps: list[float] = []

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    with pytest.raises(RetrievalError) as caught:
        await adapter(exhausted, max_retries=1, sleeper=sleeper).retrieve(request())
    assert caught.value.code == "store_unavailable"
    assert len(exhausted.vector_requests) == 2
    assert sleeps == [0.1]

    malformed = FakeClient([replace(row(), distance=-1.0)])
    with pytest.raises(RetrievalError) as malformed_error:
        await adapter(malformed, max_retries=1, sleeper=sleeper).retrieve(request())
    assert malformed_error.value.code == "malformed_result"
    assert len(malformed.vector_requests) == 1


async def test_nonretryable_and_cancellation_are_not_retried() -> None:
    client = FakeClient()
    client.failures.append(FirestoreClientError(retryable=False))
    with pytest.raises(RetrievalError) as caught:
        await adapter(client, max_retries=1).retrieve(request())
    assert caught.value.code == "store_unavailable"
    assert len(client.vector_requests) == 1

    cancelled = FakeClient()
    cancelled.failures.append(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await adapter(cancelled, max_retries=1).retrieve(request())
    assert len(cancelled.vector_requests) == 1


async def test_readiness_is_content_free_and_never_claims_exact_ready() -> None:
    client = FakeClient()
    result = await adapter(client).check_readiness(SCOPE)
    assert result.model_dump() == {
        "contract_version": "1.0",
        "scope": {"kind": "exact", "corpus_id": "docs", "corpus_version": "v1"},
        "reachable": True,
        "store_ready": True,
        "exact_version_ready": False,
    }
    query = client.readiness_requests[0]
    assert query.projection == ("schema_version",)
    assert query.limit == 1
    assert query.filters == (
        ("schema_version", FIRESTORE_RECORD_SCHEMA_VERSION),
        ("corpus_id", "docs"),
        ("corpus_version", "v1"),
        ("embedding_identity", "embed-v1"),
    )
    assert query.retry is None
    assert query.timeout_seconds == 7


@pytest.mark.parametrize(
    "scope",
    [LocalActiveScope(), ExactCorpusReference(corpus_id="other", corpus_version="v1")],
)
async def test_readiness_rejects_unsupported_scope_before_transport(scope: object) -> None:
    client = FakeClient()
    with pytest.raises(RetrievalError) as caught:
        await adapter(client).check_readiness(scope)  # type: ignore[arg-type]
    assert caught.value.code == "unsupported_scope"
    assert client.readiness_requests == []


async def test_readiness_failure_retries_then_fails_content_free() -> None:
    client = FakeClient()
    client.failures.extend(
        [FirestoreClientError(retryable=True), FirestoreClientError(retryable=True)]
    )
    sleeps: list[float] = []

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    with pytest.raises(RetrievalError) as caught:
        await adapter(client, max_retries=1, sleeper=sleeper).check_readiness(SCOPE)
    assert caught.value.code == "store_unavailable"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert len(client.readiness_requests) == 2
    assert sleeps == [0.1]


async def test_cleanup_obeys_ownership_and_is_idempotent() -> None:
    owned_client = FakeClient()
    owned = adapter(owned_client, owns_client=True)
    await asyncio.gather(owned.aclose(), owned.aclose())
    assert owned_client.close_calls == 1
    with pytest.raises(RetrievalError) as caught:
        await owned.retrieve(request())
    assert caught.value.code == "store_unavailable"
    assert owned_client.vector_requests == []

    injected_client = FakeClient()
    injected = adapter(injected_client)
    await injected.aclose()
    assert injected_client.close_calls == 0


async def test_calls_after_close_never_reach_readiness_transport() -> None:
    client = FakeClient()
    candidate = adapter(client, owns_client=True)
    await candidate.aclose()
    with pytest.raises(RetrievalError) as caught:
        await candidate.check_readiness(SCOPE)
    assert caught.value.code == "store_unavailable"
    assert client.readiness_requests == []


async def test_adapter_emits_no_canary_content_at_debug_level(
    caplog: pytest.LogCaptureFixture,
) -> None:
    canaries = (
        "canary-query-secret",
        "canary-document-secret",
        "canary-url-secret.example",
        "canary-transport-secret",
    )
    candidate = row()
    mutable_fields = cast(MutableMapping[str, object], candidate.fields)
    mutable_fields["text"] = canaries[1]
    mutable_fields["citation_url"] = f"https://{canaries[2]}/x"
    client = FakeClient([candidate])
    caplog.set_level(logging.DEBUG)
    await adapter(client).retrieve(request(query=canaries[0]))
    client.failures.append(RuntimeError(canaries[3]))
    with pytest.raises(RetrievalError):
        await adapter(client).retrieve(request(query=canaries[0]))
    rendered = caplog.text
    assert all(canary not in rendered for canary in canaries)


class _SdkQuery:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.get_result: object = []
        self.failure: Exception | None = None

    def where(self, **kwargs: object) -> "_SdkQuery":
        self.calls.append(("where", kwargs))
        return self

    def select(self, value: object) -> "_SdkQuery":
        self.calls.append(("select", value))
        return self

    def find_nearest(self, **kwargs: object) -> "_SdkQuery":
        self.calls.append(("find_nearest", kwargs))
        return self

    def limit(self, value: object) -> "_SdkQuery":
        self.calls.append(("limit", value))
        return self

    async def get(self, **kwargs: object) -> object:
        self.calls.append(("get", kwargs))
        if self.failure is not None:
            raise self.failure
        return self.get_result


class _SdkClient:
    def __init__(self) -> None:
        self.query = _SdkQuery()
        self.close_calls = 0

    def collection(self, name: str) -> _SdkQuery:
        self.query.calls.append(("collection", name))
        return self.query

    async def close(self) -> None:
        self.close_calls += 1


class _Filter:
    def __init__(self, field: str, operation: str, value: str) -> None:
        self.value = (field, operation, value)


class _And:
    def __init__(self, filters: list[_Filter]) -> None:
        self.filters = filters


class _Vector:
    def __init__(self, value: list[float]) -> None:
        self.value = value


class _Sdk:
    class base_query:
        FieldFilter = _Filter
        And = _And

    class base_vector_query:
        class DistanceMeasure:
            COSINE = "COSINE"
            EUCLIDEAN = "EUCLIDEAN"

    class vector:
        Vector = _Vector


async def test_sdk_wrapper_maps_only_bounded_query_options_and_closes_once() -> None:
    client = _SdkClient()
    wrapper = FirestoreSdkVectorClient(client, sdk=_Sdk())
    await wrapper.vector_get(
        FirestoreVectorQuery(
            collection=FIRESTORE_CHUNKS_COLLECTION,
            filters=(("schema_version", "1.0"), ("corpus_id", "docs")),
            projection=("schema_version", "chunk_id"),
            vector_field=FIRESTORE_VECTOR_FIELD,
            query_vector=(0.25, 0.75),
            distance_measure="cosine",
            distance_field=FIRESTORE_DISTANCE_FIELD,
            limit=3,
            timeout_seconds=7,
        )
    )
    nearest = cast(
        dict[str, object],
        next(value for name, value in client.query.calls if name == "find_nearest"),
    )
    assert nearest == {
        "vector_field": FIRESTORE_VECTOR_FIELD,
        "query_vector": nearest["query_vector"],
        "distance_measure": "COSINE",
        "limit": 3,
        "distance_result_field": FIRESTORE_DISTANCE_FIELD,
    }
    assert cast(Any, nearest["query_vector"]).value == [0.25, 0.75]
    assert ("get", {"retry": None, "timeout": 7}) in client.query.calls
    await asyncio.gather(wrapper.aclose(), wrapper.aclose())
    assert client.close_calls == 1


@pytest.mark.parametrize("result", [[], [object()], "malformed-complete-result"])
async def test_sdk_wrapper_readiness_accepts_any_completed_bounded_read(
    result: object,
) -> None:
    client = _SdkClient()
    client.query.get_result = result
    wrapper = FirestoreSdkVectorClient(client, sdk=_Sdk())
    readiness = FirestoreReadinessQuery(
        collection=FIRESTORE_CHUNKS_COLLECTION,
        filters=(("schema_version", "1.0"), ("corpus_id", "docs")),
        projection=("schema_version",),
        limit=1,
        timeout_seconds=7,
    )

    await wrapper.readiness_get(readiness)
    assert ("select", ("schema_version",)) in client.query.calls
    assert ("limit", 1) in client.query.calls
    assert ("get", {"retry": None, "timeout": 7}) in client.query.calls
    await wrapper.aclose()
    assert client.close_calls == 1


async def test_sdk_wrapper_readiness_normalizes_failure_without_retaining_content() -> None:
    client = _SdkClient()
    client.query.failure = RuntimeError("transport-secret")
    wrapper = FirestoreSdkVectorClient(client, sdk=_Sdk())
    readiness = FirestoreReadinessQuery(
        collection=FIRESTORE_CHUNKS_COLLECTION,
        filters=(("schema_version", "1.0"),),
        projection=("schema_version",),
        limit=1,
        timeout_seconds=7,
    )

    with pytest.raises(FirestoreClientError) as caught:
        await wrapper.readiness_get(readiness)
    assert caught.value.retryable is False
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "transport-secret" not in str(caught.value)
    await wrapper.aclose()
    assert client.close_calls == 1


def test_document_id_is_stable_and_exact() -> None:
    assert firestore_chunk_document_id(" AbC ") == (
        "c1-ccbc3985cb094d2a2c4506c4e4b55bd9ac5f53d223a4fe773a5bc3677e19abc9"
    )
