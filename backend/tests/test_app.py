import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.corpus_lifecycle import AttestationTrustPolicy, ResolvedActiveState
from app.embedding_types import EmbeddingFunction
from app.ingest.candidate_persistence import (
    AttestationIdentity,
    AttestationVerifier,
    VerifiedCandidateEvidence,
)
from app.ingest.startup import CorpusStartupError
from app.main import create_app
from app.providers.echo import EchoProvider
from app.retrieval_contracts import (
    ExactCorpusReference,
    LocalActiveScope,
    RetrievalError,
    RetrievalProbe,
    RetrievalRequest,
    RetrievalResult,
    RetrievalScope,
)
from app.retrieval_route import (
    ExactRetrievalAdapterBinding,
    LifecycleRetrievalRouteResolver,
    ResolvedRetrievalRoute,
)
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
    assert resp.content == (
        b'{"status":"ok","checks":{"database":true,"vector_store":true,'
        b'"corpus":true}}'
    )
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"] is True
    assert body["checks"]["vector_store"] is True


def test_routing_preserves_config_capability_and_recursive_startup_bytes() -> None:
    root = Path(__file__).resolve().parents[2]
    # Raw-byte SHA-256 values from accepted 45b base
    # dbc7d77d17bea366cce41e1726a469229a491d39.
    accepted_sha256 = {
        "backend/app/config.py": (
            "c0edc437213d8a0252dad94270dc9e58bc56cca51a4935ec269bd8c71c3716f1"
        ),
        "backend/app/capabilities.json": (
            "a2e44a748ff13b4705334c6278c1c7cbebd0aaa9e498e2f0cce4f2a8855a1e74"
        ),
        "backend/app/ingest/startup.py": (
            "380a02c156d5dc887dd6ca84304b4573d42aea46039909dc8e4f3fbccc6d93da"
        ),
    }
    assert set(accepted_sha256) == {
        "backend/app/config.py",
        "backend/app/capabilities.json",
        "backend/app/ingest/startup.py",
    }
    for relative, expected in accepted_sha256.items():
        actual = hashlib.sha256((root / relative).read_bytes()).hexdigest()
        assert actual == expected, relative


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
    assert resp.content == (
        b'{"status":"not_ready","checks":{"database":false,'
        b'"vector_store":true,"corpus":true}}'
    )
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
    assert resp.content == (
        b'{"status":"not_ready","checks":{"database":true,'
        b'"vector_store":false,"corpus":true}}'
    )
    body = resp.json()
    assert body["checks"]["vector_store"] is False
    assert body["checks"]["database"] is True


