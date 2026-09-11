import asyncio
import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings, SettingsValidationError
from app.corpus_lifecycle import AttestationTrustPolicy, ResolvedActiveState
from app.embedding_types import EmbeddingFunction
from app.ingest.candidate_persistence import (
    AttestationIdentity,
    AttestationVerifier,
    VerifiedCandidateEvidence,
)
from app.ingest.startup import CorpusStartupError
from app.main import _close_application_resources, create_app
from app.providers.echo import EchoProvider
from app.readiness import OllamaCatalogReadiness, ReadinessError
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
    settings = Settings(
        provider="echo",
        embedding_provider="fake",
        database_path=tmp_path / "test.db",
        chroma_path=tmp_path / "chroma",
    )
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


def test_production_guard_preserves_recursive_startup_source_bytes() -> None:
    root = Path(__file__).resolve().parents[2]
    # Raw-byte SHA-256 values from accepted 45b base
    # dbc7d77d17bea366cce41e1726a469229a491d39.
    accepted_sha256 = {
        "backend/app/ingest/startup.py": (
            "380a02c156d5dc887dd6ca84304b4573d42aea46039909dc8e4f3fbccc6d93da"
        ),
    }
    assert set(accepted_sha256) == {"backend/app/ingest/startup.py"}
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


def _forged_production_corpus_settings() -> Settings:
    values = Settings().model_dump()
    values.update(
        deployment_mode="production",
        provider="ollama",
        embedding_provider="ollama",
        corpus_path=Path("application-private-corpus"),
    )
    return Settings.model_construct(**values)


