import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.contracts import ChatTurn
from app.config import Settings
from app.db import bootstrap
from app.ingest.pipeline import ingest_upload
from app.main import create_app
from app.providers.base import Provider
from app.providers.echo import EchoProvider


@pytest.fixture(autouse=True)
def fake_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")


class FailingProvider(Provider):
    async def stream(
        self, *, message: str, history: list[ChatTurn], context: str | None = None
    ) -> AsyncIterator[str]:
        raise ConnectionError("simulated provider outage")
        yield  # pragma: no cover - unreachable, satisfies the generator type


class CapturingProvider(Provider):
    """Records the last call's arguments instead of talking to a model —
    lets a test assert *what the provider actually received*, which is the
    only way to prove retrieval (task 2.4) reaches the provider rather than
    just running and being discarded."""

    def __init__(self) -> None:
        self.last_context: str | None = None

    async def stream(
        self, *, message: str, history: list[ChatTurn], context: str | None = None
    ) -> AsyncIterator[str]:
        self.last_context = context
        yield "ok"


def parse_sse(body: str) -> list[tuple[str, str]]:
    events = []
    for block in body.strip("\n").split("\n\n"):
        lines = block.splitlines()
        event_type = next(
            line.removeprefix("event: ") for line in lines if line.startswith("event: ")
        )
        data = next(line.removeprefix("data: ") for line in lines if line.startswith("data: "))
        events.append((event_type, data))
    return events


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    settings = Settings(
        database_path=tmp_path / "test.db",
        chroma_path=tmp_path / "chroma",
        origin_allowlist="http://widget.example",
    )
    return create_app(settings, provider=EchoProvider())


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _ingest(
    app: FastAPI, settings: Settings, filename: str, content: bytes, document_id: str = "doc-1"
) -> None:
    """Ingest via the app's own in-process collection handle (task 2.7's
    constraint) — `app.state.document_collection` only exists once the
    app's lifespan has started, so call this from inside a `with
    TestClient(app):` block, never against a bare `app`.

    app.state.db belongs to the lifespan's thread; sqlite3 connections
    aren't shareable across threads, so ingest through our own connection
    to the same file rather than reusing app.state.db.
    """
    ingest_db = bootstrap(settings.database_path)
    ingest_upload(
        db=ingest_db,
        collection=app.state.document_collection,
        document_id=document_id,
        filename=filename,
        content=content,
    )
    ingest_db.close()


@pytest.fixture
def grounded_app(tmp_path: Path) -> FastAPI:
    # FakeEmbeddingFunction hashes whole documents into vectors that aren't
    # semantically meaningful (see app/embeddings/fake.py) — its L2
    # distances don't correlate with textual relevance, so tests that want
    # to exercise the "confident enough to answer" path set max_distance
    # generously rather than relying on the fake embedder to score a real
    # match as close.
    settings = Settings(
        database_path=tmp_path / "test.db",
        chroma_path=tmp_path / "chroma",
        origin_allowlist="http://widget.example",
        retrieval_max_distance=1000.0,
    )
    app = create_app(settings, provider=EchoProvider())
    with TestClient(app):
        _ingest(app, settings, "faq.md", b"Our return window is 30 days from delivery.")
    return app


