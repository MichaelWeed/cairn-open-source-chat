import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.contracts import ChatTurn
from app.config import Settings
from app.main import create_app
from app.providers.base import Provider
from app.providers.echo import EchoProvider


class FailingProvider(Provider):
    async def stream(self, *, message: str, history: list[ChatTurn]) -> AsyncIterator[str]:
        raise ConnectionError("simulated provider outage")
        yield  # pragma: no cover - unreachable, satisfies the generator type


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


def test_happy_path_round_trip(client: TestClient) -> None:
    resp = client.post(
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


def test_chunks_reconstruct_message(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/chat/message",
        json={"session_id": "s1", "message": "hello world"},
        headers={"origin": "http://widget.example"},
    )
    events = parse_sse(resp.text)
    deltas = [json.loads(data)["delta"] for t, data in events if t == "chunk"]
    assert "".join(deltas) == "hello world"


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
    settings = Settings(database_path=tmp_path / "test.db", chroma_path=tmp_path / "chroma")
    app = create_app(settings, provider=FailingProvider())
    with TestClient(app) as client:
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
