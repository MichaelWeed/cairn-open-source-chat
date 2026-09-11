from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import json
import logging
import os
import socket
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, cast

import pytest

import app.retrieval_route as retrieval_route_module
from app.corpus_lifecycle import (
    AttestationTrustPolicy,
    CorpusLifecycleError,
    ResolvedActiveState,
    VerifiedLifecycleEvidence,
)
from app.ingest.candidate_persistence import (
    AttestationIdentity,
    AttestationSigner,
    AttestationVerifier,
    CandidateAttestationVerificationService,
    CandidatePersistenceError,
    CandidatePersistenceRequest,
    CandidatePersistenceService,
    CandidateRecordKind,
    CandidateStorePage,
    CandidateStoreRecord,
    VerifiedCandidateEvidence,
    candidate_inventory_sha256,
)
from app.ingest.planner import (
    CandidateDocumentSnapshot,
    CandidateIngestionPlan,
    CandidateSourceSnapshot,
    EmbeddingSpecification,
    plan_candidate,
)
from app.readiness import ReadinessEvaluator
from app.retrieval_contracts import (
    ExactCorpusReference,
    LocalActiveScope,
    RetrievalError,
    RetrievalProbe,
    RetrievalRequest,
    RetrievalResult,
    RetrievalScope,
)
from app.retrieval_firestore import (
    FirestoreReadinessQuery,
    FirestoreRetrievalAdapter,
    FirestoreVectorQuery,
    FirestoreVectorRow,
)
from app.retrieval_route import (
    ActiveStateResolver,
    ExactRetrievalAdapterBinding,
    ExactRetrievalAdapterFactory,
    LifecycleRetrievalRouteResolver,
    ResolvedRetrievalRoute,
    StaticRetrievalRouteResolver,
    binding_from_firestore_adapter,
    validate_route_authority,
)

CandidateVerifier = Callable[
    [ExactCorpusReference, AttestationIdentity, AttestationVerifier],
    Awaitable[VerifiedCandidateEvidence],
]


def _forbidden_external(*args: object, **kwargs: object) -> object:
    del args, kwargs
    raise AssertionError("external access forbidden in retrieval-route tests")


def _google_auth_available() -> bool:
    try:
        return importlib.util.find_spec("google.auth") is not None
    except ModuleNotFoundError:
        return False


@pytest.fixture(autouse=True)
def no_external_route_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    real_getenv = os.getenv

    def guarded_getenv(key: str, default: str | None = None) -> str | None:
        if key == "PYDANTIC_DISABLE_PLUGINS":
            return real_getenv(key, default)
        return cast(str | None, _forbidden_external(key, default))

    monkeypatch.setattr(socket, "getaddrinfo", _forbidden_external)
    monkeypatch.setattr(socket, "create_connection", _forbidden_external)
    monkeypatch.setattr(socket.socket, "connect", _forbidden_external)
    monkeypatch.setattr("app.providers.gemini.GeminiProvider.stream", _forbidden_external)
    monkeypatch.setattr("app.providers.ollama.OllamaProvider.stream", _forbidden_external)
    monkeypatch.setattr(
        "app.retrieval_firestore.create_firestore_vector_client",
        _forbidden_external,
    )
    monkeypatch.setattr(
        "app.ingest.candidate_firestore.create_candidate_store",
        _forbidden_external,
    )
    monkeypatch.setattr(
        "app.corpus_lifecycle_firestore.create_corpus_lifecycle_store",
        _forbidden_external,
    )
    if _google_auth_available():
        google_auth = importlib.import_module("google.auth")
        monkeypatch.setattr(google_auth, "default", _forbidden_external)
    monkeypatch.setattr(os, "getenv", guarded_getenv)


class _VectorClient:
    def __init__(self) -> None:
        self.vector_calls = 0
        self.readiness_calls = 0
        self.close_calls = 0

    async def vector_get(self, request: FirestoreVectorQuery) -> Sequence[FirestoreVectorRow]:
        del request
        self.vector_calls += 1
        return ()

    async def readiness_get(self, request: FirestoreReadinessQuery) -> None:
        del request
        self.readiness_calls += 1

    async def aclose(self) -> None:
        self.close_calls += 1


class _Embedder:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, input: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return [[0.25, 0.5] for _ in input]


class _LocalAdapter:
    def __init__(self) -> None:
        self.retrieve_calls = 0
        self.readiness_calls = 0

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        self.retrieve_calls += 1
        return RetrievalResult(
            scope=request.scope,
            distance_measure=request.distance_measure,
            max_distance=request.max_distance,
            chunks=(),
        )

    async def check_readiness(self, scope: RetrievalScope) -> RetrievalProbe:
        self.readiness_calls += 1
        return RetrievalProbe(
            scope=scope,
            reachable=True,
            store_ready=True,
            exact_version_ready=False,
        )


def _scope(version: str = "v1") -> ExactCorpusReference:
    return ExactCorpusReference(corpus_id="public-docs", corpus_version=version)


def _firestore_adapter(
    scope: ExactCorpusReference,
) -> tuple[FirestoreRetrievalAdapter, _VectorClient, _Embedder]:
    return _firestore_adapter_for_class(FirestoreRetrievalAdapter, scope)


def _firestore_adapter_for_class(
    adapter_class: type[FirestoreRetrievalAdapter],
    scope: ExactCorpusReference,
) -> tuple[FirestoreRetrievalAdapter, _VectorClient, _Embedder]:
    client = _VectorClient()
    embedder = _Embedder()
    adapter = adapter_class(
        client=client,
        embedding_function=embedder,
        scope=scope,
        embedding_identity="embedding-v1",
        embedding_dimensions=2,
        distance_measure="cosine",
        timeout_seconds=3,
        max_retries=0,
        owns_client=False,
    )
    return adapter, client, embedder


def _binding(
    scope: ExactCorpusReference,
    adapter: FirestoreRetrievalAdapter,
) -> ExactRetrievalAdapterBinding:
    return ExactRetrievalAdapterBinding(
        scope=scope,
        embedding_identity="embedding-v1",
        embedding_dimensions=2,
        adapter=adapter,
    )


def _lifecycle_evidence(scope: ExactCorpusReference) -> VerifiedLifecycleEvidence:
    return VerifiedLifecycleEvidence(
        corpus=scope,
        plan_sha256="1" * 64,
        semantic_manifest_sha256="2" * 64,
        embedding_identity="embedding-v1",
        embedding_dimensions=2,
        document_count=1,
        chunk_count=1,
        inventory_sha256="3" * 64,
        attestation_payload_sha256="4" * 64,
        signature_algorithm_id="test-algorithm",
        signing_key_id="test-key",
    )


def _candidate_evidence(scope: ExactCorpusReference) -> VerifiedCandidateEvidence:
    return VerifiedCandidateEvidence.model_validate(_lifecycle_evidence(scope).model_dump())


class _Verifier:
    algorithm_id = "test-algorithm"
    key_id = "test-key"

    async def verify(self, payload: bytes, signature: bytes) -> bool:
        del payload, signature
        return True


class _Policy:
    def __init__(self, *, version: str = "policy-v1", generation: int = 1) -> None:
        self._version = version
        self._generation = generation
        self.version_reads = 0
        self.generation_reads = 0
        self.lookup_calls = 0
        self.verifier = _Verifier()

    @property
    def policy_version(self) -> str:
        self.version_reads += 1
        return self._version

    @property
    def policy_generation(self) -> int:
        self.generation_reads += 1
        return self._generation

    def verifier_for(self, identity: AttestationIdentity) -> _Verifier | None:
        self.lookup_calls += 1
        if (identity.algorithm_id, identity.key_id) != (
            self.verifier.algorithm_id,
            self.verifier.key_id,
        ):
            return None
        return self.verifier


class _DynamicPolicy(_Policy):
    def verifier_for(self, identity: AttestationIdentity) -> _Verifier:
        self.lookup_calls += 1
        verifier = _Verifier()
        verifier.algorithm_id = identity.algorithm_id
        verifier.key_id = identity.key_id
        return verifier


class _ActiveReader:
    def __init__(self, scope: ExactCorpusReference) -> None:
        self.scope = scope
        self.calls = 0
        self.policies: list[AttestationTrustPolicy] = []

    async def resolve_active_state(
        self, corpus_id: str, trust_policy: AttestationTrustPolicy
    ) -> ResolvedActiveState:
        self.calls += 1
        self.policies.append(trust_policy)
        assert corpus_id == self.scope.corpus_id
        # Accepted 45a performs its own property reads and allowlist selection.
        _ = trust_policy.policy_version
        _ = trust_policy.policy_generation
        identity = AttestationIdentity(algorithm_id="test-algorithm", key_id="test-key")
        assert trust_policy.verifier_for(identity) is not None
        return ResolvedActiveState(
            target=self.scope,
            pointer_revision=0,
            lifecycle_revision=0,
            evidence=_lifecycle_evidence(self.scope),
        )


class _SequenceActiveReader(_ActiveReader):
    def __init__(self, states: list[ResolvedActiveState]) -> None:
        super().__init__(states[0].target)
        self.states = states

    async def resolve_active_state(
        self, corpus_id: str, trust_policy: AttestationTrustPolicy
    ) -> ResolvedActiveState:
        state = self.states[min(self.calls, len(self.states) - 1)]
        self.scope = state.target
        self.calls += 1
        self.policies.append(trust_policy)
        assert corpus_id == state.target.corpus_id
        _ = trust_policy.policy_version
        _ = trust_policy.policy_generation
        identity = AttestationIdentity(algorithm_id="test-algorithm", key_id="test-key")
        assert trust_policy.verifier_for(identity) is not None
        return state


class _FailingActiveReader:
    def __init__(self, failure: BaseException) -> None:
        self.failure = failure
        self.calls = 0

    async def resolve_active_state(
        self, corpus_id: str, trust_policy: AttestationTrustPolicy
    ) -> ResolvedActiveState:
        del corpus_id, trust_policy
        self.calls += 1
        raise self.failure


class _CandidateVerifier:
    def __init__(self) -> None:
        self.calls = 0
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None
        self.failure: BaseException | None = None
        self.result_override: object | None = None
        self.arguments: list[
            tuple[ExactCorpusReference, AttestationIdentity, AttestationVerifier]
        ] = []

    async def __call__(
        self,
        corpus: ExactCorpusReference,
        identity: AttestationIdentity,
        verifier: AttestationVerifier,
    ) -> VerifiedCandidateEvidence:
        self.calls += 1
        self.arguments.append((corpus, identity, verifier))
        assert identity.algorithm_id == verifier.algorithm_id
        assert identity.key_id == verifier.key_id
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.failure is not None:
            raise self.failure
        if self.result_override is not None:
            return cast(VerifiedCandidateEvidence, self.result_override)
        return _candidate_evidence(corpus)


