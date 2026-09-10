import asyncio
import hashlib
import json
import socket
from collections.abc import Callable, Mapping
from typing import Literal, cast

import pytest
from pydantic import TypeAdapter

from app.ingest.candidate_persistence import (
    ATTESTATION_RECORD_SCHEMA_VERSION,
    CANDIDATE_RECORD_SCHEMA_VERSION,
    DOCUMENT_RECORD_SCHEMA_VERSION,
    AttestationSigner,
    CandidatePersistenceError,
    CandidatePersistenceRequest,
    CandidatePersistenceService,
    CandidateRecordKind,
    CandidateStorePage,
    CandidateStoreRecord,
    StrictStoreValue,
    candidate_store_key,
    canonical_json_bytes,
    expected_candidate_records,
)
from app.ingest.planner import (
    CandidateDocumentSnapshot,
    CandidateIngestionPlan,
    CandidateSourceSnapshot,
    EmbeddingSpecification,
    plan_candidate,
)
from app.retrieval_contracts import ExactCorpusReference


def _plan(
    *,
    dimensions: int = 2,
    version: str = "2026.09.10",
    content: bytes = b"Reviewed candidate content",
) -> CandidateIngestionPlan:
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
        corpus=ExactCorpusReference(corpus_id="public-docs", corpus_version=version),
        source=CandidateSourceSnapshot(
            manifest_bytes=manifest,
            documents=(CandidateDocumentSnapshot(relative_path="guide.md", content=content),),
        ),
        embedding=EmbeddingSpecification(identity="fixture-embedding-v1", dimensions=dimensions),
        embed=lambda values: tuple(tuple(float(i + 1) for i in range(dimensions)) for _ in values),
    )


class MemoryStore:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], CandidateStoreRecord] = {}
        self.calls: list[tuple[object, ...]] = []
        self.closed = 0

    def encoded_document_size(self, record: CandidateStoreRecord) -> int:
        self.calls.append(("encoded_document_size", record.kind, record.key))
        return len(repr(record.value).encode())

    def encoded_create_size(
        self, kind: Literal["candidate", "document", "chunk", "attestation"],
        records: tuple[CandidateStoreRecord, ...],
    ) -> int:
        self.calls.append(("encoded_create_size", kind, tuple(r.key for r in records)))
        return sum(self.encoded_document_size(record) + 64 for record in records)

    async def get(
        self, kind: CandidateRecordKind, key: str, *, timeout_seconds: int
    ) -> CandidateStoreRecord | None:
        self.calls.append(("get", kind, key, timeout_seconds))
        return self.records.get((kind, key))

    async def get_many(
        self,
        kind: CandidateRecordKind,
        keys: tuple[str, ...],
        *,
        timeout_seconds: int,
    ) -> tuple[CandidateStoreRecord | None, ...]:
        self.calls.append(("get_many", kind, keys, timeout_seconds))
        return tuple(self.records.get((kind, key)) for key in keys)

    async def create_many(
        self,
        kind: CandidateRecordKind,
        records: tuple[CandidateStoreRecord, ...],
        *,
        timeout_seconds: int,
    ) -> None:
        self.calls.append(("create_many", kind, tuple(r.key for r in records), timeout_seconds))
        if any((kind, record.key) in self.records for record in records):
            from app.ingest.candidate_persistence import CandidateStoreFailure

            raise CandidateStoreFailure("conflict")
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
        self.calls.append(("list_page", kind, after_key, limit, timeout_seconds))
        records = sorted(
            (
                record
                for (record_kind, _), record in self.records.items()
                if record_kind == kind
                and record.value.get("corpus_id") == corpus.corpus_id
                and record.value.get("corpus_version") == corpus.corpus_version
            ),
            key=lambda record: record.key.encode(),
        )
        if after_key is not None:
            records = [record for record in records if record.key.encode() > after_key.encode()]
        page = tuple(records[:limit])
        return CandidateStorePage(
            records=page,
            next_after_key=page[-1].key if len(records) > limit else None,
        )

    async def aclose(self) -> None:
        self.closed += 1


class FixtureSigner:
    algorithm_id = "cairn-test-sha256-v1"
    key_id = "fixture-key-1"

    def __init__(self) -> None:
        self.sign_calls = 0
        self.verify_calls = 0

    async def sign(self, payload: bytes) -> bytes:
        self.sign_calls += 1
        return hashlib.sha256(b"fixture-only\0" + payload).digest()

    async def verify(self, payload: bytes, signature: bytes) -> bool:
        self.verify_calls += 1
        return signature == hashlib.sha256(b"fixture-only\0" + payload).digest()


