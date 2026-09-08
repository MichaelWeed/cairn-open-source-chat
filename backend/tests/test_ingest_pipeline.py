import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.config import Settings
from app.db import bootstrap
from app.embedding_types import EmbeddingInput, EmbeddingVectors
from app.ingest.pipeline import ingest_upload
from app.vectorstore import DocumentCollection, get_document_collection, get_vector_client

Env = tuple[sqlite3.Connection, DocumentCollection]


class _FailingEmbeddings:
    def __call__(self, input: EmbeddingInput) -> EmbeddingVectors:
        del input
        raise RuntimeError("simulated embedding failure")


@pytest.fixture(autouse=True)
def fake_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    db = bootstrap(tmp_path / "test.db")
    settings = Settings(chroma_path=tmp_path / "chroma")
    client = get_vector_client(settings)
    collection = get_document_collection(client, settings)
    yield db, collection
    db.close()


def test_new_document_is_created(env: Env) -> None:
    db, collection = env
    result = ingest_upload(
        db=db, collection=collection, document_id="doc-1", filename="doc.md", content=b"hello world"
    )
    assert result.status == "created"
    assert result.chunk_count == 1
    assert collection.count() == 1


def test_reingesting_identical_bytes_is_unchanged(env: Env) -> None:
    db, collection = env
    content = b"hello world"
    ingest_upload(
        db=db, collection=collection, document_id="doc-1", filename="doc.md", content=content
    )
    result = ingest_upload(
        db=db, collection=collection, document_id="doc-1", filename="doc.md", content=content
    )
    assert result.status == "unchanged"
    assert collection.count() == 1


@pytest.mark.parametrize(
    "missing_chunk_indexes", [[0, 1], [1]], ids=["missing-all", "missing-one"]
)
def test_reingesting_identical_bytes_restores_missing_index_chunks(
    env: Env, missing_chunk_indexes: list[int]
) -> None:
    db, collection = env
    content = b"a" * 900
    created = ingest_upload(
        db=db, collection=collection, document_id="doc-1", filename="doc.md", content=content
    )
    assert created.chunk_count == 2
    expected_ids = [f"doc-1::chunk::{i}" for i in range(created.chunk_count)]
    collection.delete(ids=[expected_ids[i] for i in missing_chunk_indexes])

    result = ingest_upload(
        db=db, collection=collection, document_id="doc-1", filename="doc.md", content=content
    )

    assert result.status == "updated"
    assert result.chunk_count == created.chunk_count
    assert collection.get(ids=expected_ids)["ids"] == expected_ids


def test_reingesting_changed_content_replaces_chunks(env: Env) -> None:
    db, collection = env
    ingest_upload(
        db=db, collection=collection, document_id="doc-1", filename="doc.md", content=b"version one"
    )
    result = ingest_upload(
        db=db,
        collection=collection,
        document_id="doc-1",
        filename="doc.md",
        content=b"version two, much longer content here",
    )
    assert result.status == "updated"
    assert collection.count() == result.chunk_count

    stored = collection.get(ids=[f"doc-1::chunk::{i}" for i in range(result.chunk_count)])
    documents = stored["documents"]
    assert documents is not None
    assert all("version two" in doc for doc in documents)


def test_failed_reingestion_keeps_existing_chunks(env: Env) -> None:
    db, collection = env
    ingest_upload(
        db=db, collection=collection, document_id="doc-1", filename="doc.md", content=b"version one"
    )
    collection._embedding_function = _FailingEmbeddings()

    with pytest.raises(RuntimeError, match="simulated embedding failure"):
        ingest_upload(
            db=db,
            collection=collection,
            document_id="doc-1",
            filename="doc.md",
            content=b"version two",
        )

    assert collection.get(ids=["doc-1::chunk::0"])["documents"] == ["version one"]


def test_two_documents_are_independent(env: Env) -> None:
    db, collection = env
    ingest_upload(
        db=db, collection=collection, document_id="doc-1", filename="a.md", content=b"content a"
    )
    ingest_upload(
        db=db, collection=collection, document_id="doc-2", filename="b.md", content=b"content b"
    )
    assert collection.count() == 2

    result = ingest_upload(
        db=db, collection=collection, document_id="doc-1", filename="a.md", content=b"content a v2"
    )
    assert result.status == "updated"
    assert collection.count() == 2


def test_metadata_row_persisted(env: Env) -> None:
    db, collection = env
    ingest_upload(
        db=db, collection=collection, document_id="doc-1", filename="doc.md", content=b"hello"
    )
    row = db.execute(
        "SELECT source, chunk_count FROM documents WHERE id = ?", ("doc-1",)
    ).fetchone()
    assert row == ("doc.md", 1)


def test_chunk_metadata_includes_document_id(env: Env) -> None:
    db, collection = env
    ingest_upload(
        db=db, collection=collection, document_id="doc-1", filename="doc.md", content=b"hello"
    )
    stored = collection.get(ids=["doc-1::chunk::0"])
    metadatas = stored["metadatas"]
    assert metadatas is not None
    assert metadatas[0]["document_id"] == "doc-1"
    assert metadatas[0]["source"] == "doc.md"