class _CandidateMemoryStore:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], CandidateStoreRecord] = {}
        self.read_calls: list[tuple[object, ...]] = []
        self.write_calls = 0
        self.writes_forbidden = False
        self.close_calls = 0

    def encoded_create_base_size(self, kind: CandidateRecordKind) -> int:
        del kind
        return 16

    def encoded_record_sizes(self, record: CandidateStoreRecord) -> tuple[int, int, str]:
        document_size = len(repr(record.value).encode())
        return document_size, document_size + 64, candidate_inventory_sha256((record,))

    def encoded_document_size(self, record: CandidateStoreRecord) -> int:
        return self.encoded_record_sizes(record)[0]

    def encoded_create_size(
        self,
        kind: CandidateRecordKind,
        records: tuple[CandidateStoreRecord, ...],
    ) -> int:
        return self.encoded_create_base_size(kind) + sum(
            self.encoded_record_sizes(record)[1] for record in records
        )

    async def get(
        self,
        kind: CandidateRecordKind,
        key: str,
        *,
        timeout_seconds: int,
    ) -> CandidateStoreRecord | None:
        self.read_calls.append(("get", kind, key, timeout_seconds))
        return self.records.get((kind, key))

    async def get_many(
        self,
        kind: CandidateRecordKind,
        keys: tuple[str, ...],
        *,
        timeout_seconds: int,
    ) -> tuple[CandidateStoreRecord | None, ...]:
        self.read_calls.append(("get_many", kind, keys, timeout_seconds))
        return tuple(self.records.get((kind, key)) for key in keys)

    async def create_many_checked(
        self,
        kind: CandidateRecordKind,
        records: tuple[CandidateStoreRecord, ...],
        *,
        expected_encoded_size: int,
        expected_write_sha256s: tuple[str, ...],
        timeout_seconds: int,
    ) -> None:
        del expected_encoded_size, expected_write_sha256s, timeout_seconds
        await self.create_many(kind, records, timeout_seconds=3)

    async def create_many(
        self,
        kind: CandidateRecordKind,
        records: tuple[CandidateStoreRecord, ...],
        *,
        timeout_seconds: int,
    ) -> None:
        del timeout_seconds
        self.write_calls += 1
        if self.writes_forbidden:
            raise AssertionError("verification attempted a candidate write")
        for record in records:
            self.records[(kind, record.key)] = record

    async def list_page(
        self,
        kind: CandidateRecordKind,
        corpus: ExactCorpusReference,
        after_key: str | None,
        limit: int,
        *,
        timeout_seconds: int,
    ) -> CandidateStorePage:
        self.read_calls.append(("list_page", kind, corpus, after_key, limit, timeout_seconds))
        matching = sorted(
            (
                record
                for (record_kind, _), record in self.records.items()
                if record_kind == kind
                and record.value.get("corpus_id") == corpus.corpus_id
                and record.value.get("corpus_version") == corpus.corpus_version
                and (after_key is None or record.key.encode() > after_key.encode())
            ),
            key=lambda record: record.key.encode(),
        )
        page = tuple(matching[:limit])
        return CandidateStorePage(
            records=page,
            next_after_key=page[-1].key if len(matching) > limit else None,
        )

    async def aclose(self) -> None:
        self.close_calls += 1


class _FixtureSigner:
    algorithm_id = "test-algorithm"
    key_id = "test-key"

    def __init__(self) -> None:
        self.sign_calls = 0
        self.verify_calls = 0

    async def sign(self, payload: bytes) -> bytes:
        self.sign_calls += 1
        return hashlib.sha256(b"route-fixture\0" + payload).digest()

    async def verify(self, payload: bytes, signature: bytes) -> bool:
        self.verify_calls += 1
        return signature == hashlib.sha256(b"route-fixture\0" + payload).digest()


async def _no_sleep(delay: float) -> None:
    del delay


def _candidate_plan() -> CandidateIngestionPlan:
    content = b"Reviewed resolver integration content"
    manifest = json.dumps(
        {
            "version": 1,
            "documents": {
                "guide.md": {
                    "title": "Public guide",
                    "url": "https://docs.example.test/guide",
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "owner": "Docs",
                    "reviewed_at": "2026-09-10",
                    "public": True,
                }
            },
        }
    ).encode()
    return plan_candidate(
        corpus=_scope(),
        source=CandidateSourceSnapshot(
            manifest_bytes=manifest,
            documents=(CandidateDocumentSnapshot(relative_path="guide.md", content=content),),
        ),
        embedding=EmbeddingSpecification(identity="embedding-v1", dimensions=2),
        embed=lambda values: tuple((0.25, 0.5) for _ in values),
    )


class _Factory:
    def __init__(self, bindings: dict[str, ExactRetrievalAdapterBinding]) -> None:
        self.bindings = bindings
        self.calls: list[tuple[ExactCorpusReference, str, int]] = []

    def adapter_for(
        self,
        scope: ExactCorpusReference,
        *,
        embedding_identity: str,
        embedding_dimensions: int,
    ) -> ExactRetrievalAdapterBinding:
        self.calls.append((scope, embedding_identity, embedding_dimensions))
        return self.bindings[scope.corpus_version]


def _active_state(
    scope: ExactCorpusReference,
    *,
    pointer_revision: int = 0,
    evidence: VerifiedLifecycleEvidence | None = None,
) -> ResolvedActiveState:
    return ResolvedActiveState(
        target=scope,
        pointer_revision=pointer_revision,
        lifecycle_revision=0,
        evidence=evidence or _lifecycle_evidence(scope),
    )


def _resolver_with_parts(
    *,
    active_reader: ActiveStateResolver,
    candidate_verifier: CandidateVerifier,
    policy_supplier: Callable[[], AttestationTrustPolicy],
    factory: ExactRetrievalAdapterFactory,
) -> LifecycleRetrievalRouteResolver:
    return LifecycleRetrievalRouteResolver(
        corpus_id="public-docs",
        active_state_resolver=active_reader,
        verify_attested_candidate=candidate_verifier,
        trust_policy_supplier=policy_supplier,
        expected_embedding_identity="embedding-v1",
        expected_embedding_dimensions=2,
        adapter_factory=factory,
    )


def _assert_sanitized(error: RetrievalError, code: str) -> None:
    assert error.code == code
    assert error.__cause__ is None
    assert error.__context__ is None


def _render_recursive(value: object, seen: set[int] | None = None) -> str:
    visited = set() if seen is None else seen
    if id(value) in visited:
        return "<cycle>"
    visited.add(id(value))
    if isinstance(value, BaseException):
        return "\n".join(
            (
                str(value),
                repr(value),
                _render_recursive(value.args, visited),
                _render_recursive(vars(value), visited),
                _render_recursive(value.__cause__, visited),
                _render_recursive(value.__context__, visited),
            )
        )
    if isinstance(value, Mapping):
        return "\n".join(
            _render_recursive(item, visited) for pair in value.items() for item in pair
        )
    if isinstance(value, (tuple, list, set, frozenset)):
        return "\n".join(_render_recursive(item, visited) for item in value)
    return repr(value)