def _service(
    store: MemoryStore, signer: AttestationSigner | None = None, **changes: object
) -> CandidatePersistenceService:
    kwargs: dict[str, object] = {
        "store": store,
        "signer": signer or FixtureSigner(),
        "expected_embedding_identity": "fixture-embedding-v1",
        "expected_embedding_dimensions": 2,
        "timeout_seconds": 3,
        "max_retries": 1,
        "sleep": _no_sleep,
    }
    kwargs.update(changes)
    return CandidatePersistenceService(**kwargs)  # type: ignore[arg-type]


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_: object, **__: object) -> object:
        raise AssertionError("external I/O forbidden in candidate persistence tests")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr("app.providers.gemini.GeminiProvider.stream", forbidden)
    monkeypatch.setattr("app.providers.ollama.OllamaProvider.stream", forbidden)
    monkeypatch.setattr("app.retrieval_firestore.create_firestore_vector_client", forbidden)
    monkeypatch.setattr("app.ingest.candidate_firestore.create_candidate_store", forbidden)


def test_candidate_key_is_exact_stable_and_scope_isolated() -> None:
    plan = _plan()
    assert candidate_store_key(plan.corpus) == candidate_store_key(plan.corpus)
    assert candidate_store_key(plan.corpus).startswith("cand1-")
    assert candidate_store_key(_plan(version="2026.09.11").corpus) != candidate_store_key(
        plan.corpus
    )


def test_public_canonical_json_bytes_is_stable_and_content_free_on_failure() -> None:
    assert canonical_json_bytes({"z": "café", "a": [True, None, 3]}) == (
        b'{"a":[true,null,3],"z":"caf\xc3\xa9"}'
    )

    with pytest.raises(CandidatePersistenceError) as caught:
        canonical_json_bytes({"private": float("nan")})

    assert caught.value.code == "invalid_plan"
    assert "private" not in str(caught.value)


@pytest.mark.asyncio
async def test_create_confirm_and_exact_mappings() -> None:
    plan = _plan()
    store = MemoryStore()
    signer = FixtureSigner()
    service = _service(store, signer)
    request = CandidatePersistenceRequest(contract_version="1.0", plan=plan)

    created = await service.persist_and_attest_candidate(request)
    confirmed = await service.persist_and_attest_candidate(request)

    assert created.disposition == "created"
    assert confirmed.disposition == "confirmed"
    assert created.corpus == plan.corpus
    assert created.plan_sha256 == plan.plan_sha256
    assert created.document_count == 1
    assert created.chunk_count == 1
    assert signer.sign_calls == 1
    assert {kind for kind, _ in store.records} == {
        "candidate",
        "document",
        "chunk",
        "attestation",
    }
    header = store.records[("candidate", candidate_store_key(plan.corpus))]
    assert header.value == {
        "schema_version": CANDIDATE_RECORD_SCHEMA_VERSION,
        "plan_contract_version": "1.0",
        "corpus_id": "public-docs",
        "corpus_version": "2026.09.10",
        "plan_sha256": plan.plan_sha256,
        "semantic_manifest_sha256": plan.semantic_manifest_sha256,
        "embedding_identity": "fixture-embedding-v1",
        "embedding_dimensions": 2,
        "document_count": 1,
        "chunk_count": 1,
    }
    document = next(record for (kind, _), record in store.records.items() if kind == "document")
    assert document.value["schema_version"] == DOCUMENT_RECORD_SCHEMA_VERSION
    assert document.value["provenance"] == {
        "title": "Public guide",
        "url": "https://docs.example.test/guide",
        "owner": "Docs",
        "reviewed_at": "2026-09-10",
        "source_sha256": plan.documents[0].provenance.source_sha256,
    }
    attestation = store.records[("attestation", candidate_store_key(plan.corpus))]
    assert attestation.value["schema_version"] == ATTESTATION_RECORD_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_dimension_mismatch_fails_before_store_or_signer() -> None:
    store = MemoryStore()
    signer = FixtureSigner()
    service = _service(store, signer)
    with pytest.raises(CandidatePersistenceError) as caught:
        await service.persist_and_attest_candidate(
            CandidatePersistenceRequest(contract_version="1.0", plan=_plan(dimensions=3))
        )
    assert caught.value.code == "unsupported_embedding"
    assert store.calls == []
    assert signer.sign_calls == signer.verify_calls == 0

    over_limit = MemoryStore()
    with pytest.raises(CandidatePersistenceError) as over:
        await _service(
            over_limit,
            expected_embedding_dimensions=2048,
        ).persist_and_attest_candidate(
            CandidatePersistenceRequest(
                contract_version="1.0", plan=_plan(dimensions=2049)
            )
        )
    assert over.value.code == "unsupported_embedding"
    assert over_limit.calls == []


