import json
from collections.abc import Callable
from pathlib import Path

import pytest

from app.config import Settings
from app.db import bootstrap
from app.ingest.pipeline import ingest_upload
from app.retrieval import (
    RetrievedChunk,
    build_citations,
    build_context_block,
    retrieve_chunks,
    should_refuse,
)
from app.retrieval_contracts import (
    LocalActiveScope,
    RetrievalError,
    RetrievalRequest,
    RetrievalResult,
)
from app.retrieval_integrity import compile_grounding_bundle
from app.vectorstore import DocumentCollection, get_document_collection, get_vector_client


@pytest.fixture(autouse=True)
def fake_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")


@pytest.fixture
def collection(tmp_path: Path) -> DocumentCollection:
    settings = Settings(chroma_path=tmp_path / "chroma")
    client = get_vector_client(settings)
    return get_document_collection(client, settings)


def test_retrieve_chunks_empty_collection_returns_nothing(collection: DocumentCollection) -> None:
    assert retrieve_chunks(collection, "anything") == []


def test_retrieve_chunks_returns_matching_documents(
    tmp_path: Path, collection: DocumentCollection
) -> None:
    db = bootstrap(tmp_path / "test.db")
    ingest_upload(
        db=db,
        collection=collection,
        document_id="doc-1",
        filename="faq.md",
        content=b"Our return window is 30 days from delivery.",
    )
    db.close()

    chunks = retrieve_chunks(collection, "return window", top_k=4)
    assert len(chunks) == 1
    assert chunks[0].document_id == "doc-1"
    assert chunks[0].source == "faq.md"
    assert "30 days" in chunks[0].text
    assert chunks[0].chunk_index == 0
    assert isinstance(chunks[0].distance, float)


def test_retrieve_chunks_clamps_top_k_to_collection_size(collection: DocumentCollection) -> None:
    collection.add(
        ids=["doc-1::chunk::0", "doc-2::chunk::0"],
        documents=["first chunk text", "second chunk text"],
        metadatas=[
            {"document_id": "doc-1", "source": "a.md", "chunk_index": 0},
            {"document_id": "doc-2", "source": "b.md", "chunk_index": 0},
        ],
    )
    chunks = retrieve_chunks(collection, "chunk text", top_k=6)
    assert len(chunks) == 2


def test_build_citations_dedupes_by_document_preserving_order() -> None:
    chunks = [
        RetrievedChunk(
            chunk_id="doc-2::chunk::0",
            document_id="doc-2",
            source="b.md",
            chunk_index=0,
            text="b",
            distance=0.1,
        ),
        RetrievedChunk(
            chunk_id="doc-1::chunk::0",
            document_id="doc-1",
            source="a.md",
            chunk_index=0,
            text="a",
            distance=0.2,
        ),
        RetrievedChunk(
            chunk_id="doc-2::chunk::1",
            document_id="doc-2",
            source="b.md",
            chunk_index=1,
            text="b again",
            distance=0.3,
        ),
    ]
    citations = build_citations(chunks)
    assert [c.id for c in citations] == ["doc-2", "doc-1"]
    assert citations[0].title == "b.md"
    assert citations[0].url == "document://doc-2"


def test_build_citations_empty_for_no_chunks() -> None:
    assert build_citations([]) == []


def test_build_context_block_no_chunks_uses_integrity_json_shape() -> None:
    block = build_context_block([])
    header, payload = block.split("\n", 1)
    assert "untrusted support data" in header
    assert json.loads(payload) == {
        "schema": "cairn-retrieved-context-json-v1",
        "chunks": [],
    }