def _assert_canary_absent_from_surfaces(
    error: RetrievalError,
    canary: str,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured = capsys.readouterr()
    rendered = "\n".join(
        (
            _render_recursive(error),
            caplog.text,
            *(str(record.getMessage()) for record in caplog.records),
            captured.out,
            captured.err,
        )
    )
    assert canary not in rendered


def _forged_route(
    scope: object,
    adapter: object,
    binding: object,
) -> ResolvedRetrievalRoute:
    route = object.__new__(ResolvedRetrievalRoute)
    object.__setattr__(route, "scope", scope)
    object.__setattr__(route, "adapter", adapter)
    object.__setattr__(route, "exact_binding", binding)
    return route


def _lifecycle_resolver(
    *,
    scope: ExactCorpusReference | None = None,
    policy_supplier: Callable[[], _Policy] | None = None,
    candidate_verifier: _CandidateVerifier | None = None,
) -> tuple[
    LifecycleRetrievalRouteResolver,
    _ActiveReader,
    _CandidateVerifier,
    _Factory,
    FirestoreRetrievalAdapter,
    _VectorClient,
    _Embedder,
    _Policy,
]:
    selected_scope = scope or _scope()
    adapter, client, embedder = _firestore_adapter(selected_scope)
    factory = _Factory({selected_scope.corpus_version: _binding(selected_scope, adapter)})
    active_reader = _ActiveReader(selected_scope)
    verifier = candidate_verifier or _CandidateVerifier()
    policy = _Policy()
    supplier = policy_supplier or (lambda: policy)
    resolver = LifecycleRetrievalRouteResolver(
        corpus_id=selected_scope.corpus_id,
        active_state_resolver=active_reader,
        verify_attested_candidate=verifier,
        trust_policy_supplier=supplier,
        expected_embedding_identity="embedding-v1",
        expected_embedding_dimensions=2,
        adapter_factory=factory,
    )
    return resolver, active_reader, verifier, factory, adapter, client, embedder, policy


@pytest.mark.asyncio
async def test_static_local_route_is_generic_and_no_firestore_private_state_is_read() -> None:
    adapter = _LocalAdapter()
    route = ResolvedRetrievalRoute(scope=LocalActiveScope(), adapter=adapter)
    resolver = StaticRetrievalRouteResolver(route)

    assert await resolver.resolve_route() == route
    assert await resolver.check_readiness() == RetrievalProbe(
        scope=LocalActiveScope(),
        reachable=True,
        store_ready=True,
        exact_version_ready=False,
    )
    assert adapter.retrieve_calls == 0
    assert adapter.readiness_calls == 1


@pytest.mark.asyncio
async def test_static_local_readiness_never_relays_a_forged_exact_ready_claim() -> None:
    class MaliciousLocalAdapter(_LocalAdapter):
        async def check_readiness(self, scope: RetrievalScope) -> RetrievalProbe:
            self.readiness_calls += 1
            return RetrievalProbe.model_construct(
                contract_version="1.0",
                scope=scope,
                reachable=True,
                store_ready=True,
                exact_version_ready=True,
            )

    adapter = MaliciousLocalAdapter()
    resolver = StaticRetrievalRouteResolver(
        ResolvedRetrievalRoute(scope=LocalActiveScope(), adapter=adapter)
    )

    assert await resolver.check_readiness() == RetrievalProbe(
        scope=LocalActiveScope(),
        reachable=True,
        store_ready=True,
        exact_version_ready=False,
    )
    assert adapter.readiness_calls == 1


@pytest.mark.asyncio
async def test_static_exact_route_rechecks_authoritative_m6_binding_before_each_use() -> None:
    scope = _scope()
    adapter, client, embedder = _firestore_adapter(scope)
    route = ResolvedRetrievalRoute(
        scope=scope,
        adapter=adapter,
        exact_binding=_binding(scope, adapter),
    )
    resolver = StaticRetrievalRouteResolver(route)

    assert (await resolver.resolve_route()).scope == scope
    object.__setattr__(adapter, "_embedding_identity", "mutated")
    with pytest.raises(RetrievalError) as caught:
        await resolver.check_readiness()
    assert caught.value.code == "malformed_result"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert client.readiness_calls == 0
    assert client.vector_calls == 0
    assert embedder.calls == 0


@pytest.mark.asyncio
async def test_lifecycle_cache_refreshes_once_and_returns_exact_bound_route() -> None:
    resolver, active, verifier, factory, adapter, client, embedder, policy = _lifecycle_resolver()

    first = await resolver.resolve_route()
    second = await resolver.resolve_route()

    assert first == second
    assert first.scope == _scope()
    assert first.adapter is adapter
    assert active.calls == 2
    assert verifier.calls == 1
    assert len(factory.calls) == 2
    assert policy.version_reads == 4
    assert policy.generation_reads == 4
    assert policy.lookup_calls == 3
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0
    retained = repr(resolver.__dict__)
    assert "_Verifier" not in retained
    assert "_Policy" not in retained
    assert "FirestoreRetrievalAdapter" not in retained


@pytest.mark.asyncio
async def test_lifecycle_uses_actual_bound_m8_verification_service_read_only() -> None:
    plan = _candidate_plan()
    store = _CandidateMemoryStore()
    signer: AttestationSigner = _FixtureSigner()
    persistence = CandidatePersistenceService(
        store=store,
        signer=signer,
        expected_embedding_identity="embedding-v1",
        expected_embedding_dimensions=2,
        timeout_seconds=3,
        max_retries=0,
        sleep=_no_sleep,
        owns_store=False,
    )
    receipt = await persistence.persist_and_attest_candidate(
        CandidatePersistenceRequest(contract_version="1.0", plan=plan)
    )
    seed_write_calls = store.write_calls
    seed_sign_calls = cast(_FixtureSigner, signer).sign_calls
    store.read_calls.clear()
    store.writes_forbidden = True
    verification = CandidateAttestationVerificationService(
        store=store,
        timeout_seconds=3,
        max_retries=0,
        sleep=_no_sleep,
        owns_store=False,
    )
    evidence = VerifiedLifecycleEvidence(
        corpus=plan.corpus,
        plan_sha256=plan.plan_sha256,
        semantic_manifest_sha256=plan.semantic_manifest_sha256,
        embedding_identity=plan.embedding.identity,
        embedding_dimensions=plan.embedding.dimensions,
        document_count=len(plan.documents),
        chunk_count=sum(len(document.chunks) for document in plan.documents),
        inventory_sha256=receipt.inventory_sha256,
        attestation_payload_sha256=receipt.attestation_payload_sha256,
        signature_algorithm_id=signer.algorithm_id,
        signing_key_id=signer.key_id,
    )
    active = _SequenceActiveReader([_active_state(plan.corpus, evidence=evidence)])
    adapter, client, embedder = _firestore_adapter(plan.corpus)
    factory = _Factory({plan.corpus.corpus_version: _binding(plan.corpus, adapter)})
    policy = _Policy()
    resolver = _resolver_with_parts(
        active_reader=active,
        candidate_verifier=verification.verify_attested_candidate,
        policy_supplier=lambda: policy,
        factory=factory,
    )

    route = await resolver.resolve_route()

    assert route.scope == plan.corpus
    assert route.adapter is adapter
    assert store.write_calls == seed_write_calls
    assert cast(_FixtureSigner, signer).sign_calls == seed_sign_calls
    assert store.read_calls
    assert {call[0] for call in store.read_calls} == {"get", "list_page"}
    assert store.close_calls == 0
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.asyncio
async def test_concurrent_cache_miss_single_flights_refresh_lookup_and_m8() -> None:
    verifier = _CandidateVerifier()
    verifier.entered = asyncio.Event()
    verifier.release = asyncio.Event()
    resolver, active, _, factory, _, client, embedder, policy = _lifecycle_resolver(
        candidate_verifier=verifier
    )

    tasks = [asyncio.create_task(resolver.resolve_route()) for _ in range(5)]
    await verifier.entered.wait()
    await asyncio.sleep(0)
    verifier.release.set()
    routes = await asyncio.gather(*tasks)

    assert all(route.scope == _scope() for route in routes)
    assert active.calls == 5
    assert verifier.calls == 1
    assert policy.lookup_calls == 6  # five accepted 45a lookups plus one refresh
    assert len(factory.calls) == 5
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.asyncio
async def test_lifecycle_readiness_composes_exact_fact_and_preserves_m6_booleans() -> None:
    resolver, _, verifier, _, _, client, embedder, _ = _lifecycle_resolver()

    probe = await resolver.check_readiness()

    assert probe == RetrievalProbe(
        scope=_scope(), reachable=True, store_ready=True, exact_version_ready=True
    )
    assert verifier.calls == 1
    assert client.readiness_calls == 1
    assert client.vector_calls == 0
    assert embedder.calls == 0


@pytest.mark.parametrize(
    ("reachable", "store_ready", "vector_state", "exact_state"),
    [
        (False, False, "not_ready", "unknown"),
        (True, False, "not_ready", "unknown"),
        (True, True, "ready", "ready"),
    ],
)
@pytest.mark.asyncio
async def test_aggregate_readiness_uses_one_actual_lifecycle_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    reachable: bool,
    store_ready: bool,
    vector_state: str,
    exact_state: str,
) -> None:
    probe_calls = 0

    async def probe(
        adapter: FirestoreRetrievalAdapter, scope: RetrievalScope
    ) -> RetrievalProbe:
        nonlocal probe_calls
        del adapter
        probe_calls += 1
        return RetrievalProbe(
            scope=scope,
            reachable=reachable,
            store_ready=store_ready,
            exact_version_ready=False,
        )

    monkeypatch.setattr(FirestoreRetrievalAdapter, "check_readiness", probe)
    resolver, active, verifier, _, _, client, embedder, _ = _lifecycle_resolver()

    def forbidden_heartbeat() -> object:
        raise AssertionError("lifecycle readiness must not use local heartbeat")

    evaluator = ReadinessEvaluator(
        database_probe=lambda: True,
        corpus_probe=lambda: True,
        local_vector_probe=forbidden_heartbeat,
        retrieval_route_resolver=resolver,
        retrieval_profile="lifecycle_exact",
        expected_retrieval_scope=None,
        provider_name="echo",
        provider_model="",
        embedding_name="fake",
        embedding_model="",
        gemini_probe=None,
        ollama_catalog_probe=None,
        budget_probe=None,
    )

    report = await evaluator.evaluate()

    assert report.checks[1].state == vector_state
    assert report.checks[6].state == exact_state
    assert active.calls == verifier.calls == probe_calls == 1
    assert client.readiness_calls == client.vector_calls == embedder.calls == 0


@pytest.mark.asyncio
async def test_lifecycle_readiness_rechecks_authority_after_route_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_validate = validate_route_authority
    validation_calls = 0
    resolver, active, verifier, factory, adapter, client, embedder, _ = _lifecycle_resolver()

    def validate_then_mutate(
        route: object,
        *,
        requested_scope: RetrievalScope | None = None,
    ) -> ResolvedRetrievalRoute:
        nonlocal validation_calls
        validation_calls += 1
        validated = real_validate(route, requested_scope=requested_scope)
        if validation_calls == 1:
            object.__setattr__(adapter, "_embedding_identity", "mutated-after-resolve")
        return validated

    monkeypatch.setattr(
        retrieval_route_module,
        "validate_route_authority",
        validate_then_mutate,
    )

    with pytest.raises(RetrievalError) as caught:
        await resolver.check_readiness()

    _assert_sanitized(caught.value, "malformed_result")
    assert validation_calls == 2
    assert active.calls == verifier.calls == 1
    assert len(factory.calls) == 1
    assert client.readiness_calls == client.vector_calls == embedder.calls == 0


@pytest.mark.asyncio
async def test_lifecycle_failures_are_non_oracular_and_do_not_update_cache() -> None:
    canary = "SENSITIVE-RAW-FAILURE"
    candidate = _CandidateVerifier()
    candidate.failure = RuntimeError(canary)
    resolver, active, _, factory, _, client, embedder, _ = _lifecycle_resolver(
        candidate_verifier=candidate
    )

    with pytest.raises(RetrievalError) as caught:
        await resolver.resolve_route()
    assert caught.value.code == "store_unavailable"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert canary not in repr(caught.value)
    assert active.calls == 1
    assert candidate.calls == 1
    assert factory.calls == []
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0
    assert "last_good" not in repr(resolver.__dict__).lower() or "None" in repr(resolver.__dict__)


