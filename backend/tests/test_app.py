import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.embedding_types import EmbeddingFunction
from app.ingest.startup import CorpusStartupError
from app.main import create_app
from app.vectorstore import DocumentCollection, VectorStoreClient, get_vector_client


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    settings = Settings(database_path=tmp_path / "test.db", chroma_path=tmp_path / "chroma")
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def test_healthz(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz(client: TestClient) -> None:
    resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"] is True
    assert body["checks"]["vector_store"] is True


class _BrokenConnection:
    def execute(self, *args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("simulated failure")

    def close(self) -> None:
        pass


def test_readyz_reports_unready_when_db_unavailable(app: FastAPI, client: TestClient) -> None:
    # Swap in a stub rather than closing the real connection: sqlite3
    # connections are thread-affine and TestClient runs the app in a
    # separate thread from the test itself.
    app.state.db = _BrokenConnection()
    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["database"] is False


class _BrokenVectorClient:
    def heartbeat(self) -> int:
        raise RuntimeError("simulated vector-store failure")


def test_readyz_reports_unready_when_vector_store_unavailable(
    app: FastAPI, client: TestClient
) -> None:
    app.state.vector_client = _BrokenVectorClient()
    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["checks"]["vector_store"] is False
    assert body["checks"]["database"] is True


def test_database_file_created(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "test.db"
    settings = Settings(database_path=db_path, chroma_path=tmp_path / "chroma")
    app = create_app(settings)
    with TestClient(app):
        assert db_path.exists()


class _TrackingVectorClient:
    def __init__(self, delegate: VectorStoreClient) -> None:
        self._delegate = delegate
        self.closed = False

    def get_or_create_collection(
        self, *, name: str, embedding_function: EmbeddingFunction
    ) -> DocumentCollection:
        return self._delegate.get_or_create_collection(
            name=name, embedding_function=embedding_function
        )

    def heartbeat(self) -> None:
        self._delegate.heartbeat()

    def close(self) -> None:
        self.closed = True
        self._delegate.close()


def test_lifespan_closes_vector_client_on_normal_shutdown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: list[_TrackingVectorClient] = []

    def tracked_client(settings: Settings) -> _TrackingVectorClient:
        client = _TrackingVectorClient(get_vector_client(settings))
        observed.append(client)
        return client

    monkeypatch.setattr("app.main.get_vector_client", tracked_client)
    app = create_app(Settings(database_path=tmp_path / "test.db", chroma_path=tmp_path / "chroma"))

    with TestClient(app):
        assert observed[0].closed is False

    assert observed[0].closed is True


def test_lifespan_closes_vector_client_after_failed_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: list[_TrackingVectorClient] = []

    def tracked_client(settings: Settings) -> _TrackingVectorClient:
        client = _TrackingVectorClient(get_vector_client(settings))
        observed.append(client)
        return client

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    monkeypatch.setattr("app.main.get_vector_client", tracked_client)
    app = create_app(
        Settings(
            database_path=tmp_path / "test.db",
            chroma_path=tmp_path / "chroma",
            corpus_path=corpus,
        )
    )

    with pytest.raises(CorpusStartupError, match="no Markdown or PDF files"):
        with TestClient(app):
            pass

    assert observed[0].closed is True