@pytest.mark.asyncio
async def test_exact_2048_dimension_candidate_is_preserved() -> None:
    plan = _plan(dimensions=2048)
    store = MemoryStore()
    receipt = await _service(
        store,
        expected_embedding_dimensions=2048,
    ).persist_and_attest_candidate(
        CandidatePersistenceRequest(contract_version="1.0", plan=plan)
    )
    assert receipt.disposition == "created"
    chunk = next(record for (kind, _), record in store.records.items() if kind == "chunk")
    assert chunk.value["embedding"] == plan.documents[0].chunks[0].embedding


@pytest.mark.asyncio
async def test_different_plan_conflicts_before_child_or_signer() -> None:
    first = _plan()
    store = MemoryStore()
    service = _service(store)
    await service.persist_and_attest_candidate(
        CandidatePersistenceRequest(contract_version="1.0", plan=first)
    )
    store.calls.clear()
    different = _plan(content=b"Different reviewed content")
    with pytest.raises(CandidatePersistenceError) as caught:
        await service.persist_and_attest_candidate(
            CandidatePersistenceRequest(contract_version="1.0", plan=different)
        )
    assert caught.value.code == "candidate_conflict"
    assert not any(call[0] in {"list_page", "create_many"} for call in store.calls)


@pytest.mark.asyncio
async def test_owned_store_closes_once_and_shared_store_does_not_close() -> None:
    owned = MemoryStore()
    service = _service(owned, owns_store=True)
    await service.aclose()
    await service.aclose()
    assert owned.closed == 1
    with pytest.raises(CandidatePersistenceError) as caught:
        await service.persist_and_attest_candidate(
            CandidatePersistenceRequest(contract_version="1.0", plan=_plan())
        )
    assert caught.value.code == "store_unavailable"

    shared = MemoryStore()
    shared_service = _service(shared)
    await shared_service.aclose()
    assert shared.closed == 0


@pytest.mark.asyncio
async def test_incomplete_exact_prefix_resumes_and_read_only_verifier_never_signs() -> None:
    plan = _plan()
    request = CandidatePersistenceRequest(contract_version="1.0", plan=plan)
    store = MemoryStore()
    initial_signer = FixtureSigner()
    await _service(store, initial_signer).persist_and_attest_candidate(request)
    _, _, chunks = expected_candidate_records(plan)
    store.records.pop(("chunk", chunks[0].key))
    store.records.pop(("attestation", candidate_store_key(plan.corpus)))

    resumed_signer = FixtureSigner()
    resumed = await _service(store, resumed_signer).persist_and_attest_candidate(request)
    assert resumed.disposition == "resumed"
    assert resumed_signer.sign_calls == 1

    class VerifierOnly:
        algorithm_id = "cairn-test-sha256-v1"
        key_id = "fixture-key-1"

        def __init__(self) -> None:
            self.verify_calls = 0

        async def verify(self, payload: bytes, signature: bytes) -> bool:
            self.verify_calls += 1
            return signature == hashlib.sha256(b"fixture-only\0" + payload).digest()

    verifier = VerifierOnly()
    store.calls.clear()
    verified = await _service(store).verify_attested_candidate(
        request, verifier=verifier
    )
    assert verified.disposition == "confirmed"
    assert verifier.verify_calls == 1
    assert not any(call[0] == "create_many" for call in store.calls)


@pytest.mark.asyncio
async def test_unknown_header_commit_is_confirmed_not_claimed_created() -> None:
    class UnknownCommittedStore(MemoryStore):
        def __init__(self) -> None:
            super().__init__()
            self.ambiguous = True

        async def create_many(
            self,
            kind: CandidateRecordKind,
            records: tuple[CandidateStoreRecord, ...],
            *,
            timeout_seconds: int,
        ) -> None:
            await super().create_many(
                kind, records, timeout_seconds=timeout_seconds
            )
            if kind == "candidate" and self.ambiguous:
                self.ambiguous = False
                from app.ingest.candidate_persistence import CandidateStoreFailure

                raise CandidateStoreFailure("transient")

    store = UnknownCommittedStore()
    receipt = await _service(store).persist_and_attest_candidate(
        CandidatePersistenceRequest(contract_version="1.0", plan=_plan())
    )
    assert receipt.disposition == "confirmed"