def test_invalid_snapshot_is_rejected_before_every_application_side_effect(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[str] = []

    def tripwire(name: str) -> Callable[..., object]:
        def fail(*args: object, **kwargs: object) -> object:
            del args, kwargs
            calls.append(name)
            raise AssertionError(name)

        return fail

    intercepted = {
        "logging": ("app.main.configure_logging", tripwire("logging")),
        "provider": ("app.main._default_provider", tripwire("provider")),
        "environment": ("app.config.os.getenv", tripwire("environment")),
        "database": ("app.main.bootstrap", tripwire("database")),
        "vector_client": ("app.main.get_vector_client", tripwire("vector_client")),
        "collection": (
            "app.main.get_document_collection",
            tripwire("collection"),
        ),
        "corpus": ("app.main.ingest_corpus", tripwire("corpus")),
        "provenance": (
            "app.ingest.provenance.load_provenance_manifest",
            tripwire("provenance"),
        ),
        "retrieval": (
            "app.main._default_firestore_adapter",
            tripwire("retrieval"),
        ),
        "binding": (
            "app.main.binding_from_firestore_adapter",
            tripwire("binding"),
        ),
        "lifecycle": (
            "app.main.StaticRetrievalRouteResolver",
            tripwire("lifecycle"),
        ),
        "readiness": (
            "app.main.ReadinessEvaluator",
            tripwire("readiness"),
        ),
        "catalog": (
            "app.main.OllamaCatalogProbe.application_owned",
            tripwire("catalog"),
        ),
        "candidate_store": (
            "app.ingest.candidate_firestore.create_candidate_store",
            tripwire("candidate_store"),
        ),
        "credential_factory": (
            "app.retrieval_firestore.create_firestore_vector_client",
            tripwire("credential_factory"),
        ),
        "lifecycle_store": (
            "app.corpus_lifecycle_firestore.create_corpus_lifecycle_store",
            tripwire("lifecycle_store"),
        ),
        "dns": ("socket.getaddrinfo", tripwire("dns")),
        "socket": ("socket.socket.connect", tripwire("socket")),
        "optional_import": (
            "app.main.importlib.import_module",
            tripwire("optional_import"),
        ),
    }
    for target, replacement in intercepted.values():
        monkeypatch.setattr(target, replacement)

    caplog.set_level("DEBUG")
    with pytest.raises(SettingsValidationError) as caught:
        create_app(_forged_production_corpus_settings())

    rendered = (
        str(caught.value)
        + repr(caught.value)
        + repr(caught.value.args)
        + repr(vars(caught.value))
        + repr(caught.value.errors(include_input=True))
        + caught.value.json(include_input=True)
        + repr(caught.value.__cause__)
        + repr(caught.value.__context__)
        + caplog.text
    )
    assert "CORPUS_PATH" in rendered
    assert "application-private-corpus" not in rendered
    assert calls == []

    for name, (_, replacement) in intercepted.items():
        with pytest.raises(AssertionError, match=name):
            replacement()
    assert calls == list(intercepted)


def test_create_app_isolates_snapshot_from_pre_lifespan_caller_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original_db = tmp_path / "original.db"
    original_chroma = tmp_path / "original-chroma"
    source = Settings(
        provider="echo",
        embedding_provider="fake",
        database_path=original_db,
        chroma_path=original_chroma,
        admin_bootstrap_password="original-secret",
        gemini_api_key=SecretStr("original-api-secret"),
    )
    corpus_calls = 0

    def forbidden_ingestion(**kwargs: object) -> object:
        nonlocal corpus_calls
        del kwargs
        corpus_calls += 1
        raise AssertionError("caller mutation enabled ingestion")

    monkeypatch.setattr("app.main.ingest_corpus", forbidden_ingestion)
    application = create_app(source)
    snapshot = application.state.settings

    source.deployment_mode = "production"
    source.provider = "ollama"
    source.embedding_provider = "ollama"
    source.retrieval_backend = "firestore"
    source.corpus_path = tmp_path / "mutated-corpus"
    source.database_path = tmp_path / "mutated.db"
    source.chroma_path = tmp_path / "mutated-chroma"
    source.admin_bootstrap_password = "mutated-secret"
    assert source.gemini_api_key is not None
    object.__setattr__(source.gemini_api_key, "_secret_value", "mutated-api-secret")

    with TestClient(application) as client:
        assert client.get("/readyz").status_code == 200

    assert type(snapshot) is Settings
    assert snapshot is not source
    assert snapshot.deployment_mode == "development"
    assert snapshot.corpus_path is None
    assert snapshot.retrieval_backend == "local"
    assert snapshot.database_path == original_db
    assert snapshot.chroma_path == original_chroma
    assert snapshot.admin_bootstrap_password == "original-secret"
    assert snapshot.gemini_api_key is not None
    assert snapshot.gemini_api_key.get_secret_value() == "original-api-secret"
    assert isinstance(application.state.provider, EchoProvider)
    assert type(application.state.retrieval_scope) is LocalActiveScope
    assert corpus_calls == 0
    assert original_db.exists()
    assert not (tmp_path / "mutated.db").exists()
    assert not (tmp_path / "mutated-chroma").exists()


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
        self.resolve_calls = 0

    async def resolve_active_state(
        self,
        corpus_id: str,
        trust_policy: AttestationTrustPolicy,
    ) -> ResolvedActiveState:
        del corpus_id, trust_policy
        self.resolve_calls += 1
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


def test_production_lifecycle_composition_is_misconfigured_without_hooks(
    tmp_path: Path,
) -> None:
    class _Catalog:
        def __init__(self) -> None:
            self.calls = 0

        async def check_readiness(self) -> object:
            self.calls += 1
            return OllamaCatalogReadiness(
                reachable=True,
                model_names=("generation-model", "embedding-model"),
            )

    resolver, active, _ = _caller_owned_lifecycle_resolver()
    catalog = _Catalog()
    app = create_app(
        Settings(
            deployment_mode="production",
            provider="ollama",
            ollama_model="generation-model",
            embedding_provider="ollama",
            embedding_model="embedding-model",
            database_path=tmp_path / "test.db",
            chroma_path=tmp_path / "chroma",
        ),
        provider=EchoProvider(),
        retrieval_route_resolver=resolver,
        ollama_catalog_probe=catalog,
    )

    with TestClient(app) as client:
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.content == (
            b'{"status":"not_ready","checks":{"database":true,'
            b'"vector_store":false,"corpus":false}}'
        )
    assert active.resolve_calls == 0
    assert catalog.calls == 1


def test_lifecycle_subclass_cannot_select_exact_profile(tmp_path: Path) -> None:
    class _LifecycleSubclass(LifecycleRetrievalRouteResolver):
        pass

    resolver, active, candidate = _caller_owned_lifecycle_resolver()
    subclass = _LifecycleSubclass(
        corpus_id="public-docs",
        active_state_resolver=active,
        verify_attested_candidate=candidate.verify_attested_candidate,
        trust_policy_supplier=lambda: _UnusedTrustPolicy(),
        expected_embedding_identity="embedding-v1",
        expected_embedding_dimensions=2,
        adapter_factory=_UnusedExactAdapterFactory(),
    )
    app = create_app(
        Settings(database_path=tmp_path / "test.db", chroma_path=tmp_path / "chroma"),
        provider=EchoProvider(),
        retrieval_route_resolver=subclass,
    )
    with TestClient(app) as client:
        response = client.get("/readyz")
        assert response.status_code == 503
        report = asyncio.run(app.state.readiness_evaluator.evaluate())
        assert report.checks[6].state == "not_required"
    assert active.resolve_calls == 2


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


async def test_resource_close_cancellation_stops_every_later_close() -> None:
    events: list[str] = []

    class _CancelledRetrieval:
        async def aclose(self) -> None:
            events.append("retrieval")
            raise asyncio.CancelledError

    class _SyncResource:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            events.append(self.name)

    class _AsyncResource:
        def __init__(self, name: str, cancel: bool = False) -> None:
            self.name = name
            self.cancel = cancel

        async def aclose(self) -> None:
            events.append(self.name)
            if self.cancel:
                raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _close_application_resources(
            owns_retrieval=True,
            retrieval=cast(Any, _CancelledRetrieval()),
            vector_client=_SyncResource("vector"),
            db=cast(Any, _SyncResource("database")),
            catalog_probe=cast(Any, _AsyncResource("catalog")),
            owns_provider=True,
            provider=cast(Any, _AsyncResource("provider")),
        )
    assert events == ["retrieval"]

    events.clear()
    with pytest.raises(asyncio.CancelledError):
        await _close_application_resources(
            owns_retrieval=False,
            retrieval=None,
            vector_client=_SyncResource("vector"),
            db=cast(Any, _SyncResource("database")),
            catalog_probe=cast(Any, _AsyncResource("catalog", cancel=True)),
            owns_provider=True,
            provider=cast(Any, _AsyncResource("provider")),
        )
    assert events == ["vector", "database", "catalog"]


async def test_ordinary_resource_close_failure_is_content_free_and_finishes_cleanup() -> None:
    canary = "PRIVATE-RESOURCE-CLOSE-CANARY"
    events: list[str] = []

    class _Broken:
        def close(self) -> None:
            events.append("broken")
            raise RuntimeError(canary)

    class _Closed:
        def close(self) -> None:
            events.append("closed")

    with pytest.raises(ReadinessError) as caught:
        await _close_application_resources(
            owns_retrieval=False,
            retrieval=None,
            vector_client=_Broken(),
            db=cast(Any, _Closed()),
            catalog_probe=None,
            owns_provider=False,
            provider=EchoProvider(),
        )
    rendered = (
        str(caught.value)
        + repr(caught.value)
        + repr(caught.value.args)
        + repr(vars(caught.value))
        + repr(caught.value.__cause__)
        + repr(caught.value.__context__)
    )
    assert canary not in rendered
    assert events == ["broken", "closed"]
