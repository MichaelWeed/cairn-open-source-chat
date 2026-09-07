import sqlite3
from pathlib import Path

import pytest

from app.config import Settings
from app.embedding_types import EmbeddingInput, EmbeddingVectors
from app.vectorstore import DocumentCollection, get_document_collection, get_vector_client


class _FixedEmbeddings:
    def __call__(self, input: EmbeddingInput) -> EmbeddingVectors:
        vectors = {
            "origin": [0.0, 0.0],
            "left": [-1.0, 0.0],
            "right": [1.0, 0.0],
            "mismatch": [1.0],
        }
        return [vectors[text] for text in input]


@pytest.fixture(autouse=True)
def fake_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")


def test_client_creates_persistent_storage(tmp_path: Path) -> None:
    settings = Settings(chroma_path=tmp_path / "chroma")
    get_vector_client(settings)
    assert (tmp_path / "chroma").exists()


def test_collection_add_and_query_round_trip(tmp_path: Path) -> None:
    settings = Settings(chroma_path=tmp_path / "chroma")
    client = get_vector_client(settings)
    collection = get_document_collection(client, settings)

    collection.add(ids=["doc-1"], documents=["the quick brown fox"])
    assert collection.count() == 1

    result = collection.query(query_texts=["quick brown fox"], n_results=1)
    assert result["ids"][0] == ["doc-1"]


def test_get_document_collection_is_idempotent(tmp_path: Path) -> None:
    settings = Settings(chroma_path=tmp_path / "chroma")
    client = get_vector_client(settings)
    first = get_document_collection(client, settings)
    first.add(ids=["doc-1"], documents=["persisted"])

    second = get_document_collection(client, settings)
    assert second.count() == 1


def test_uses_versioned_sqlite_file_with_v1_schema(tmp_path: Path) -> None:
    settings = Settings(chroma_path=tmp_path / "chroma")
    get_vector_client(settings)

    database_path = settings.chroma_path / "cairn-vectors-v1.sqlite3"
    assert database_path.is_file()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)


def test_preserves_legacy_chroma_data_and_persists_between_clients(tmp_path: Path) -> None:
    settings = Settings(chroma_path=tmp_path / "chroma")
    settings.chroma_path.mkdir()
    legacy_file = settings.chroma_path / "chroma.sqlite3"
    legacy_file.write_text("legacy data")

    first = get_document_collection(get_vector_client(settings), settings)
    first.add(ids=["doc-1"], documents=["persisted"])
    second = get_document_collection(get_vector_client(settings), settings)

    assert legacy_file.read_text() == "legacy data"
    assert second.get(ids=["doc-1"])["documents"] == ["persisted"]


def test_add_is_atomic_for_invalid_lengths_duplicate_and_existing_ids(tmp_path: Path) -> None:
    settings = Settings(chroma_path=tmp_path / "chroma")
    collection = get_document_collection(get_vector_client(settings), settings)
    collection.add(ids=["stored"], documents=["stored document"])

    with pytest.raises(ValueError, match="equal lengths"):
        collection.add(ids=["length-1", "length-2"], documents=["one"])
    with pytest.raises(ValueError, match="duplicate"):
        collection.add(ids=["duplicate", "duplicate"], documents=["one", "two"])
    with pytest.raises(ValueError, match="already exists"):
        collection.add(ids=["stored", "new"], documents=["old", "new"])

    assert collection.count() == 1
    assert collection.get(ids=["stored", "new"])["ids"] == ["stored"]


def test_delete_get_and_query_preserve_local_collection_contract(tmp_path: Path) -> None:
    settings = Settings(chroma_path=tmp_path / "chroma")
    collection = get_document_collection(get_vector_client(settings), settings)
    collection.add(
        ids=["b", "a"],
        documents=["same", "same"],
        metadatas=[{"rank": "b"}, {"rank": "a"}],
    )

    stored = collection.get(ids=["a", "missing", "b"])
    assert stored["ids"] == ["a", "b"]
    assert stored["documents"] == ["same", "same"]

    result = collection.query(query_texts=["same", "same"], n_results=10)
    assert result["ids"] == [["a", "b"], ["a", "b"]]
    assert result["distances"] == [[0.0, 0.0], [0.0, 0.0]]

    collection.delete(ids=["a", "missing"])
    assert collection.count() == 1
    assert collection.get(ids=["a", "b"])["ids"] == ["b"]


def test_query_uses_squared_l2_tie_breaking_and_rejects_dimension_mismatch(tmp_path: Path) -> None:
    settings = Settings(chroma_path=tmp_path / "chroma")
    client = get_vector_client(settings)
    collection: DocumentCollection = client.get_or_create_collection(
        name="fixed", embedding_function=_FixedEmbeddings()
    )
    collection.add(ids=["right", "left"], documents=["right", "left"])

    result = collection.query(query_texts=["origin", "origin"], n_results=10)
    assert result["ids"] == [["left", "right"], ["left", "right"]]
    assert result["distances"] == [[1.0, 1.0], [1.0, 1.0]]

    with pytest.raises(ValueError, match="dimensions must match"):
        collection.query(query_texts=["mismatch"], n_results=1)


def test_query_rejects_malformed_stored_embedding(tmp_path: Path) -> None:
    settings = Settings(chroma_path=tmp_path / "chroma")
    collection = get_document_collection(get_vector_client(settings), settings)
    collection.add(ids=["doc-1"], documents=["stored"])
    with sqlite3.connect(settings.chroma_path / "cairn-vectors-v1.sqlite3") as connection:
        connection.execute("UPDATE vectors SET embedding = ? WHERE id = ?", ("not-json", "doc-1"))

    with pytest.raises(ValueError, match="stored embedding is malformed"):
        collection.query(query_texts=["stored"], n_results=1)