def test_build_context_block_is_json_facade_over_integrity_compiler() -> None:
    chunks = [
        RetrievedChunk(
            chunk_id="doc-1::chunk::0",
            document_id="doc-1",
            source="faq.md",
            chunk_index=0,
            text="30 days",
            distance=0.1,
        )
    ]
    block = build_context_block(chunks)
    request = RetrievalRequest(
        scope=LocalActiveScope(),
        query="returns",
        max_results=1,
        max_distance=1.2,
        distance_measure="squared_l2",
    )
    compiled = compile_grounding_bundle(
        request=request,
        adapter_result=RetrievalResult(
            scope=request.scope,
            distance_measure=request.distance_measure,
            max_distance=request.max_distance,
            chunks=tuple(chunks),
        ),
    )
    assert compiled is not None
    assert block == compiled.retrieved_context
    assert "<retrieved-context>" not in block
    _, payload = block.split("\n", 1)
    assert json.loads(payload)["chunks"] == [
        {"ordinal": 0, "source": "faq.md", "text": "30 days"}
    ]


def test_build_citations_is_facade_over_integrity_compiler() -> None:
    chunks = [
        RetrievedChunk(
            chunk_id="doc-1::chunk::0",
            document_id="doc-1",
            source="faq.md",
            chunk_index=0,
            text="30 days",
            distance=0.1,
        )
    ]
    request = RetrievalRequest(
        scope=LocalActiveScope(),
        query="returns",
        max_results=1,
        max_distance=1.2,
        distance_measure="squared_l2",
    )
    compiled = compile_grounding_bundle(
        request=request,
        adapter_result=RetrievalResult(
            scope=request.scope,
            distance_measure=request.distance_measure,
            max_distance=request.max_distance,
            chunks=tuple(chunks),
        ),
    )
    assert compiled is not None
    assert build_citations(chunks) == list(compiled.citations)


@pytest.mark.parametrize(
    "helper",
    [
        lambda chunks: build_citations(chunks),
        lambda chunks: build_context_block(chunks),
        lambda chunks: should_refuse(chunks),
    ],
)
def test_compatibility_facades_normalize_forged_chunks_content_free(
    helper: Callable[[list[RetrievedChunk]], object],
) -> None:
    forged = RetrievedChunk(
        chunk_id="doc-1::chunk::0",
        document_id="doc-1",
        source="faq.md",
        chunk_index=0,
        text="30 days",
        distance=0.1,
    ).model_copy(update={"citation_title": "private-canary", "citation_url": None})

    with pytest.raises(RetrievalError) as caught:
        helper([forged])

    assert caught.value.code == "malformed_result"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "private-canary" not in str(caught.value)
    assert "private-canary" not in repr(caught.value)


def test_should_refuse_true_for_no_chunks() -> None:
    assert should_refuse([], max_distance=1.2) is True


def test_should_refuse_false_when_a_chunk_is_within_threshold() -> None:
    chunks = [
        RetrievedChunk(
            chunk_id="doc-1::chunk::0",
            document_id="doc-1",
            source="a.md",
            chunk_index=0,
            text="a",
            distance=0.9,
        ),
        RetrievedChunk(
            chunk_id="doc-2::chunk::0",
            document_id="doc-2",
            source="b.md",
            chunk_index=0,
            text="b",
            distance=5.0,
        ),
    ]
    assert should_refuse(chunks, max_distance=1.2) is False


def test_should_refuse_true_when_best_chunk_exceeds_threshold() -> None:
    chunks = [
        RetrievedChunk(
            chunk_id="doc-1::chunk::0",
            document_id="doc-1",
            source="a.md",
            chunk_index=0,
            text="a",
            distance=1.3,
        ),
        RetrievedChunk(
            chunk_id="doc-2::chunk::0",
            document_id="doc-2",
            source="b.md",
            chunk_index=0,
            text="b",
            distance=5.0,
        ),
    ]
    assert should_refuse(chunks, max_distance=1.2) is True


def test_should_refuse_uses_default_threshold_when_unspecified() -> None:
    close_chunk = [
        RetrievedChunk(
            chunk_id="doc-1::chunk::0",
            document_id="doc-1",
            source="a.md",
            chunk_index=0,
            text="a",
            distance=0.1,
        )
    ]
    far_chunk = [
        RetrievedChunk(
            chunk_id="doc-1::chunk::0",
            document_id="doc-1",
            source="a.md",
            chunk_index=0,
            text="a",
            distance=99.0,
        )
    ]
    assert should_refuse(close_chunk) is False
    assert should_refuse(far_chunk) is True
