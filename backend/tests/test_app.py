import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


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


class _BrokenChromaClient:
    def heartbeat(self) -> int:
        raise RuntimeError("simulated chroma failure")


def test_readyz_reports_unready_when_vector_store_unavailable(
    app: FastAPI, client: TestClient
) -> None:
    app.state.chroma_client = _BrokenChromaClient()
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
