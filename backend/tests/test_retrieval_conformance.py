import asyncio
from collections.abc import Sequence
from pathlib import Path

import pytest

from app.config import Settings
from app.retrieval import LocalRetrievalAdapter
from app.retrieval_contracts import (
    ExactCorpusReference,
    LocalActiveScope,
    RetrievalError,
    RetrievalRequest,
)
from app.vectorstore import (
    DocumentCollection,
    VectorStoreClient,
    get_document_collection,
    get_vector_client,
)


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


async def test_empty_collection_counts_once_and_never_queries() -> None:
    collection = MemoryCollection(count=0)
    result = await LocalRetrievalAdapter(collection).retrieve(_request())
    assert result.chunks == ()
    assert result.refused is True
    assert collection.count_calls == 1
    assert collection.query_calls == []


async def test_query_is_preserved_and_result_count_is_clamped() -> None:
    collection = MemoryCollection(count=2)
    result = await LocalRetrievalAdapter(collection).retrieve(_request(max_results=6))
    assert result.chunks[0].chunk_id == "doc-1::chunk::0"
    assert collection.query_calls == [([" exact query "], 2)]


async def test_exact_scope_and_non_local_metric_fail_before_store_access() -> None:
    collection = MemoryCollection()
    adapter = LocalRetrievalAdapter(collection)
    exact = _request(scope=ExactCorpusReference(corpus_id="docs", corpus_version="v1"))
    with pytest.raises(RetrievalError) as exact_error:
        await adapter.retrieve(exact)
    assert exact_error.value.code == "unsupported_scope"
    with pytest.raises(RetrievalError) as metric_error:
        await adapter.retrieve(_request(distance_measure="cosine"))
    assert metric_error.value.code == "unsupported_scope"
    assert collection.count_calls == 0


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


async def test_readiness_is_scope_aware_and_does_not_query() -> None:
    collection = MemoryCollection(count=0)
    probe = await LocalRetrievalAdapter(collection).check_readiness(LocalActiveScope())
    assert probe.reachable and probe.store_ready and not probe.exact_version_ready
    assert collection.query_calls == []
    with pytest.raises(RetrievalError) as caught:
        await LocalRetrievalAdapter(collection).check_readiness(
            ExactCorpusReference(corpus_id="docs", corpus_version="v1")
        )
    assert caught.value.code == "unsupported_scope"


async def test_readiness_storage_failure_is_content_free() -> None:
    with pytest.raises(RetrievalError) as caught:
        await LocalRetrievalAdapter(FailingCollection()).check_readiness(LocalActiveScope())
    assert caught.value.code == "store_unavailable"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "storage-secret" not in str(caught.value)


async def test_cancelled_task_does_not_enter_synchronous_collection() -> None:
    collection = MemoryCollection()
    task = asyncio.create_task(LocalRetrievalAdapter(collection).retrieve(_request()))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert collection.count_calls == 0


async def test_cancelled_readiness_does_not_enter_synchronous_collection() -> None:
    collection = MemoryCollection()
    task = asyncio.create_task(
        LocalRetrievalAdapter(collection).check_readiness(LocalActiveScope())
    )
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert collection.count_calls == 0


@pytest.fixture(autouse=True)
def fake_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")


async def test_real_sqlite_collection_conforms_without_external_calls(tmp_path: Path) -> None:
    settings = Settings(chroma_path=tmp_path / "chroma")
    client = get_vector_client(settings)
    collection: DocumentCollection = get_document_collection(client, settings)
    collection.add(
        ids=["doc-b::chunk::0", "doc-a::chunk::0"],
        documents=["same", "same"],
        metadatas=[
            {"document_id": "doc-b", "source": "b.md", "chunk_index": 0},
            {"document_id": "doc-a", "source": "a.md", "chunk_index": 0},
        ],
    )
    result = await LocalRetrievalAdapter(collection).retrieve(_request(max_results=2))
    assert [chunk.chunk_id for chunk in result.chunks] == [
        "doc-a::chunk::0",
        "doc-b::chunk::0",
    ]
    client.close()


@pytest.mark.parametrize("collection_kind", ["memory", "sqlite"])
async def test_shared_collection_conformance_preserves_order_text_and_provenance(
    collection_kind: str, tmp_path: Path
) -> None:
    ids = ["doc-a::chunk::0", "doc-a::chunk::1", "doc-b::chunk::0"]
    documents = [" leading  and trailing ", "second\n\nchunk", "third"]
    metadatas = [
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
    ]
    client: VectorStoreClient | None = None
    if collection_kind == "memory":
        collection: MemoryCollection | DocumentCollection = MemoryCollection(
            result={
                "ids": [ids],
                "documents": [documents],
                "metadatas": [metadatas],
                "distances": [[0.0, 0.0, 0.0]],
            },
            count=3,
        )
    else:
        client = VectorStoreClient(tmp_path / "fixed.sqlite3")
        collection = client.get_or_create_collection(
            name="documents",
            embedding_function=lambda texts: [[0.0, 0.0] for _ in texts],
        )
        collection.add(ids=ids[::-1], documents=documents[::-1], metadatas=metadatas[::-1])

    result = await LocalRetrievalAdapter(collection).retrieve(_request(max_results=3))

    assert [chunk.chunk_id for chunk in result.chunks] == ids
    assert [chunk.text for chunk in result.chunks] == documents
    assert result.chunks[0].citation_title == " A title "
    assert result.chunks[0].citation_url == "https://example.com/a?q=1#part"
    assert result.chunks[2].citation_title is None
    assert result.chunks[2].citation_url is None
    assert result.best_distance == 0.0
    assert result.refused is False
    if client is not None:
        client.close()


class FailingCollection(MemoryCollection):
    def count(self) -> object:
        raise RuntimeError("storage-secret")


async def test_store_exception_is_sanitized_without_retaining_cause() -> None:
    with pytest.raises(RetrievalError) as caught:
        await LocalRetrievalAdapter(FailingCollection()).retrieve(_request())
    assert caught.value.code == "store_unavailable"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "storage-secret" not in str(caught.value)
