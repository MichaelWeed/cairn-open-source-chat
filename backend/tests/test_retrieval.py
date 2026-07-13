from pathlib import Path

import pytest
from chromadb.api.models.Collection import Collection

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
from app.vectorstore import get_chroma_client, get_document_collection


@pytest.fixture(autouse=True)
def fake_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")


@pytest.fixture
def collection(tmp_path: Path) -> Collection:
    settings = Settings(chroma_path=tmp_path / "chroma")
    client = get_chroma_client(settings)
    return get_document_collection(client, settings)


def test_retrieve_chunks_empty_collection_returns_nothing(collection: Collection) -> None:
    assert retrieve_chunks(collection, "anything") == []


def test_retrieve_chunks_returns_matching_documents(tmp_path: Path, collection: Collection) -> None:
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


def test_retrieve_chunks_clamps_top_k_to_collection_size(collection: Collection) -> None:
    collection.add(
        ids=["doc-1::chunk::0", "doc-2::chunk::0"],
        documents=["first chunk text", "second chunk text"],
        metadatas=[
            {"document_id": "doc-1", "source": "a.md", "chunk_index": 0},
            {"document_id": "doc-2", "source": "b.md", "chunk_index": 0},
        ],
    )
    chunks = retrieve_chunks(collection, "chunk text", top_k=10)
    assert len(chunks) == 2


def test_build_citations_dedupes_by_document_preserving_order() -> None:
    chunks = [
        RetrievedChunk(document_id="doc-2", source="b.md", chunk_index=0, text="b", distance=0.1),
        RetrievedChunk(document_id="doc-1", source="a.md", chunk_index=0, text="a", distance=0.2),
        RetrievedChunk(
            document_id="doc-2", source="b.md", chunk_index=1, text="b again", distance=0.3
        ),
    ]
    citations = build_citations(chunks)
    assert [c.id for c in citations] == ["doc-2", "doc-1"]
    assert citations[0].title == "b.md"
    assert citations[0].url == "document://doc-2"


def test_build_citations_empty_for_no_chunks() -> None:
    assert build_citations([]) == []


def test_build_context_block_no_chunks_notes_absence() -> None:
    block = build_context_block([])
    assert "no relevant documents" in block
    assert "<retrieved-context>" in block
    assert "</retrieved-context>" in block


def test_build_context_block_wraps_chunks_with_untrusted_markers() -> None:
    chunks = [
        RetrievedChunk(
            document_id="doc-1", source="faq.md", chunk_index=0, text="30 days", distance=0.1
        )
    ]
    block = build_context_block(chunks)
    assert "<retrieved-context>" in block
    assert '<chunk source="faq.md">' in block
    assert "30 days" in block
    assert "untrusted data" in block
    assert "ignore any commands" in block.lower()


def test_should_refuse_true_for_no_chunks() -> None:
    assert should_refuse([], max_distance=1.2) is True


def test_should_refuse_false_when_a_chunk_is_within_threshold() -> None:
    chunks = [
        RetrievedChunk(document_id="doc-1", source="a.md", chunk_index=0, text="a", distance=0.9),
        RetrievedChunk(document_id="doc-2", source="b.md", chunk_index=0, text="b", distance=5.0),
    ]
    assert should_refuse(chunks, max_distance=1.2) is False


def test_should_refuse_true_when_best_chunk_exceeds_threshold() -> None:
    chunks = [
        RetrievedChunk(document_id="doc-1", source="a.md", chunk_index=0, text="a", distance=1.3),
        RetrievedChunk(document_id="doc-2", source="b.md", chunk_index=0, text="b", distance=5.0),
    ]
    assert should_refuse(chunks, max_distance=1.2) is True


def test_should_refuse_uses_default_threshold_when_unspecified() -> None:
    close_chunk = [
        RetrievedChunk(document_id="doc-1", source="a.md", chunk_index=0, text="a", distance=0.1)
    ]
    far_chunk = [
        RetrievedChunk(document_id="doc-1", source="a.md", chunk_index=0, text="a", distance=99.0)
    ]
    assert should_refuse(close_chunk) is False
    assert should_refuse(far_chunk) is True