@pytest.fixture
def grounded_client(grounded_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(grounded_app) as c:
        yield c


def test_happy_path_round_trip(grounded_client: TestClient) -> None:
    resp = grounded_client.post(
        "/api/v1/chat/message",
        json={"session_id": "s1", "message": "hi there"},
        headers={"origin": "http://widget.example"},
    )
    assert resp.status_code == 200
    events = parse_sse(resp.text)
    types = [t for t, _ in events]
    assert types[0] == "status"
    assert "chunk" in types
    assert types[-1] == "done"


def test_chunks_reconstruct_message(grounded_client: TestClient) -> None:
    resp = grounded_client.post(
        "/api/v1/chat/message",
        json={"session_id": "s1", "message": "hello world"},
        headers={"origin": "http://widget.example"},
    )
    events = parse_sse(resp.text)
    deltas = [json.loads(data)["delta"] for t, data in events if t == "chunk"]
    assert "".join(deltas) == "hello world"


def test_refuses_and_skips_provider_when_nothing_ingested(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/chat/message",
        json={"session_id": "s1", "message": "hi there"},
        headers={"origin": "http://widget.example"},
    )
    assert resp.status_code == 200
    events = parse_sse(resp.text)
    types = [t for t, _ in events]
    assert "citations" not in types

    deltas = [json.loads(data)["delta"] for t, data in events if t == "chunk"]
    assert "".join(deltas) != "hi there"  # not an echo — the provider was never called
    assert "don't have enough information" in "".join(deltas)

    done_body = json.loads(next(data for t, data in events if t == "done"))
    assert done_body["finish_reason"] == "refused"


def test_refuses_when_confidence_below_threshold_despite_matching_chunk(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "test.db",
        chroma_path=tmp_path / "chroma",
        retrieval_max_distance=0.0001,  # unreachably strict — forces refusal
    )
    app = create_app(settings, provider=EchoProvider())

    with TestClient(app) as client:
        _ingest(app, settings, "faq.md", b"Our return window is 30 days from delivery.")
        resp = client.post(
            "/api/v1/chat/message", json={"session_id": "s1", "message": "return window"}
        )

    events = parse_sse(resp.text)
    types = [t for t, _ in events]
    assert "citations" not in types
    done_body = json.loads(next(data for t, data in events if t == "done"))
    assert done_body["finish_reason"] == "refused"


def test_message_over_cap_rejected(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/chat/message",
        json={"session_id": "s1", "message": "x" * 501},
        headers={"origin": "http://widget.example"},
    )
    assert resp.status_code == 422


def test_disallowed_origin_rejected(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/chat/message",
        json={"session_id": "s1", "message": "hi"},
        headers={"origin": "http://evil.example"},
    )
    assert resp.status_code == 403


def test_missing_origin_header_allowed(client: TestClient) -> None:
    resp = client.post("/api/v1/chat/message", json={"session_id": "s1", "message": "hi"})
    assert resp.status_code == 200


def test_provider_failure_yields_error_event(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "test.db",
        chroma_path=tmp_path / "chroma",
        retrieval_max_distance=1000.0,  # must clear the refusal gate to reach the provider
    )
    app = create_app(settings, provider=FailingProvider())
    with TestClient(app) as client:
        _ingest(app, settings, "faq.md", b"Our return window is 30 days from delivery.")
        resp = client.post("/api/v1/chat/message", json={"session_id": "s1", "message": "hi"})
    events = parse_sse(resp.text)
    assert events[-1][0] == "error"

    error_body = json.loads(events[-1][1])
    assert error_body["code"] == "provider_unavailable"
    assert error_body["retryable"] is True


def test_ip_rate_limit_exhausted_yields_error_event(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "test.db",
        chroma_path=tmp_path / "chroma",
        rate_limit_ip_capacity=1,
    )
    app = create_app(settings, provider=EchoProvider())
    with TestClient(app) as client:
        first = client.post("/api/v1/chat/message", json={"session_id": "s1", "message": "hi"})
        second = client.post("/api/v1/chat/message", json={"session_id": "s2", "message": "hi"})

    assert first.status_code == 200
    events = parse_sse(second.text)
    assert events[0][0] == "error"

    assert json.loads(events[0][1])["code"] == "rate_limited"


def test_session_rate_limit_exhausted_yields_error_event(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "test.db",
        chroma_path=tmp_path / "chroma",
        rate_limit_ip_capacity=1000,
        rate_limit_session_capacity=1,
    )
    app = create_app(settings, provider=EchoProvider())
    with TestClient(app) as client:
        first = client.post("/api/v1/chat/message", json={"session_id": "same", "message": "hi"})
        second = client.post("/api/v1/chat/message", json={"session_id": "same", "message": "hi"})

    assert first.status_code == 200
    events = parse_sse(second.text)
    assert events[0][0] == "error"


def test_retrieval_grounds_the_reply_and_emits_citations(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "test.db",
        chroma_path=tmp_path / "chroma",
        retrieval_max_distance=1000.0,  # see grounded_app fixture docstring above
    )
    provider = CapturingProvider()
    app = create_app(settings, provider=provider)

    with TestClient(app) as client:
        # app.state.db belongs to the lifespan's thread; sqlite3 connections
        # aren't shareable across threads, so ingest through our own
        # connection to the same file rather than reusing app.state.db.
        ingest_db = bootstrap(settings.database_path)
        ingest_upload(
            db=ingest_db,
            collection=app.state.document_collection,
            document_id="doc-1",
            filename="returns.md",
            content=b"Our return window is 30 days from delivery.",
        )
        ingest_db.close()

        resp = client.post(
            "/api/v1/chat/message", json={"session_id": "s1", "message": "return window"}
        )

    assert resp.status_code == 200
    events = parse_sse(resp.text)
    types = [t for t, _ in events]
    assert "citations" in types

    citations_body = json.loads(next(data for t, data in events if t == "citations"))
    assert citations_body["sources"] == [
        {"id": "doc-1", "title": "returns.md", "url": "document://doc-1"}
    ]

    assert provider.last_context is not None
    assert "30 days" in provider.last_context
    assert "<retrieved-context>" in provider.last_context


def test_no_citations_event_when_nothing_ingested(tmp_path: Path) -> None:
    settings = Settings(database_path=tmp_path / "test.db", chroma_path=tmp_path / "chroma")
    app = create_app(settings, provider=EchoProvider())
    with TestClient(app) as client:
        resp = client.post("/api/v1/chat/message", json={"session_id": "s1", "message": "hi"})

    events = parse_sse(resp.text)
    types = [t for t, _ in events]
    assert "citations" not in types
    assert types[0] == "status"
