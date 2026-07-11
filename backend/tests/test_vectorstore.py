from pathlib import Path

import pytest

from app.config import Settings
from app.vectorstore import get_chroma_client, get_document_collection


@pytest.fixture(autouse=True)
def fake_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")


def test_client_creates_persistent_storage(tmp_path: Path) -> None:
    settings = Settings(chroma_path=tmp_path / "chroma")
    get_chroma_client(settings)
    assert (tmp_path / "chroma").exists()


def test_collection_add_and_query_round_trip(tmp_path: Path) -> None:
    settings = Settings(chroma_path=tmp_path / "chroma")
    client = get_chroma_client(settings)
    collection = get_document_collection(client, settings)

    collection.add(ids=["doc-1"], documents=["the quick brown fox"])
    assert collection.count() == 1

    result = collection.query(query_texts=["quick brown fox"], n_results=1)
    assert result["ids"][0] == ["doc-1"]


def test_get_document_collection_is_idempotent(tmp_path: Path) -> None:
    settings = Settings(chroma_path=tmp_path / "chroma")
    client = get_chroma_client(settings)
    first = get_document_collection(client, settings)
    first.add(ids=["doc-1"], documents=["persisted"])

    second = get_document_collection(client, settings)
    assert second.count() == 1