@pytest.mark.parametrize(
    ("phase", "expected_code"),
    [
        ("active", "store_unavailable"),
        ("m8", "store_unavailable"),
        ("policy", "store_unavailable"),
        ("factory", "malformed_result"),
    ],
)
@pytest.mark.asyncio
async def test_failure_canaries_are_absent_from_recursive_error_logs_and_output(
    phase: str,
    expected_code: str,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canary = f"SECRET-{phase}-RAW-FAILURE"
    caplog.set_level(logging.DEBUG)
    active: ActiveStateResolver = _ActiveReader(_scope())
    candidate: CandidateVerifier = _CandidateVerifier()
    policy = _Policy()

    class FailingFactory(_Factory):
        def adapter_for(
            self,
            scope: ExactCorpusReference,
            *,
            embedding_identity: str,
            embedding_dimensions: int,
        ) -> ExactRetrievalAdapterBinding:
            del scope, embedding_identity, embedding_dimensions
            raise RuntimeError(canary)

    adapter, client, embedder = _firestore_adapter(_scope())
    factory: ExactRetrievalAdapterFactory = _Factory({"v1": _binding(_scope(), adapter)})

    def policy_supplier() -> AttestationTrustPolicy:
        if phase == "policy":
            raise RuntimeError(canary)
        return policy

    if phase == "active":
        active = _FailingActiveReader(RuntimeError(canary))
    elif phase == "m8":
        failing_candidate = _CandidateVerifier()
        failing_candidate.failure = RuntimeError(canary)
        candidate = failing_candidate
    elif phase == "factory":
        factory = FailingFactory({})
    resolver = _resolver_with_parts(
        active_reader=active,
        candidate_verifier=candidate,
        policy_supplier=policy_supplier,
        factory=factory,
    )

    with pytest.raises(RetrievalError) as caught:
        await resolver.resolve_route()

    _assert_sanitized(caught.value, expected_code)
    _assert_canary_absent_from_surfaces(caught.value, canary, caplog, capsys)
    assert client.readiness_calls == client.vector_calls == embedder.calls == 0


@pytest.mark.parametrize(
    ("field", "mutated"),
    [
        ("_scope", lambda: _scope("v2")),
        ("_embedding_identity", lambda: "embedding-v2"),
        ("_embedding_dimensions", lambda: 3),
    ],
)
@pytest.mark.asyncio
async def test_validate_route_authority_rejects_mutation_before_retrieval_io(
    field: str, mutated: Callable[[], object]
) -> None:
    scope = _scope()
    adapter, client, embedder = _firestore_adapter(scope)
    route = ResolvedRetrievalRoute(
        scope=scope,
        adapter=adapter,
        exact_binding=_binding(scope, adapter),
    )
    object.__setattr__(adapter, field, mutated())

    with pytest.raises(RetrievalError) as caught:
        validate_route_authority(route, requested_scope=scope)
    assert caught.value.code == "malformed_result"
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.asyncio
async def test_cancellation_while_waiting_for_refresh_lock_is_unchanged() -> None:
    verifier = _CandidateVerifier()
    verifier.entered = asyncio.Event()
    verifier.release = asyncio.Event()
    resolver, active, _, factory, _, client, embedder, policy = _lifecycle_resolver(
        candidate_verifier=verifier
    )
    winner = asyncio.create_task(resolver.resolve_route())
    await verifier.entered.wait()
    waiter = asyncio.create_task(resolver.resolve_route())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await waiter
    verifier.release.set()
    await winner

    assert caught.value is not None
    assert verifier.calls == 1
    assert active.calls == 2
    assert policy.lookup_calls == 3
    assert len(factory.calls) == 1
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


def test_route_models_copy_authority_and_reject_invalid_combinations() -> None:
    original_scope = _scope()
    adapter, _, _ = _firestore_adapter(original_scope)
    binding = _binding(original_scope, adapter)
    route = ResolvedRetrievalRoute(
        scope=original_scope,
        adapter=adapter,
        exact_binding=binding,
    )

    assert route.scope == original_scope
    assert route.scope is not original_scope
    assert binding.scope == original_scope
    assert binding.scope is not original_scope

    object.__setattr__(original_scope, "corpus_version", "mutated")
    assert route.scope == _scope()
    assert binding.scope == _scope()

    invalid_routes = (
        {"scope": object(), "adapter": _LocalAdapter()},
        {"scope": LocalActiveScope(), "adapter": object()},
        {
            "scope": LocalActiveScope(),
            "adapter": adapter,
            "exact_binding": binding,
        },
        {"scope": _scope(), "adapter": adapter},
    )
    for values in invalid_routes:
        with pytest.raises(RetrievalError) as caught:
            ResolvedRetrievalRoute(**cast(Any, values))
        _assert_sanitized(caught.value, "invalid_request")


@pytest.mark.parametrize("moving_alias", ["active", "current", "latest", "stable"])
def test_route_models_reject_constructed_moving_aliases_and_hidden_extras(
    moving_alias: str,
) -> None:
    adapter = _LocalAdapter()
    forged = ExactCorpusReference.model_construct(
        kind="exact",
        corpus_id="public-docs",
        corpus_version=moving_alias,
    )
    with pytest.raises(RetrievalError) as caught:
        ExactRetrievalAdapterBinding(
            scope=forged,
            embedding_identity="embedding-v1",
            embedding_dimensions=2,
            adapter=adapter,
        )
    _assert_sanitized(caught.value, "invalid_request")

    hidden = _scope()
    object.__setattr__(hidden, "__pydantic_extra__", {"secret": "not-authority"})
    with pytest.raises(RetrievalError) as caught:
        ResolvedRetrievalRoute(scope=hidden, adapter=adapter)
    _assert_sanitized(caught.value, "invalid_request")


def test_static_resolver_rejects_route_subclass_and_protocol_lookalike() -> None:
    class RouteSubclass(ResolvedRetrievalRoute):
        pass

    class RouteLookalike:
        scope = LocalActiveScope()
        adapter = _LocalAdapter()
        exact_binding = None

    for route in (
        RouteSubclass(scope=LocalActiveScope(), adapter=_LocalAdapter()),
        RouteLookalike(),
    ):
        with pytest.raises(RetrievalError) as caught:
            StaticRetrievalRouteResolver(cast(Any, route))
        _assert_sanitized(caught.value, "invalid_request")


@pytest.mark.parametrize(
    ("identity", "dimensions", "adapter_factory"),
    [
        ("", 2, lambda: _LocalAdapter()),
        (" leading", 2, lambda: _LocalAdapter()),
        ("trailing ", 2, lambda: _LocalAdapter()),
        ("bad\nidentity", 2, lambda: _LocalAdapter()),
        ("embedding-v1", True, lambda: _LocalAdapter()),
        ("embedding-v1", 0, lambda: _LocalAdapter()),
        ("embedding-v1", 2049, lambda: _LocalAdapter()),
        ("embedding-v1", 2, object),
    ],
)
def test_exact_binding_rejects_coercible_or_malformed_inputs(
    identity: object,
    dimensions: object,
    adapter_factory: Callable[[], object],
) -> None:
    with pytest.raises(RetrievalError) as caught:
        ExactRetrievalAdapterBinding(
            scope=_scope(),
            embedding_identity=cast(Any, identity),
            embedding_dimensions=cast(Any, dimensions),
            adapter=cast(Any, adapter_factory()),
        )
    _assert_sanitized(caught.value, "invalid_request")


def test_binding_from_firestore_adapter_copies_good_authority_without_io() -> None:
    scope = _scope()
    adapter, client, embedder = _firestore_adapter(scope)

    binding = binding_from_firestore_adapter(scope, adapter)

    assert type(binding) is ExactRetrievalAdapterBinding
    assert binding.scope == scope
    assert binding.scope is not scope
    assert binding.embedding_identity == "embedding-v1"
    assert binding.embedding_dimensions == 2
    assert binding.adapter is adapter
    assert client.readiness_calls == client.vector_calls == embedder.calls == 0


@pytest.mark.parametrize(
    "missing_field", ["_scope", "_embedding_identity", "_embedding_dimensions"]
)
def test_binding_from_firestore_adapter_rejects_missing_private_authority(
    missing_field: str,
) -> None:
    scope = _scope()
    adapter, client, embedder = _firestore_adapter(scope)
    delattr(adapter, missing_field)

    with pytest.raises(RetrievalError) as caught:
        binding_from_firestore_adapter(scope, adapter)

    _assert_sanitized(caught.value, "malformed_result")
    assert client.readiness_calls == client.vector_calls == embedder.calls == 0


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("_scope", object()),
        ("_embedding_identity", " malformed"),
        ("_embedding_dimensions", True),
    ],
)
def test_binding_from_firestore_adapter_rejects_malformed_private_authority(
    field: str,
    bad_value: object,
) -> None:
    scope = _scope()
    adapter, client, embedder = _firestore_adapter(scope)
    object.__setattr__(adapter, field, bad_value)

    with pytest.raises(RetrievalError) as caught:
        binding_from_firestore_adapter(scope, adapter)

    _assert_sanitized(caught.value, "malformed_result")
    assert client.readiness_calls == client.vector_calls == embedder.calls == 0


def test_binding_from_firestore_adapter_rejects_requested_scope_mutation() -> None:
    adapter, client, embedder = _firestore_adapter(_scope("v1"))

    with pytest.raises(RetrievalError) as caught:
        binding_from_firestore_adapter(_scope("v2"), adapter)

    _assert_sanitized(caught.value, "malformed_result")
    assert client.readiness_calls == client.vector_calls == embedder.calls == 0


@pytest.mark.asyncio
async def test_static_exact_route_preserves_m6_probe_without_claiming_lifecycle() -> None:
    scope = _scope()
    adapter, client, embedder = _firestore_adapter(scope)
    route = ResolvedRetrievalRoute(
        scope=scope,
        adapter=adapter,
        exact_binding=_binding(scope, adapter),
    )
    resolver = StaticRetrievalRouteResolver(route)

    assert await resolver.check_readiness() == RetrievalProbe(
        scope=scope,
        reachable=True,
        store_ready=True,
        exact_version_ready=False,
    )
    assert client.readiness_calls == 1
    assert client.vector_calls == embedder.calls == 0


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("_scope", lambda: _scope("v2")),
        ("_embedding_identity", lambda: "embedding-v2"),
        ("_embedding_dimensions", lambda: 3),
        ("_embedding_identity", lambda: " bad"),
        ("_embedding_dimensions", lambda: True),
        ("_scope", object),
    ],
)
@pytest.mark.asyncio
async def test_static_route_return_rejects_each_mutated_or_malformed_private_authority(
    field: str, bad_value: Callable[[], object]
) -> None:
    scope = _scope()
    adapter, client, embedder = _firestore_adapter(scope)
    route = ResolvedRetrievalRoute(
        scope=scope,
        adapter=adapter,
        exact_binding=_binding(scope, adapter),
    )
    resolver = StaticRetrievalRouteResolver(route)
    object.__setattr__(adapter, field, bad_value())

    with pytest.raises(RetrievalError) as caught:
        await resolver.resolve_route()
    _assert_sanitized(caught.value, "malformed_result")
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.asyncio
async def test_static_route_rejects_missing_private_authority_before_any_adapter_method() -> None:
    scope = _scope()
    adapter, client, embedder = _firestore_adapter(scope)
    route = ResolvedRetrievalRoute(
        scope=scope,
        adapter=adapter,
        exact_binding=_binding(scope, adapter),
    )
    resolver = StaticRetrievalRouteResolver(route)
    delattr(adapter, "_scope")

    with pytest.raises(RetrievalError) as caught:
        await resolver.check_readiness()
    _assert_sanitized(caught.value, "malformed_result")
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.asyncio
async def test_lying_descriptor_cannot_route_b_through_an_a_bound_adapter() -> None:
    scope_a = _scope("v1")
    scope_b = _scope("v2")
    adapter_a, client_a, embedder_a = _firestore_adapter(scope_a)
    lying = ExactRetrievalAdapterBinding(
        scope=scope_b,
        embedding_identity="embedding-v1",
        embedding_dimensions=2,
        adapter=adapter_a,
    )
    with pytest.raises(RetrievalError) as caught:
        route = ResolvedRetrievalRoute(
            scope=scope_b,
            adapter=adapter_a,
            exact_binding=lying,
        )
        resolver = StaticRetrievalRouteResolver(route)
        await resolver.resolve_route()
    _assert_sanitized(caught.value, "malformed_result")
    assert client_a.vector_calls == client_a.readiness_calls == embedder_a.calls == 0