def test_database_file_created(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "test.db"
    settings = Settings(database_path=db_path, chroma_path=tmp_path / "chroma")
    app = create_app(settings)
    with TestClient(app):
        assert db_path.exists()


class _InjectedRouteResolver:
    def __init__(self) -> None:
        self.close_calls = 0

    async def resolve_route(self) -> ResolvedRetrievalRoute:
        raise AssertionError("route resolution is request-scoped")

    async def check_readiness(self) -> RetrievalProbe:
        raise AssertionError("aggregate readiness is deferred")

    async def aclose(self) -> None:
        self.close_calls += 1


class _InjectedAdapter:
    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        return RetrievalResult(
            scope=request.scope,
            distance_measure=request.distance_measure,
            max_distance=request.max_distance,
            chunks=(),
        )

    async def check_readiness(self, scope: RetrievalScope) -> RetrievalProbe:
        return RetrievalProbe(
            scope=scope,
            reachable=True,
            store_ready=True,
            exact_version_ready=False,
        )


class _CloseTrackedActiveStateResolver:
    def __init__(self) -> None:
        self.close_calls = 0

    async def resolve_active_state(
        self,
        corpus_id: str,
        trust_policy: AttestationTrustPolicy,
    ) -> ResolvedActiveState:
        del corpus_id, trust_policy
        raise AssertionError("active state must not be read during application lifespan")

    async def aclose(self) -> None:
        self.close_calls += 1


class _CloseTrackedCandidateVerificationService:
    def __init__(self) -> None:
        self.close_calls = 0

    async def verify_attested_candidate(
        self,
        corpus: ExactCorpusReference,
        identity: AttestationIdentity,
        verifier: AttestationVerifier,
    ) -> VerifiedCandidateEvidence:
        del corpus, identity, verifier
        raise AssertionError("M8 verification must remain request-scoped")

    async def aclose(self) -> None:
        self.close_calls += 1


class _UnusedTrustPolicy:
    policy_version = "policy-v1"
    policy_generation = 1

    def verifier_for(self, identity: AttestationIdentity) -> None:
        del identity
        return None


class _UnusedExactAdapterFactory:
    def adapter_for(
        self,
        scope: ExactCorpusReference,
        *,
        embedding_identity: str,
        embedding_dimensions: int,
    ) -> ExactRetrievalAdapterBinding:
        del scope, embedding_identity, embedding_dimensions
        raise AssertionError("adapter construction must remain request-scoped")


def _caller_owned_lifecycle_resolver() -> tuple[
    LifecycleRetrievalRouteResolver,
    _CloseTrackedActiveStateResolver,
    _CloseTrackedCandidateVerificationService,
]:
    active = _CloseTrackedActiveStateResolver()
    candidate = _CloseTrackedCandidateVerificationService()
    policy = _UnusedTrustPolicy()
    resolver = LifecycleRetrievalRouteResolver(
        corpus_id="public-docs",
        active_state_resolver=active,
        verify_attested_candidate=candidate.verify_attested_candidate,
        trust_policy_supplier=lambda: policy,
        expected_embedding_identity="embedding-v1",
        expected_embedding_dimensions=2,
        adapter_factory=_UnusedExactAdapterFactory(),
    )
    return resolver, active, candidate


def test_injected_route_resolver_is_caller_owned_and_selected_once(tmp_path: Path) -> None:
    resolver = _InjectedRouteResolver()
    settings = Settings(database_path=tmp_path / "test.db", chroma_path=tmp_path / "chroma")
    app = create_app(
        settings,
        provider=EchoProvider(),
        retrieval_route_resolver=resolver,
    )

    with TestClient(app):
        assert app.state.retrieval_route_resolver is resolver

    assert resolver.close_calls == 0


def test_injected_lifecycle_dependencies_are_not_closed_on_normal_shutdown(
    tmp_path: Path,
) -> None:
    resolver, active, candidate = _caller_owned_lifecycle_resolver()
    app = create_app(
        Settings(database_path=tmp_path / "test.db", chroma_path=tmp_path / "chroma"),
        provider=EchoProvider(),
        retrieval_route_resolver=resolver,
    )

    with TestClient(app):
        assert app.state.retrieval_route_resolver is resolver

    assert active.close_calls == 0
    assert candidate.close_calls == 0


def test_injected_lifecycle_dependencies_are_not_closed_after_startup_failure(
    tmp_path: Path,
) -> None:
    resolver, active, candidate = _caller_owned_lifecycle_resolver()
    corpus = tmp_path / "empty-corpus"
    corpus.mkdir()
    app = create_app(
        Settings(
            database_path=tmp_path / "test.db",
            chroma_path=tmp_path / "chroma",
            corpus_path=corpus,
        ),
        provider=EchoProvider(),
        retrieval_route_resolver=resolver,
    )

    with pytest.raises(CorpusStartupError, match="no Markdown or PDF files"):
        with TestClient(app):
            pass

    assert active.close_calls == 0
    assert candidate.close_calls == 0


@pytest.mark.parametrize(
    "legacy",
    ["adapter", "scope"],
)
def test_route_resolver_and_legacy_binding_are_rejected_before_startup(
    legacy: str,
    tmp_path: Path,
) -> None:
    resolver = _InjectedRouteResolver()
    kwargs: dict[str, object] = {"retrieval_route_resolver": resolver}
    if legacy == "adapter":
        kwargs["retrieval_adapter"] = _InjectedAdapter()
    else:
        kwargs["retrieval_scope"] = LocalActiveScope()

    with pytest.raises(RetrievalError) as caught:
        create_app(
            Settings(
                database_path=tmp_path / "not-created.db",
                chroma_path=tmp_path / "not-created-chroma",
            ),
            provider=EchoProvider(),
            **kwargs,  # type: ignore[arg-type]
        )
    assert caught.value.code == "invalid_request"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert not (tmp_path / "not-created.db").exists()
    assert resolver.close_calls == 0


class _TrackingVectorClient:
    def __init__(self, delegate: VectorStoreClient) -> None:
        self._delegate = delegate
        self.closed = False
        self.close_calls = 0

    def get_or_create_collection(
        self, *, name: str, embedding_function: EmbeddingFunction
    ) -> DocumentCollection:
        return self._delegate.get_or_create_collection(
            name=name, embedding_function=embedding_function
        )

    def heartbeat(self) -> None:
        self._delegate.heartbeat()

    def close(self) -> None:
        self.close_calls += 1
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
    assert observed[0].close_calls == 1


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
    assert observed[0].close_calls == 1
