import asyncio
import socket
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from app.retrieval import LocalRetrievalAdapter, build_citations
from app.retrieval_contracts import (
    ExactCorpusReference,
    LocalActiveScope,
    RetrievalError,
    RetrievalRequest,
)
from app.vectorstore import DocumentCollection, VectorStoreClient

_IDS = (
    "doc-a::chunk::0",
    "doc-a::chunk::1",
    "doc-b::chunk::0",
    "doc-c::chunk::0",
    "doc-d::chunk::0",
    "doc-e::chunk::0",
    "doc-f::chunk::0",
)
_DOCUMENTS = (
    " leading  and trailing ",
    "second\n\nchunk",
    "third",
    "fourth",
    "fifth",
    "sixth",
    "seventh",
)
_METADATAS = (
    {
        "document_id": "doc-a",
        "source": "a.md",
        "chunk_index": 0,
        "citation_title": " A title ",
        "citation_url": "https://example.com/a?q=1#part",
    },
    {
        "document_id": "doc-a",
        "source": "a.md",
        "chunk_index": 1,
        "citation_title": " A title ",
        "citation_url": "https://example.com/a?q=1#part",
    },
    {"document_id": "doc-b", "source": "b.md", "chunk_index": 0},
    {"document_id": "doc-c", "source": "c.md", "chunk_index": 0},
    {"document_id": "doc-d", "source": "d.md", "chunk_index": 0},
    {"document_id": "doc-e", "source": "e.md", "chunk_index": 0},
    {"document_id": "doc-f", "source": "f.md", "chunk_index": 0},
)
_EMBEDDINGS = {
    " exact query ": 0.0,
    "offset query": 0.5,
    **dict(zip(_DOCUMENTS, (0.0, -1.0, 1.0, 2.0, 3.0, 4.0, 5.0), strict=True)),
}


def _fixed_embeddings(input: Sequence[str]) -> list[list[float]]:
    return [[_EMBEDDINGS[text]] for text in input]


class ConformingMemoryCollection:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, dict[str, object], float]] = []
        self.failed = False

    def add(
        self,
        *,
        ids: Sequence[str],
        documents: Sequence[str],
        metadatas: Sequence[dict[str, object]],
    ) -> None:
        self.rows.extend(
            (identifier, document, dict(metadata), _EMBEDDINGS[document])
            for identifier, document, metadata in zip(ids, documents, metadatas, strict=True)
        )

    def clear(self) -> None:
        self.rows.clear()

    def count(self) -> int:
        if self.failed:
            raise RuntimeError("storage-secret")
        return len(self.rows)

    def query(self, *, query_texts: Sequence[str], n_results: int) -> object:
        if self.failed:
            raise RuntimeError("storage-secret")
        query = _EMBEDDINGS[query_texts[0]]
        scored = sorted(
            ((query - vector) ** 2, identifier, document, metadata)
            for identifier, document, metadata, vector in self.rows
        )[:n_results]
        return {
            "ids": [[row[1] for row in scored]],
            "documents": [[row[2] for row in scored]],
            "metadatas": [[row[3] for row in scored]],
            "distances": [[row[0] for row in scored]],
        }


class TrackingCollection:
    def __init__(self, delegate: ConformingMemoryCollection | DocumentCollection) -> None:
        self.delegate = delegate
        self.count_calls = 0
        self.query_calls: list[tuple[list[str], int]] = []

    def count(self) -> object:
        self.count_calls += 1
        return self.delegate.count()

    def query(self, *, query_texts: Sequence[str], n_results: int) -> object:
        self.query_calls.append((list(query_texts), n_results))
        return self.delegate.query(query_texts=query_texts, n_results=n_results)


@dataclass
class CollectionHarness:
    kind: str
    collection: TrackingCollection
    client: VectorStoreClient | None

    def clear(self) -> None:
        delegate = self.collection.delegate
        if isinstance(delegate, ConformingMemoryCollection):
            delegate.clear()
        else:
            delegate.delete(ids=_IDS)

    def retain(self, count: int) -> None:
        delegate = self.collection.delegate
        removed_ids = _IDS[count:]
        if isinstance(delegate, ConformingMemoryCollection):
            delegate.rows = [row for row in delegate.rows if row[0] not in removed_ids]
        else:
            delegate.delete(ids=removed_ids)

    def fail(self) -> None:
        delegate = self.collection.delegate
        if isinstance(delegate, ConformingMemoryCollection):
            delegate.failed = True
        else:
            assert self.client is not None
            self.client.close()