@pytest.mark.parametrize("facsimile_kind", ["binding_subclass", "adapter_subclass", "lookalike"])
def test_bridge_rejects_binding_and_adapter_facsimiles_content_free(
    facsimile_kind: str,
) -> None:
    scope = _scope()
    adapter, client, embedder = _firestore_adapter(scope)
    binding: object = _binding(scope, adapter)
    selected_adapter: object = adapter

    if facsimile_kind == "binding_subclass":

        class BindingSubclass(ExactRetrievalAdapterBinding):
            pass

        binding = BindingSubclass(
            scope=scope,
            embedding_identity="embedding-v1",
            embedding_dimensions=2,
            adapter=adapter,
        )
    elif facsimile_kind == "adapter_subclass":

        class AdapterSubclass(FirestoreRetrievalAdapter):
            pass

        selected_adapter, client, embedder = _firestore_adapter_for_class(AdapterSubclass, scope)
        binding = _binding(scope, selected_adapter)
    else:

        class AdapterLookalike:
            _scope = scope
            _embedding_identity = "embedding-v1"
            _embedding_dimensions = 2

            async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
                raise AssertionError("must not retrieve")

            async def check_readiness(self, requested: object) -> RetrievalProbe:
                raise AssertionError("must not probe")

        selected_adapter = AdapterLookalike()
        binding = ExactRetrievalAdapterBinding(
            scope=scope,
            embedding_identity="embedding-v1",
            embedding_dimensions=2,
            adapter=cast(Any, selected_adapter),
        )

    route = _forged_route(scope, selected_adapter, binding)
    with pytest.raises(RetrievalError) as caught:
        validate_route_authority(route, requested_scope=scope)
    _assert_sanitized(caught.value, "malformed_result")
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.asyncio
async def test_promotion_uses_distinct_exact_adapters_and_never_rereads_one_operation() -> None:
    scope_a = _scope("v1")
    scope_b = _scope("v2")
    adapter_a, client_a, embedder_a = _firestore_adapter(scope_a)
    adapter_b, client_b, embedder_b = _firestore_adapter(scope_b)
    states = [_active_state(scope_a), _active_state(scope_b, pointer_revision=1)]
    active = _SequenceActiveReader(states)
    verifier = _CandidateVerifier()
    factory = _Factory(
        {
            scope_a.corpus_version: _binding(scope_a, adapter_a),
            scope_b.corpus_version: _binding(scope_b, adapter_b),
        }
    )
    policy = _Policy()
    resolver = _resolver_with_parts(
        active_reader=active,
        candidate_verifier=verifier,
        policy_supplier=lambda: policy,
        factory=factory,
    )

    route_a = await resolver.resolve_route()
    route_b = await resolver.resolve_route()

    assert (route_a.scope, route_a.adapter) == (scope_a, adapter_a)
    assert (route_b.scope, route_b.adapter) == (scope_b, adapter_b)
    assert route_a.exact_binding is not route_b.exact_binding
    assert active.calls == 2
    assert verifier.calls == 2
    assert [call[0] for call in factory.calls] == [scope_a, scope_b]
    assert client_a.vector_calls == client_b.vector_calls == 0
    assert client_a.readiness_calls == client_b.readiness_calls == 0
    assert embedder_a.calls == embedder_b.calls == 0


@pytest.mark.asyncio
async def test_refresh_uses_all_eleven_public_evidence_fields_and_exact_identity() -> None:
    resolver, _, verifier, factory, _, client, embedder, _ = _lifecycle_resolver()

    route = await resolver.resolve_route()

    assert route.scope == _scope()
    assert verifier.calls == 1
    corpus, identity, selected = verifier.arguments[0]
    assert corpus == _scope()
    assert identity == AttestationIdentity(algorithm_id="test-algorithm", key_id="test-key")
    assert selected is not None
    assert set(VerifiedCandidateEvidence.model_fields) == {
        "corpus",
        "plan_sha256",
        "semantic_manifest_sha256",
        "embedding_identity",
        "embedding_dimensions",
        "document_count",
        "chunk_count",
        "inventory_sha256",
        "attestation_payload_sha256",
        "signature_algorithm_id",
        "signing_key_id",
    }
    assert set(VerifiedLifecycleEvidence.model_fields) == set(
        VerifiedCandidateEvidence.model_fields
    )
    assert len(factory.calls) == 1
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.parametrize(
    ("field", "different"),
    [
        ("plan_sha256", "a" * 64),
        ("semantic_manifest_sha256", "b" * 64),
        ("embedding_identity", "embedding-v2"),
        ("embedding_dimensions", 3),
        ("document_count", 2),
        ("chunk_count", 2),
        ("inventory_sha256", "c" * 64),
        ("attestation_payload_sha256", "d" * 64),
        ("signature_algorithm_id", "other-algorithm"),
        ("signing_key_id", "other-key"),
    ],
)
@pytest.mark.asyncio
async def test_each_non_corpus_projection_mismatch_fails_before_factory_or_io(
    field: str, different: object
) -> None:
    candidate = _CandidateVerifier()
    values = _candidate_evidence(_scope()).model_dump()
    values[field] = different
    candidate.result_override = VerifiedCandidateEvidence.model_validate(values)
    resolver, _, _, factory, _, client, embedder, _ = _lifecycle_resolver(
        candidate_verifier=candidate
    )

    with pytest.raises(RetrievalError) as caught:
        await resolver.resolve_route()
    _assert_sanitized(caught.value, "store_unavailable")
    assert candidate.calls == 1
    assert factory.calls == []
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.parametrize("bad_result", [False, True, 1, object()])
@pytest.mark.asyncio
async def test_non_model_m8_result_is_fixed_failure_with_no_cache_or_io(
    bad_result: object,
) -> None:
    candidate = _CandidateVerifier()
    candidate.result_override = bad_result
    resolver, _, _, factory, _, client, embedder, _ = _lifecycle_resolver(
        candidate_verifier=candidate
    )

    with pytest.raises(RetrievalError) as caught:
        await resolver.resolve_route()
    _assert_sanitized(caught.value, "store_unavailable")
    assert candidate.calls == 1
    assert factory.calls == []
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.asyncio
async def test_unexpected_m8_exception_is_fixed_and_drops_raw_context() -> None:
    canary = "SECRET-UNEXPECTED-M8-FAILURE"
    candidate = _CandidateVerifier()
    candidate.failure = RuntimeError(canary)
    resolver, _, _, factory, _, client, embedder, _ = _lifecycle_resolver(
        candidate_verifier=candidate
    )

    with pytest.raises(RetrievalError) as caught:
        await resolver.resolve_route()
    _assert_sanitized(caught.value, "store_unavailable")
    assert canary not in repr(caught.value)
    assert factory.calls == []
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.asyncio
async def test_m8_subclass_and_hidden_extra_are_rejected_without_retention() -> None:
    class EvidenceSubclass(VerifiedCandidateEvidence):
        pass

    canary = "SECRET-EVIDENCE-EXTRA"
    for result in (
        EvidenceSubclass.model_validate(_candidate_evidence(_scope()).model_dump()),
        _candidate_evidence(_scope()),
    ):
        if type(result) is VerifiedCandidateEvidence:
            object.__setattr__(result, "__pydantic_extra__", {"hidden": canary})
        candidate = _CandidateVerifier()
        candidate.result_override = result
        resolver, _, _, factory, _, client, embedder, _ = _lifecycle_resolver(
            candidate_verifier=candidate
        )

        with pytest.raises(RetrievalError) as caught:
            await resolver.resolve_route()
        _assert_sanitized(caught.value, "store_unavailable")
        assert canary not in repr(caught.value)
        assert canary not in repr(resolver.__dict__)
        assert factory.calls == []
        assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.asyncio