def test_public_models_are_recursive_immutable_strict_and_content_free() -> None:
    nested = {"nested": {"value": "before"}}
    record = CandidateStoreRecord(kind="candidate", key="safe", value=nested)
    nested["nested"]["value"] = "after"
    frozen_nested = record.value["nested"]
    assert isinstance(frozen_nested, Mapping)
    assert frozen_nested["value"] == "before"
    with pytest.raises(TypeError):
        record.value["new"] = "blocked"  # type: ignore[index]

    canary = "PRIVATE-CANDIDATE-CANARY"
    surfaces: tuple[Callable[[], object], ...] = (
        lambda: CandidateStoreRecord(
            kind="candidate",
            key="safe",
            value=cast(Mapping[str, StrictStoreValue], {"bad": [canary]}),
        ),
        lambda: CandidateStoreRecord.model_validate(
            {"kind": "candidate", "key": "safe", "value": {}, canary: canary},
            extra="ignore",
        ),
        lambda: TypeAdapter(CandidateStoreRecord).validate_python(
            {"kind": "candidate", "key": "safe", "value": {}, canary: canary},
            extra="allow",
        ),
        lambda: record.model_copy(update={canary: canary}),
    )
    for surface in surfaces:
        with pytest.raises(CandidatePersistenceError) as caught:
            surface()
        rendered = (
            str(caught.value)
            + repr(caught.value)
            + repr(caught.value.errors(include_input=True))
            + caught.value.json(include_input=True)
            + repr(caught.value.__cause__)
            + repr(caught.value.__context__)
        )
        assert canary not in rendered


def test_batch_planning_splits_at_400_and_detects_size_drift() -> None:
    store = MemoryStore()
    service = _service(store)
    records = tuple(
        CandidateStoreRecord(kind="document", key=f"doc_{index:064x}", value={"x": index})
        for index in range(401)
    )
    assert [len(batch) for batch in service._plan_batches("document", records)] == [400, 1]

    class DriftingStore(MemoryStore):
        def __init__(self) -> None:
            super().__init__()
            self.offset = 0

        def encoded_create_size(
            self,
            kind: CandidateRecordKind,
            records: tuple[CandidateStoreRecord, ...],
        ) -> int:
            self.offset += 1
            return super().encoded_create_size(kind, records) + self.offset

    drifting = DriftingStore()
    with pytest.raises(CandidatePersistenceError) as caught:
        asyncio.run(
            _service(drifting).persist_and_attest_candidate(
                CandidatePersistenceRequest(contract_version="1.0", plan=_plan())
            )
        )
    assert caught.value.code == "malformed_store"


@pytest.mark.asyncio
async def test_document_bound_and_transient_read_retry_are_bounded() -> None:
    class OversizedStore(MemoryStore):
        def encoded_document_size(self, record: CandidateStoreRecord) -> int:
            super().encoded_document_size(record)
            return 1_048_577

    oversized = OversizedStore()
    with pytest.raises(CandidatePersistenceError) as too_large:
        await _service(oversized).persist_and_attest_candidate(
            CandidatePersistenceRequest(contract_version="1.0", plan=_plan())
        )
    assert too_large.value.code == "store_bounds_exceeded"
    assert not any(call[0] in {"get", "get_many", "create_many"} for call in oversized.calls)

    class RetryStore(MemoryStore):
        def __init__(self) -> None:
            super().__init__()
            self.failures = 1

        async def get(
            self, kind: CandidateRecordKind, key: str, *, timeout_seconds: int
        ) -> CandidateStoreRecord | None:
            if self.failures:
                self.failures -= 1
                from app.ingest.candidate_persistence import CandidateStoreFailure

                raise CandidateStoreFailure("transient")
            return await super().get(kind, key, timeout_seconds=timeout_seconds)

    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    retried = RetryStore()
    receipt = await _service(retried, sleep=sleep).persist_and_attest_candidate(
        CandidatePersistenceRequest(contract_version="1.0", plan=_plan())
    )
    assert receipt.disposition == "created"
    assert sleeps == [0.1]
    timed_calls = {"get", "get_many", "create_many", "list_page"}
    assert all(call[-1] == 3 for call in retried.calls if call[0] in timed_calls)


def test_no_external_guard_has_executable_negative_controls() -> None:
    for operation in (
        lambda: socket.getaddrinfo("localhost", 80),
        lambda: socket.create_connection(("127.0.0.1", 9)),
        lambda: socket.socket().connect(("127.0.0.1", 9)),
    ):
        with pytest.raises(AssertionError, match="external I/O forbidden"):
            operation()