@pytest.fixture(autouse=True)
def no_external_retrieval_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make this local conformance suite fail immediately on provider/network use."""

    def forbidden(*_: object, **__: object) -> object:
        raise AssertionError("external call forbidden in retrieval conformance tests")

    for name in (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "OLLAMA_HOST",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr("app.providers.gemini.GeminiProvider.stream", forbidden)
    monkeypatch.setattr("app.providers.ollama.OllamaProvider.stream", forbidden)


def test_external_call_guard_denies_dns_and_connection_attempts() -> None:
    def direct_connect() -> None:
        with socket.socket() as candidate:
            candidate.connect(("127.0.0.1", 9))

    operations = (
        lambda: socket.getaddrinfo("localhost", 0),
        lambda: socket.create_connection(("localhost", 9)),
        direct_connect,
    )
    for operation in operations:
        with pytest.raises(
            AssertionError,
            match="external call forbidden in retrieval conformance tests",
        ):
            operation()


@pytest.fixture(params=("memory", "sqlite"))
def conforming_collection(
    request: pytest.FixtureRequest, tmp_path: Path
) -> Iterator[CollectionHarness]:
    kind = cast(str, request.param)
    client: VectorStoreClient | None = None
    if kind == "memory":
        delegate: ConformingMemoryCollection | DocumentCollection = ConformingMemoryCollection()
    else:
        client = VectorStoreClient(tmp_path / "conformance.sqlite3")
        delegate = client.get_or_create_collection(
            name="documents", embedding_function=_fixed_embeddings
        )
    delegate.add(
        ids=_IDS[::-1], documents=_DOCUMENTS[::-1], metadatas=_METADATAS[::-1]
    )
    harness = CollectionHarness(kind, TrackingCollection(delegate), client)
    yield harness
    if client is not None:
        client.close()


def _request(**overrides: object) -> RetrievalRequest:
    values: dict[str, object] = {
        "scope": LocalActiveScope(),
        "query": " exact query ",
        "max_results": 4,
        "max_distance": 1.2,
        "distance_measure": "squared_l2",
    }
    values.update(overrides)
    return RetrievalRequest.model_validate(values)


def _valid_result() -> dict[str, list[list[object]]]:
    return {
        "ids": [["doc-1::chunk::0"]],
        "documents": [["Thirty days."]],
        "metadatas": [[{"document_id": "doc-1", "source": "faq.md", "chunk_index": 0}]],
        "distances": [[0.25]],
    }


class MemoryCollection:
    def __init__(self, result: object | None = None, count: object = 1) -> None:
        self.result = _valid_result() if result is None else result
        self.count_value = count
        self.count_calls = 0
        self.query_calls: list[tuple[list[str], int]] = []

    def count(self) -> object:
        self.count_calls += 1
        return self.count_value

    def query(self, *, query_texts: Sequence[str], n_results: int) -> object:
        self.query_calls.append((list(query_texts), n_results))
        return self.result


async def test_shared_empty_collection_counts_once_and_never_queries(
    conforming_collection: CollectionHarness,
) -> None:
    conforming_collection.clear()
    collection = conforming_collection.collection
    result = await LocalRetrievalAdapter(collection).retrieve(_request())
    assert result.chunks == ()
    assert result.refused is True
    assert collection.count_calls == 1
    assert collection.query_calls == []


async def test_shared_nonempty_collection_clamps_query_to_collection_count(
    conforming_collection: CollectionHarness,
) -> None:
    conforming_collection.retain(2)
    collection = conforming_collection.collection

    result = await LocalRetrievalAdapter(collection).retrieve(
        _request(max_results=6)
    )

    assert [chunk.chunk_id for chunk in result.chunks] == list(_IDS[:2])
    assert collection.count_calls == 1
    assert collection.query_calls == [([" exact query "], 2)]


async def test_shared_query_bounds_order_ties_cutoffs_and_provenance(
    conforming_collection: CollectionHarness,
) -> None:
    collection = conforming_collection.collection
    result = await LocalRetrievalAdapter(collection).retrieve(_request(max_results=6))
    assert [chunk.chunk_id for chunk in result.chunks] == list(_IDS[:6])
    assert [chunk.distance for chunk in result.chunks] == [0.0, 1.0, 1.0, 4.0, 9.0, 16.0]
    assert [chunk.text for chunk in result.chunks[:3]] == list(_DOCUMENTS[:3])
    assert result.chunks[0].citation_title == " A title "
    assert result.chunks[0].citation_url == "https://example.com/a?q=1#part"
    assert result.chunks[1].citation_title == " A title "
    assert result.chunks[2].citation_title is None
    citations = build_citations(result.chunks)
    assert [citation.model_dump() for citation in citations[:2]] == [
        {
            "id": "doc-a",
            "title": " A title ",
            "url": "https://example.com/a?q=1#part",
        },
        {"id": "doc-b", "title": "b.md", "url": "document://doc-b"},
    ]
    cutoff = await LocalRetrievalAdapter(collection).retrieve(_request(max_results=2))
    assert [chunk.chunk_id for chunk in cutoff.chunks] == list(_IDS[:2])
    assert collection.query_calls == [([" exact query "], 6), ([" exact query "], 2)]


async def test_shared_exact_scope_and_non_local_metric_fail_before_store_access(
    conforming_collection: CollectionHarness,
) -> None:
    collection = conforming_collection.collection
    adapter = LocalRetrievalAdapter(collection)
    exact = _request(scope=ExactCorpusReference(corpus_id="docs", corpus_version="v1"))
    with pytest.raises(RetrievalError) as exact_error:
        await adapter.retrieve(exact)
    assert exact_error.value.code == "unsupported_scope"
    with pytest.raises(RetrievalError) as metric_error:
        await adapter.retrieve(_request(distance_measure="cosine"))
    assert metric_error.value.code == "unsupported_scope"
    assert collection.count_calls == 0
    assert collection.query_calls == []


async def test_shared_threshold_equality_and_above_cutoff(
    conforming_collection: CollectionHarness,
) -> None:
    adapter = LocalRetrievalAdapter(conforming_collection.collection)
    at_cutoff = await adapter.retrieve(_request(query="offset query", max_distance=0.25))
    above_cutoff = await adapter.retrieve(_request(query="offset query", max_distance=0.249))
    assert at_cutoff.best_distance == 0.25
    assert at_cutoff.refused is False
    assert above_cutoff.best_distance == 0.25
    assert above_cutoff.refused is True


@pytest.mark.parametrize("count", [-1, True, 1.5, "1"])
async def test_malformed_count_is_rejected(count: object) -> None:
    with pytest.raises(RetrievalError) as caught:
        await LocalRetrievalAdapter(MemoryCollection(count=count)).retrieve(_request())
    assert caught.value.code == "malformed_result"


@pytest.mark.parametrize(
    "result",
    [
        [],
        {"ids": [[]], "documents": [[]], "metadatas": [[]]},
        {"ids": [], "documents": [[]], "metadatas": [[]], "distances": [[]]},
        {"ids": [[], []], "documents": [[]], "metadatas": [[]], "distances": [[]]},
        {"ids": [["a"]], "documents": [[]], "metadatas": [[]], "distances": [[]]},
        {
            "ids": [["doc-1::chunk::0"]],
            "documents": [["text"]],
            "metadatas": [[{"document_id": "doc-1", "source": "a", "chunk_index": 0}]],
            "distances": [[True]],
        },
        {
            "ids": [["doc-1::chunk::0"]],
            "documents": [["text"]],
            "metadatas": [[{"document_id": "doc-1", "source": "a", "chunk_index": 0}]],
            "distances": [[0]],
        },
        {
            "ids": [["wrong"]],
            "documents": [["text"]],
            "metadatas": [[{"document_id": "doc-1", "source": "a", "chunk_index": 0}]],
            "distances": [[0.0]],
        },
        {
            "ids": [["doc-1::chunk::0"]],
            "documents": [["text"]],
            "metadatas": [
                [{"document_id": "doc-1", "source": "a", "chunk_index": 0, "extra": "x"}]
            ],
            "distances": [[0.0]],
        },
    ],
)
async def test_malformed_store_shapes_and_values_fail_closed(result: object) -> None:
    with pytest.raises(RetrievalError) as caught:
        await LocalRetrievalAdapter(MemoryCollection(result=result)).retrieve(_request())
    assert caught.value.code == "malformed_result"


async def test_order_duplicates_and_metadata_identity_are_validated_without_sorting() -> None:
    bad_results = [
        {
            "ids": [["a::chunk::0", "b::chunk::0"]],
            "documents": [["a", "b"]],
            "metadatas": [
                [
                    {"document_id": "a", "source": "a", "chunk_index": 0},
                    {"document_id": "b", "source": "b", "chunk_index": 0},
                ]
            ],
            "distances": [[0.2, 0.1]],
        },
        {
            "ids": [["b::chunk::0", "a::chunk::0"]],
            "documents": [["b", "a"]],
            "metadatas": [
                [
                    {"document_id": "b", "source": "b", "chunk_index": 0},
                    {"document_id": "a", "source": "a", "chunk_index": 0},
                ]
            ],
            "distances": [[0.1, 0.1]],
        },
        {
            "ids": [["doc::chunk::0", "doc::chunk::0"]],
            "documents": [["a", "b"]],
            "metadatas": [
                [
                    {"document_id": "doc", "source": "a", "chunk_index": 0},
                    {"document_id": "doc", "source": "a", "chunk_index": 0},
                ]
            ],
            "distances": [[0.1, 0.2]],
        },
        {
            "ids": [["doc::chunk::0", "doc::chunk::1"]],
            "documents": [["a", "b"]],
            "metadatas": [
                [
                    {"document_id": "doc", "source": "a", "chunk_index": 0},
                    {"document_id": "doc", "source": "b", "chunk_index": 1},
                ]
            ],
            "distances": [[0.1, 0.2]],
        },
    ]
    for raw in bad_results:
        with pytest.raises(RetrievalError) as caught:
            await LocalRetrievalAdapter(MemoryCollection(result=raw, count=2)).retrieve(
                _request(max_results=2)
            )
        assert caught.value.code == "malformed_result"


async def test_store_cannot_return_more_rows_than_requested() -> None:
    raw = {
        "ids": [[f"doc-{index}::chunk::0" for index in range(5)]],
        "documents": [["text" for _ in range(5)]],
        "metadatas": [
            [{"document_id": f"doc-{index}", "source": "a", "chunk_index": 0} for index in range(5)]
        ],
        "distances": [[float(index) for index in range(5)]],
    }
    with pytest.raises(RetrievalError) as caught:
        await LocalRetrievalAdapter(MemoryCollection(result=raw, count=5)).retrieve(_request())
    assert caught.value.code == "malformed_result"


async def test_citation_pair_is_preserved_exactly() -> None:
    raw = _valid_result()
    metadata = raw["metadatas"][0][0]
    assert isinstance(metadata, dict)
    metadata.update(
        {"citation_title": " Returns Policy ", "citation_url": "http://example.com/p?q=1#x"}
    )
    result = await LocalRetrievalAdapter(MemoryCollection(result=raw)).retrieve(_request())
    assert result.chunks[0].citation_title == " Returns Policy "
    assert result.chunks[0].citation_url == "http://example.com/p?q=1#x"


async def test_shared_readiness_is_scope_aware_and_does_not_query(
    conforming_collection: CollectionHarness,
) -> None:
    conforming_collection.clear()
    collection = conforming_collection.collection
    probe = await LocalRetrievalAdapter(collection).check_readiness(LocalActiveScope())
    assert probe.reachable and probe.store_ready and not probe.exact_version_ready
    assert collection.query_calls == []
    with pytest.raises(RetrievalError) as caught:
        await LocalRetrievalAdapter(collection).check_readiness(
            ExactCorpusReference(corpus_id="docs", corpus_version="v1")
        )
    assert caught.value.code == "unsupported_scope"


@pytest.mark.parametrize("operation", ["retrieve", "readiness"])
async def test_shared_store_errors_are_content_free(
    operation: str, conforming_collection: CollectionHarness
) -> None:
    conforming_collection.fail()
    adapter = LocalRetrievalAdapter(conforming_collection.collection)
    with pytest.raises(RetrievalError) as caught:
        if operation == "retrieve":
            await adapter.retrieve(_request())
        else:
            await adapter.check_readiness(LocalActiveScope())
    assert caught.value.code == "store_unavailable"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "storage-secret" not in str(caught.value)


@pytest.mark.parametrize("operation", ["retrieve", "readiness"])
async def test_shared_cancelled_tasks_never_enter_synchronous_collection(
    operation: str, conforming_collection: CollectionHarness
) -> None:
    adapter = LocalRetrievalAdapter(conforming_collection.collection)

    async def invoke() -> object:
        if operation == "retrieve":
            return await adapter.retrieve(_request())
        return await adapter.check_readiness(LocalActiveScope())

    task = asyncio.create_task(invoke())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert conforming_collection.collection.count_calls == 0
    assert conforming_collection.collection.query_calls == []