async def test_m8_direct_hidden_dict_key_is_rejected_without_retention() -> None:
    canary = "SECRET-M8-DICT-EXTRA"
    evidence = _candidate_evidence(_scope())
    evidence.__dict__["hidden"] = canary
    candidate = _CandidateVerifier()
    candidate.result_override = evidence
    resolver, _, _, factory, _, client, embedder, _ = _lifecycle_resolver(
        candidate_verifier=candidate
    )

    with pytest.raises(RetrievalError) as caught:
        await resolver.resolve_route()
    _assert_sanitized(caught.value, "store_unavailable")
    assert canary not in repr(caught.value)
    assert canary not in repr(resolver.__dict__)
    assert factory.calls == []
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.parametrize(
    "injection",
    ["state", "target", "evidence", "evidence_corpus"],
)
@pytest.mark.asyncio
async def test_45a_recursive_hidden_dict_keys_fail_before_m8_factory_or_io(
    injection: str,
) -> None:
    canary = f"SECRET-45A-{injection}"
    state = _active_state(_scope())
    target: object
    if injection == "state":
        target = state
    elif injection == "target":
        target = state.target
    elif injection == "evidence":
        target = state.evidence
    else:
        target = state.evidence.corpus
    cast(Any, target).__dict__["hidden"] = canary
    active = _SequenceActiveReader([state])
    candidate = _CandidateVerifier()
    adapter, client, embedder = _firestore_adapter(_scope())
    factory = _Factory({"v1": _binding(_scope(), adapter)})
    policy = _Policy()
    resolver = _resolver_with_parts(
        active_reader=active,
        candidate_verifier=candidate,
        policy_supplier=lambda: policy,
        factory=factory,
    )

    with pytest.raises(RetrievalError) as caught:
        await resolver.resolve_route()
    _assert_sanitized(caught.value, "store_unavailable")
    assert canary not in repr(caught.value)
    assert canary not in repr(resolver.__dict__)
    assert candidate.calls == 0
    assert factory.calls == []
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.parametrize("subclass_kind", ["state", "evidence"])
@pytest.mark.asyncio
async def test_45a_state_and_nested_evidence_subclasses_are_rejected(
    subclass_kind: str,
) -> None:
    class StateSubclass(ResolvedActiveState):
        pass

    class EvidenceSubclass(VerifiedLifecycleEvidence):
        pass

    if subclass_kind == "state":
        raw_state: ResolvedActiveState = StateSubclass.model_validate(
            _active_state(_scope()).model_dump()
        )
    else:
        raw_state = _active_state(_scope())
        subclass = EvidenceSubclass.model_validate(raw_state.evidence.model_dump())
        object.__setattr__(raw_state, "evidence", subclass)
    active = _SequenceActiveReader([raw_state])
    candidate = _CandidateVerifier()
    adapter, client, embedder = _firestore_adapter(_scope())
    factory = _Factory({"v1": _binding(_scope(), adapter)})
    policy = _Policy()
    resolver = _resolver_with_parts(
        active_reader=active,
        candidate_verifier=candidate,
        policy_supplier=lambda: policy,
        factory=factory,
    )

    with pytest.raises(RetrievalError) as caught:
        await resolver.resolve_route()
    _assert_sanitized(caught.value, "store_unavailable")
    assert candidate.calls == 0
    assert factory.calls == []
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.asyncio
async def test_candidate_corpus_projection_mismatch_fails_before_factory_or_io() -> None:
    candidate = _CandidateVerifier()
    values = _candidate_evidence(_scope()).model_dump()
    values["corpus"] = _scope("v2")
    candidate.result_override = VerifiedCandidateEvidence.model_validate(values)
    resolver, _, _, factory, _, client, embedder, _ = _lifecycle_resolver(
        candidate_verifier=candidate
    )

    with pytest.raises(RetrievalError) as caught:
        await resolver.resolve_route()
    _assert_sanitized(caught.value, "store_unavailable")
    assert candidate.calls == 1
    assert factory.calls == []
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.parametrize(
    "code",
    [
        "invalid_plan",
        "unsupported_embedding",
        "candidate_conflict",
        "store_bounds_exceeded",
        "store_unavailable",
        "malformed_store",
        "attestation_failed",
    ],
)
@pytest.mark.asyncio
async def test_every_m8_error_mapping_is_non_oracular_and_sanitized(code: str) -> None:
    candidate = _CandidateVerifier()
    candidate.failure = CandidatePersistenceError(cast(Any, code))
    resolver, _, _, factory, _, client, embedder, _ = _lifecycle_resolver(
        candidate_verifier=candidate
    )

    with pytest.raises(RetrievalError) as caught:
        await resolver.resolve_route()
    _assert_sanitized(caught.value, "store_unavailable")
    if code != "store_unavailable":
        assert code not in repr(caught.value)
    assert factory.calls == []
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.parametrize(
    "code",
    [
        "invalid_request",
        "candidate_unavailable",
        "attestation_untrusted",
        "lifecycle_conflict",
        "store_unavailable",
        "malformed_store",
        "no_active_version",
        "active_version_forbidden",
    ],
)
@pytest.mark.asyncio
async def test_every_45a_error_mapping_is_non_oracular_and_sanitized(code: str) -> None:
    reader = _FailingActiveReader(CorpusLifecycleError(cast(Any, code)))
    candidate = _CandidateVerifier()
    adapter, client, embedder = _firestore_adapter(_scope())
    factory = _Factory({"v1": _binding(_scope(), adapter)})
    policy = _Policy()
    resolver = _resolver_with_parts(
        active_reader=reader,
        candidate_verifier=candidate,
        policy_supplier=lambda: policy,
        factory=factory,
    )

    with pytest.raises(RetrievalError) as caught:
        await resolver.resolve_route()
    _assert_sanitized(caught.value, "store_unavailable")
    if code != "store_unavailable":
        assert code not in repr(caught.value)
    assert reader.calls == 1
    assert candidate.calls == 0
    assert factory.calls == []
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.asyncio
async def test_single_flight_constructs_refresh_identity_only_for_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_identity = AttestationIdentity
    identity_constructions = 0

    def counted_identity(**values: object) -> AttestationIdentity:
        nonlocal identity_constructions
        identity_constructions += 1
        return real_identity.model_validate(values)

    monkeypatch.setattr(retrieval_route_module, "AttestationIdentity", counted_identity)
    verifier = _CandidateVerifier()
    verifier.entered = asyncio.Event()
    verifier.release = asyncio.Event()
    policy = _Policy()
    supplier_calls = 0

    def supplier() -> _Policy:
        nonlocal supplier_calls
        supplier_calls += 1
        return policy

    resolver, active, _, factory, _, client, embedder, _unused_policy = _lifecycle_resolver(
        candidate_verifier=verifier,
        policy_supplier=supplier,
    )

    tasks = [asyncio.create_task(resolver.resolve_route()) for _ in range(4)]
    await verifier.entered.wait()
    await asyncio.sleep(0)
    verifier.release.set()
    await asyncio.gather(*tasks)
    await resolver.resolve_route()

    assert identity_constructions == 1
    assert supplier_calls == 5
    assert active.calls == 5
    assert verifier.calls == 1
    assert policy.version_reads == policy.generation_reads == 10
    assert policy.lookup_calls == 6  # five 45a lookups and one 45b winner lookup
    assert len(factory.calls) == 5
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.parametrize(
    ("second_version", "second_generation"),
    [("policy-v1", 2), ("policy-v2", 1)],
)
@pytest.mark.asyncio
async def test_inconsistent_policy_replacement_fails_closed_without_refresh_or_factory(
    second_version: str, second_generation: int
) -> None:
    policies = [
        _Policy(version="policy-v1", generation=1),
        _Policy(version=second_version, generation=second_generation),
    ]
    supplier_calls = 0

    def supplier() -> _Policy:
        nonlocal supplier_calls
        selected = policies[min(supplier_calls, 1)]
        supplier_calls += 1
        return selected

    resolver, active, verifier, factory, _, client, embedder, _ = _lifecycle_resolver(
        policy_supplier=supplier
    )

    await resolver.resolve_route()
    with pytest.raises(RetrievalError) as caught:
        await resolver.resolve_route()

    _assert_sanitized(caught.value, "store_unavailable")
    assert supplier_calls == 2
    assert active.calls == 2
    assert verifier.calls == 1
    assert len(factory.calls) == 1
    assert policies[0].version_reads == policies[0].generation_reads == 2
    assert policies[1].version_reads == policies[1].generation_reads == 2
    assert policies[0].lookup_calls == 2
    assert policies[1].lookup_calls == 1
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.asyncio
async def test_valid_policy_rotation_invalidates_cache_and_uses_one_instance_per_call() -> None:
    policies = [
        _Policy(version="policy-v1", generation=1),
        _Policy(version="policy-v2", generation=2),
    ]
    supplier_calls = 0

    def supplier() -> _Policy:
        nonlocal supplier_calls
        selected = policies[min(supplier_calls, 1)]
        supplier_calls += 1
        return selected

    resolver, active, verifier, factory, _, client, embedder, _ = _lifecycle_resolver(
        policy_supplier=supplier
    )

    await resolver.resolve_route()
    await resolver.resolve_route()

    assert supplier_calls == 2
    assert active.calls == 2
    assert verifier.calls == 2
    assert len(factory.calls) == 2
    for policy in policies:
        assert policy.version_reads == policy.generation_reads == 2
        assert policy.lookup_calls == 2
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.asyncio
async def test_policy_generation_rollback_fails_closed_without_refresh_or_factory() -> None:
    policies = [
        _Policy(version="policy-v2", generation=2),
        _Policy(version="policy-v3", generation=1),
    ]
    supplier_calls = 0

    def supplier() -> _Policy:
        nonlocal supplier_calls
        selected = policies[min(supplier_calls, 1)]
        supplier_calls += 1
        return selected

    resolver, active, verifier, factory, _, client, embedder, _ = _lifecycle_resolver(
        policy_supplier=supplier
    )

    await resolver.resolve_route()
    with pytest.raises(RetrievalError) as caught:
        await resolver.resolve_route()

    _assert_sanitized(caught.value, "store_unavailable")
    assert supplier_calls == 2
    assert active.calls == 2
    assert verifier.calls == 1
    assert len(factory.calls) == 1
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.parametrize("changed_authority", ["pointer", "payload", "signer"])
@pytest.mark.asyncio
async def test_each_lifecycle_fingerprint_authority_independently_invalidates_cache(
    changed_authority: str,
) -> None:
    scope = _scope()
    first_evidence = _lifecycle_evidence(scope)
    second_evidence = first_evidence
    pointer_revision = 0
    if changed_authority == "pointer":
        pointer_revision = 1
    elif changed_authority == "payload":
        second_evidence = first_evidence.model_copy(update={"attestation_payload_sha256": "5" * 64})
    else:
        second_evidence = first_evidence.model_copy(
            update={
                "signature_algorithm_id": "test-algorithm-v2",
                "signing_key_id": "test-key-v2",
            }
        )
    states = [
        _active_state(scope, evidence=first_evidence),
        _active_state(
            scope,
            pointer_revision=pointer_revision,
            evidence=second_evidence,
        ),
    ]
    active = _SequenceActiveReader(states)
    verification_calls = 0

    async def verify(
        corpus: ExactCorpusReference,
        identity: AttestationIdentity,
        verifier: AttestationVerifier,
    ) -> VerifiedCandidateEvidence:
        nonlocal verification_calls
        assert corpus == scope
        assert (identity.algorithm_id, identity.key_id) == (
            verifier.algorithm_id,
            verifier.key_id,
        )
        evidence = states[verification_calls].evidence
        verification_calls += 1
        return VerifiedCandidateEvidence.model_validate(evidence.model_dump())

    adapter, client, embedder = _firestore_adapter(scope)
    factory = _Factory({"v1": _binding(scope, adapter)})
    policy = _DynamicPolicy()
    resolver = _resolver_with_parts(
        active_reader=active,
        candidate_verifier=verify,
        policy_supplier=lambda: policy,
        factory=factory,
    )

    await resolver.resolve_route()
    first_fingerprint = resolver._last_good_fingerprint
    await resolver.resolve_route()

    assert resolver._last_good_fingerprint != first_fingerprint
    assert active.calls == 2
    assert verification_calls == 2
    assert len(factory.calls) == 2
    assert policy.version_reads == policy.generation_reads == 4
    assert policy.lookup_calls == 4
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.parametrize(
    ("embedding_identity", "embedding_dimensions"),
    [("embedding-v2", 2), ("embedding-v1", 3)],
)
@pytest.mark.asyncio
async def test_active_embedding_authority_mismatch_fails_before_m8_factory_or_m6(
    embedding_identity: str, embedding_dimensions: int
) -> None:
    scope = _scope()
    evidence = _lifecycle_evidence(scope).model_copy(
        update={
            "embedding_identity": embedding_identity,
            "embedding_dimensions": embedding_dimensions,
        }
    )
    active = _SequenceActiveReader([_active_state(scope, evidence=evidence)])
    candidate = _CandidateVerifier()
    adapter, client, embedder = _firestore_adapter(scope)
    factory = _Factory({"v1": _binding(scope, adapter)})
    policy = _Policy()
    resolver = _resolver_with_parts(
        active_reader=active,
        candidate_verifier=candidate,
        policy_supplier=lambda: policy,
        factory=factory,
    )

    with pytest.raises(RetrievalError) as caught:
        await resolver.resolve_route()
    _assert_sanitized(caught.value, "store_unavailable")
    assert active.calls == 1
    assert candidate.calls == 0
    assert factory.calls == []
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.parametrize("mismatch", ["scope", "identity", "dimensions"])
@pytest.mark.asyncio
async def test_lifecycle_factory_descriptor_mismatch_is_malformed_before_m6_io(
    mismatch: str,
) -> None:
    requested = _scope()
    binding_scope = _scope("v2") if mismatch == "scope" else requested
    adapter, client, embedder = _firestore_adapter(binding_scope)
    binding = ExactRetrievalAdapterBinding(
        scope=binding_scope,
        embedding_identity="embedding-v2" if mismatch == "identity" else "embedding-v1",
        embedding_dimensions=3 if mismatch == "dimensions" else 2,
        adapter=adapter,
    )
    factory = _Factory({"v1": binding})
    active = _ActiveReader(requested)
    candidate = _CandidateVerifier()
    policy = _Policy()
    resolver = _resolver_with_parts(
        active_reader=active,
        candidate_verifier=candidate,
        policy_supplier=lambda: policy,
        factory=factory,
    )

    with pytest.raises(RetrievalError) as caught:
        await resolver.resolve_route()
    _assert_sanitized(caught.value, "malformed_result")
    assert candidate.calls == 1
    assert len(factory.calls) == 1
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.parametrize(
    ("reachable", "store_ready"),
    [(False, False), (True, False), (True, True)],
)
@pytest.mark.asyncio
async def test_lifecycle_readiness_preserves_m6_booleans_and_composes_exact_fact(
    monkeypatch: pytest.MonkeyPatch,
    reachable: bool,
    store_ready: bool,
) -> None:
    calls: list[object] = []

    async def probe(adapter: FirestoreRetrievalAdapter, scope: RetrievalScope) -> RetrievalProbe:
        del adapter
        calls.append(scope)
        return RetrievalProbe(
            scope=scope,
            reachable=reachable,
            store_ready=store_ready,
            exact_version_ready=False,
        )

    monkeypatch.setattr(FirestoreRetrievalAdapter, "check_readiness", probe)
    resolver, _, verifier, _, _, client, embedder, _ = _lifecycle_resolver()

    result = await resolver.check_readiness()

    assert result == RetrievalProbe(
        scope=_scope(),
        reachable=reachable,
        store_ready=store_ready,
        exact_version_ready=reachable and store_ready,
    )
    assert calls == [_scope()]
    assert verifier.calls == 1
    assert client.readiness_calls == client.vector_calls == embedder.calls == 0


@pytest.mark.asyncio
async def test_lifecycle_readiness_rejects_scope_echo_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe_calls = 0

    async def wrong_probe(
        adapter: FirestoreRetrievalAdapter, scope: RetrievalScope
    ) -> RetrievalProbe:
        nonlocal probe_calls
        del adapter, scope
        probe_calls += 1
        return RetrievalProbe(
            scope=_scope("v2"),
            reachable=True,
            store_ready=True,
            exact_version_ready=False,
        )

    monkeypatch.setattr(FirestoreRetrievalAdapter, "check_readiness", wrong_probe)
    resolver, _, _, _, _, client, embedder, _ = _lifecycle_resolver()

    with pytest.raises(RetrievalError) as caught:
        await resolver.check_readiness()
    _assert_sanitized(caught.value, "malformed_result")
    assert probe_calls == 1
    assert client.readiness_calls == client.vector_calls == embedder.calls == 0


@pytest.mark.parametrize("bad_probe", [None, False, object()])
@pytest.mark.asyncio
async def test_lifecycle_readiness_rejects_non_probe_result_content_free(
    monkeypatch: pytest.MonkeyPatch,
    bad_probe: object,
) -> None:
    async def malformed_probe(adapter: FirestoreRetrievalAdapter, scope: object) -> object:
        del adapter, scope
        return bad_probe

    monkeypatch.setattr(FirestoreRetrievalAdapter, "check_readiness", malformed_probe)
    resolver, _, _, _, _, client, embedder, _ = _lifecycle_resolver()

    with pytest.raises(RetrievalError) as caught:
        await resolver.check_readiness()
    _assert_sanitized(caught.value, "malformed_result")
    assert client.readiness_calls == client.vector_calls == embedder.calls == 0


def test_requested_scope_echo_mismatch_is_rejected_before_retrieval() -> None:
    scope = _scope()
    adapter, client, embedder = _firestore_adapter(scope)
    route = ResolvedRetrievalRoute(
        scope=scope,
        adapter=adapter,
        exact_binding=_binding(scope, adapter),
    )

    with pytest.raises(RetrievalError) as caught:
        validate_route_authority(route, requested_scope=_scope("v2"))
    _assert_sanitized(caught.value, "malformed_result")
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.asyncio
async def test_cancelled_active_resolution_propagates_without_later_work() -> None:
    cancellation = asyncio.CancelledError()
    reader = _FailingActiveReader(cancellation)
    candidate = _CandidateVerifier()
    adapter, client, embedder = _firestore_adapter(_scope())
    factory = _Factory({"v1": _binding(_scope(), adapter)})
    policy = _Policy()
    resolver = _resolver_with_parts(
        active_reader=reader,
        candidate_verifier=candidate,
        policy_supplier=lambda: policy,
        factory=factory,
    )

    with pytest.raises(asyncio.CancelledError) as caught:
        await resolver.resolve_route()
    assert caught.value is cancellation
    assert reader.calls == 1
    assert candidate.calls == 0
    assert factory.calls == []
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.asyncio
async def test_cancelled_active_reread_preserves_prior_cache_without_fallback() -> None:
    cancellation = asyncio.CancelledError()

    class Reader:
        calls = 0

        async def resolve_active_state(
            self, corpus_id: str, trust_policy: AttestationTrustPolicy
        ) -> ResolvedActiveState:
            del corpus_id
            self.calls += 1
            if self.calls == 2:
                raise cancellation
            _ = trust_policy.policy_version
            _ = trust_policy.policy_generation
            identity = AttestationIdentity(algorithm_id="test-algorithm", key_id="test-key")
            assert trust_policy.verifier_for(identity) is not None
            return _active_state(_scope())

    reader = Reader()
    candidate = _CandidateVerifier()
    adapter, client, embedder = _firestore_adapter(_scope())
    factory = _Factory({"v1": _binding(_scope(), adapter)})
    policy = _Policy()
    resolver = _resolver_with_parts(
        active_reader=reader,
        candidate_verifier=candidate,
        policy_supplier=lambda: policy,
        factory=factory,
    )

    assert (await resolver.resolve_route()).scope == _scope()
    fingerprint = resolver._last_good_fingerprint
    with pytest.raises(asyncio.CancelledError) as caught:
        await resolver.resolve_route()

    assert caught.value is cancellation
    assert resolver._last_good_fingerprint == fingerprint
    assert reader.calls == 2
    assert candidate.calls == 1
    assert len(factory.calls) == 1
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.asyncio
async def test_sync_policy_supplier_cancellation_preserves_prior_cache() -> None:
    cancellation = asyncio.CancelledError()
    policy = _Policy()
    supplier_calls = 0

    def supplier() -> _Policy:
        nonlocal supplier_calls
        supplier_calls += 1
        if supplier_calls == 2:
            raise cancellation
        return policy

    resolver, active, verifier, factory, _, client, embedder, _ = _lifecycle_resolver(
        policy_supplier=supplier
    )
    await resolver.resolve_route()
    fingerprint = resolver._last_good_fingerprint

    with pytest.raises(asyncio.CancelledError) as caught:
        await resolver.resolve_route()

    assert caught.value is cancellation
    assert resolver._last_good_fingerprint == fingerprint
    assert supplier_calls == 2
    assert active.calls == 1
    assert verifier.calls == 1
    assert len(factory.calls) == 1
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.asyncio
async def test_sync_refresh_verifier_cancellation_preserves_prior_cache() -> None:
    cancellation = asyncio.CancelledError()

    class CancellingPolicy(_Policy):
        def verifier_for(self, identity: AttestationIdentity) -> _Verifier | None:
            if self.lookup_calls == 3:
                raise cancellation
            return super().verifier_for(identity)

    scope_a = _scope("v1")
    scope_b = _scope("v2")
    active = _SequenceActiveReader(
        [_active_state(scope_a), _active_state(scope_b, pointer_revision=1)]
    )
    candidate = _CandidateVerifier()
    adapter_a, client_a, embedder_a = _firestore_adapter(scope_a)
    adapter_b, client_b, embedder_b = _firestore_adapter(scope_b)
    factory = _Factory(
        {
            "v1": _binding(scope_a, adapter_a),
            "v2": _binding(scope_b, adapter_b),
        }
    )
    policy = CancellingPolicy()
    resolver = _resolver_with_parts(
        active_reader=active,
        candidate_verifier=candidate,
        policy_supplier=lambda: policy,
        factory=factory,
    )
    await resolver.resolve_route()
    fingerprint = resolver._last_good_fingerprint

    with pytest.raises(asyncio.CancelledError) as caught:
        await resolver.resolve_route()

    assert caught.value is cancellation
    assert resolver._last_good_fingerprint == fingerprint
    assert active.calls == 2
    assert candidate.calls == 1
    assert len(factory.calls) == 1
    assert client_a.vector_calls == client_b.vector_calls == 0
    assert client_a.readiness_calls == client_b.readiness_calls == 0
    assert embedder_a.calls == embedder_b.calls == 0


@pytest.mark.asyncio
async def test_sync_factory_cancellation_propagates_without_adapter_io() -> None:
    cancellation = asyncio.CancelledError()
    scope_a = _scope("v1")
    scope_b = _scope("v2")
    adapter_a, client_a, embedder_a = _firestore_adapter(scope_a)
    adapter_b, client_b, embedder_b = _firestore_adapter(scope_b)

    class CancellingFactory(_Factory):
        def adapter_for(
            self,
            scope: ExactCorpusReference,
            *,
            embedding_identity: str,
            embedding_dimensions: int,
        ) -> ExactRetrievalAdapterBinding:
            if scope == scope_b:
                self.calls.append((scope, embedding_identity, embedding_dimensions))
                raise cancellation
            return super().adapter_for(
                scope,
                embedding_identity=embedding_identity,
                embedding_dimensions=embedding_dimensions,
            )

    active = _SequenceActiveReader(
        [_active_state(scope_a), _active_state(scope_b, pointer_revision=1)]
    )
    candidate = _CandidateVerifier()
    factory = CancellingFactory(
        {
            "v1": _binding(scope_a, adapter_a),
            "v2": _binding(scope_b, adapter_b),
        }
    )
    policy = _Policy()
    resolver = _resolver_with_parts(
        active_reader=active,
        candidate_verifier=candidate,
        policy_supplier=lambda: policy,
        factory=factory,
    )
    await resolver.resolve_route()

    with pytest.raises(asyncio.CancelledError) as caught:
        await resolver.resolve_route()

    assert caught.value is cancellation
    assert active.calls == candidate.calls == 2
    assert [call[0] for call in factory.calls] == [scope_a, scope_b]
    assert client_a.vector_calls == client_b.vector_calls == 0
    assert client_a.readiness_calls == client_b.readiness_calls == 0
    assert embedder_a.calls == embedder_b.calls == 0


@pytest.mark.asyncio
async def test_cancelled_refresh_preserves_previous_cache_and_never_falls_back() -> None:
    scope_a = _scope("v1")
    scope_b = _scope("v2")
    active = _SequenceActiveReader(
        [_active_state(scope_a), _active_state(scope_b, pointer_revision=1)]
    )
    adapter_a, client_a, embedder_a = _firestore_adapter(scope_a)
    adapter_b, client_b, embedder_b = _firestore_adapter(scope_b)
    factory = _Factory(
        {
            "v1": _binding(scope_a, adapter_a),
            "v2": _binding(scope_b, adapter_b),
        }
    )
    refresh_calls = 0
    cancellation = asyncio.CancelledError()

    async def refresh(
        corpus: ExactCorpusReference,
        identity: AttestationIdentity,
        verifier: AttestationVerifier,
    ) -> VerifiedCandidateEvidence:
        nonlocal refresh_calls
        del identity, verifier
        refresh_calls += 1
        if refresh_calls == 2:
            raise cancellation
        return _candidate_evidence(corpus)

    policy = _Policy()
    resolver = _resolver_with_parts(
        active_reader=active,
        candidate_verifier=refresh,
        policy_supplier=lambda: policy,
        factory=factory,
    )

    assert (await resolver.resolve_route()).scope == scope_a
    fingerprint_a = resolver._last_good_fingerprint
    with pytest.raises(asyncio.CancelledError) as caught:
        await resolver.resolve_route()
    assert caught.value is cancellation
    assert resolver._last_good_fingerprint == fingerprint_a
    assert [call[0] for call in factory.calls] == [scope_a]

    assert (await resolver.resolve_route()).scope == scope_b
    assert refresh_calls == 3
    assert resolver._last_good_fingerprint != fingerprint_a
    assert [call[0] for call in factory.calls] == [scope_a, scope_b]
    assert client_a.vector_calls == client_b.vector_calls == 0
    assert client_a.readiness_calls == client_b.readiness_calls == 0
    assert embedder_a.calls == embedder_b.calls == 0


@pytest.mark.asyncio
async def test_failed_refresh_preserves_previous_cache_and_does_not_return_old_route() -> None:
    scope_a = _scope("v1")
    scope_b = _scope("v2")
    active = _SequenceActiveReader(
        [_active_state(scope_a), _active_state(scope_b, pointer_revision=1)]
    )
    adapter_a, client_a, embedder_a = _firestore_adapter(scope_a)
    adapter_b, client_b, embedder_b = _firestore_adapter(scope_b)
    factory = _Factory(
        {
            "v1": _binding(scope_a, adapter_a),
            "v2": _binding(scope_b, adapter_b),
        }
    )
    refresh_calls = 0

    async def refresh(
        corpus: ExactCorpusReference,
        identity: AttestationIdentity,
        verifier: AttestationVerifier,
    ) -> VerifiedCandidateEvidence:
        nonlocal refresh_calls
        del identity, verifier
        refresh_calls += 1
        if refresh_calls == 2:
            raise CandidatePersistenceError("malformed_store")
        return _candidate_evidence(corpus)

    policy = _Policy()
    resolver = _resolver_with_parts(
        active_reader=active,
        candidate_verifier=refresh,
        policy_supplier=lambda: policy,
        factory=factory,
    )

    assert (await resolver.resolve_route()).scope == scope_a
    fingerprint_a = resolver._last_good_fingerprint
    with pytest.raises(RetrievalError) as caught:
        await resolver.resolve_route()

    _assert_sanitized(caught.value, "store_unavailable")
    assert resolver._last_good_fingerprint == fingerprint_a
    assert [call[0] for call in factory.calls] == [scope_a]
    assert client_a.vector_calls == client_b.vector_calls == 0
    assert client_a.readiness_calls == client_b.readiness_calls == 0
    assert embedder_a.calls == embedder_b.calls == 0


@pytest.mark.asyncio
async def test_cancelled_readiness_propagates_and_has_no_retrieval_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = asyncio.CancelledError()
    readiness_calls = 0

    async def cancelled_probe(
        adapter: FirestoreRetrievalAdapter, scope: RetrievalScope
    ) -> RetrievalProbe:
        nonlocal readiness_calls
        del adapter, scope
        readiness_calls += 1
        raise cancellation

    monkeypatch.setattr(FirestoreRetrievalAdapter, "check_readiness", cancelled_probe)
    resolver, _, verifier, _, _, client, embedder, _ = _lifecycle_resolver()

    with pytest.raises(asyncio.CancelledError) as caught:
        await resolver.check_readiness()
    assert caught.value is cancellation
    assert readiness_calls == 1
    assert verifier.calls == 1
    fingerprint = resolver._last_good_fingerprint
    assert fingerprint is not None
    assert (await resolver.resolve_route()).scope == _scope()
    assert resolver._last_good_fingerprint == fingerprint
    assert verifier.calls == 1
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.parametrize(
    "code",
    [
        "invalid_request",
        "unsupported_scope",
        "store_unavailable",
        "malformed_result",
        "context_too_large",
    ],
)
@pytest.mark.asyncio
async def test_static_readiness_preserves_existing_m6_error_code(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    async def failed_probe(
        adapter: FirestoreRetrievalAdapter, scope: RetrievalScope
    ) -> RetrievalProbe:
        del adapter, scope
        raise RetrievalError(cast(Any, code))

    monkeypatch.setattr(FirestoreRetrievalAdapter, "check_readiness", failed_probe)
    scope = _scope()
    adapter, client, embedder = _firestore_adapter(scope)
    resolver = StaticRetrievalRouteResolver(
        ResolvedRetrievalRoute(
            scope=scope,
            adapter=adapter,
            exact_binding=_binding(scope, adapter),
        )
    )

    with pytest.raises(RetrievalError) as caught:
        await resolver.check_readiness()
    _assert_sanitized(caught.value, code)
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


@pytest.mark.asyncio
async def test_factory_exception_is_malformed_without_raw_context_or_io() -> None:
    canary = "SECRET-FACTORY-FAILURE"

    class FailingFactory:
        calls = 0

        def adapter_for(
            self,
            scope: ExactCorpusReference,
            *,
            embedding_identity: str,
            embedding_dimensions: int,
        ) -> ExactRetrievalAdapterBinding:
            del scope, embedding_identity, embedding_dimensions
            self.calls += 1
            raise RuntimeError(canary)

    factory = FailingFactory()
    active = _ActiveReader(_scope())
    candidate = _CandidateVerifier()
    policy = _Policy()
    resolver = _resolver_with_parts(
        active_reader=active,
        candidate_verifier=candidate,
        policy_supplier=lambda: policy,
        factory=factory,
    )

    with pytest.raises(RetrievalError) as caught:
        await resolver.resolve_route()
    _assert_sanitized(caught.value, "malformed_result")
    assert canary not in repr(caught.value)
    assert factory.calls == 1
    assert candidate.calls == 1


@pytest.mark.asyncio
async def test_policy_supplier_failure_is_fixed_and_calls_nothing_later() -> None:
    canary = "SECRET-POLICY-SUPPLIER-FAILURE"
    supplier_calls = 0

    def supplier() -> AttestationTrustPolicy:
        nonlocal supplier_calls
        supplier_calls += 1
        raise RuntimeError(canary)

    active = _ActiveReader(_scope())
    candidate = _CandidateVerifier()
    adapter, client, embedder = _firestore_adapter(_scope())
    factory = _Factory({"v1": _binding(_scope(), adapter)})
    resolver = _resolver_with_parts(
        active_reader=active,
        candidate_verifier=candidate,
        policy_supplier=supplier,
        factory=factory,
    )

    with pytest.raises(RetrievalError) as caught:
        await resolver.resolve_route()
    _assert_sanitized(caught.value, "store_unavailable")
    assert canary not in repr(caught.value)
    assert supplier_calls == 1
    assert active.calls == candidate.calls == 0
    assert factory.calls == []
    assert client.vector_calls == client.readiness_calls == embedder.calls == 0


def test_resolver_construction_rejects_invalid_inputs_before_lifecycle_work() -> None:
    valid = {
        "corpus_id": "public-docs",
        "active_state_resolver": _ActiveReader(_scope()),
        "verify_attested_candidate": _CandidateVerifier(),
        "trust_policy_supplier": lambda: _Policy(),
        "expected_embedding_identity": "embedding-v1",
        "expected_embedding_dimensions": 2,
        "adapter_factory": _Factory({}),
    }
    invalid_updates = (
        {"corpus_id": "active"},
        {"active_state_resolver": object()},
        {"verify_attested_candidate": object()},
        {"trust_policy_supplier": object()},
        {"expected_embedding_identity": " bad"},
        {"expected_embedding_dimensions": True},
        {"adapter_factory": object()},
    )

    for update in invalid_updates:
        with pytest.raises(RetrievalError) as caught:
            LifecycleRetrievalRouteResolver(**cast(Any, valid | update))
        _assert_sanitized(caught.value, "invalid_request")
    assert cast(_ActiveReader, valid["active_state_resolver"]).calls == 0
    assert cast(_CandidateVerifier, valid["verify_attested_candidate"]).calls == 0


@pytest.mark.asyncio
async def test_resolvers_are_read_only_caller_owned_and_make_no_network_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    network_calls = 0

    def forbidden_network(*args: object, **kwargs: object) -> object:
        nonlocal network_calls
        del args, kwargs
        network_calls += 1
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket, "create_connection", forbidden_network)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden_network)
    resolver, active, verifier, factory, _, client, embedder, policy = _lifecycle_resolver()

    await resolver.resolve_route()
    await resolver.check_readiness()

    assert network_calls == 0
    assert active.calls == 2
    assert verifier.calls == 1
    assert len(factory.calls) == 2
    assert policy.lookup_calls == 3
    assert client.close_calls == 0
    assert client.vector_calls == 0
    assert client.readiness_calls == 1
    assert embedder.calls == 0
    assert not hasattr(resolver, "aclose")


def test_global_no_external_guard_has_exercised_negative_controls() -> None:
    from app.corpus_lifecycle_firestore import create_corpus_lifecycle_store
    from app.ingest.candidate_firestore import create_candidate_store
    from app.providers.gemini import GeminiProvider
    from app.providers.ollama import OllamaProvider
    from app.retrieval_firestore import create_firestore_vector_client

    def connect_socket() -> object:
        with socket.socket() as candidate:
            candidate.connect(("example.test", 443))
        return object()

    operations: list[Callable[[], object]] = [
        lambda: socket.getaddrinfo("example.test", 443),
        lambda: socket.create_connection(("example.test", 443)),
        connect_socket,
        lambda: os.getenv("SECRET_KEY_CANARY"),
        lambda: GeminiProvider.stream(cast(GeminiProvider, object()), cast(Any, object())),
        lambda: OllamaProvider.stream(cast(OllamaProvider, object()), cast(Any, object())),
        lambda: create_firestore_vector_client("forbidden-project"),
        lambda: create_candidate_store("forbidden-project"),
        lambda: create_corpus_lifecycle_store("forbidden-project"),
    ]
    if _google_auth_available():
        google_auth = importlib.import_module("google.auth")
        operations.append(lambda: cast(Any, google_auth).default())

    for operation in operations:
        with pytest.raises(
            AssertionError,
            match="external access forbidden in retrieval-route tests",
        ):
            operation()


def test_google_auth_probe_is_safe_when_parent_package_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_parent(name: str) -> None:
        del name
        raise ModuleNotFoundError("google")

    monkeypatch.setattr(importlib.util, "find_spec", missing_parent)

    assert _google_auth_available() is False
