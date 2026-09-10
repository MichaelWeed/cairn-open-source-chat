import asyncio
import base64
import hashlib
import importlib
import json
import math
import os
import socket
import subprocess
import sys
from collections.abc import Callable, Mapping
from functools import partial
from typing import Any, cast

import pytest
from pydantic import TypeAdapter

from app.ingest.candidate_persistence import (
    ATTESTATION_RECORD_SCHEMA_VERSION,
    CANDIDATE_RECORD_SCHEMA_VERSION,
    DOCUMENT_RECORD_SCHEMA_VERSION,
    AttestationCorpus,
    AttestationEmbedding,
    AttestationIdentity,
    AttestationPayload,
    AttestationRecordSchemas,
    AttestationSigner,
    CandidateAttestation,
    CandidateAttestationVerificationService,
    CandidatePersistenceError,
    CandidatePersistenceReceipt,
    CandidatePersistenceRequest,
    CandidatePersistenceService,
    CandidateRecordKind,
    CandidateStorePage,
    CandidateStoreRecord,
    StrictStoreValue,
    VerifiedCandidateEvidence,
    _CandidateStoreMalformed,
    candidate_inventory_sha256,
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

    def __repr__(self) -> str:
        return "<memory-candidate-store>"

    def encoded_create_base_size(self, kind: CandidateRecordKind) -> int:
        self.calls.append(("encoded_create_base_size", kind))
        return 16

    def encoded_record_sizes(self, record: CandidateStoreRecord) -> tuple[int, int, str]:
        self.calls.append(("encoded_record_sizes", record.kind, record.key))
        document_size = len(repr(record.value).encode())
        return document_size, document_size + 64, candidate_inventory_sha256((record,))

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

    async def create_many_checked(
        self,
        kind: CandidateRecordKind,
        records: tuple[CandidateStoreRecord, ...],
        *,
        expected_encoded_size: int,
        expected_write_sha256s: tuple[str, ...],
        timeout_seconds: int,
    ) -> None:
        self.calls.append(("create_many", kind, tuple(r.key for r in records), timeout_seconds))
        agreements = tuple(self.encoded_record_sizes(record) for record in records)
        actual_size = self.encoded_create_base_size(kind) + sum(
            agreement[1] for agreement in agreements
        )
        if (
            actual_size != expected_encoded_size
            or tuple(agreement[2] for agreement in agreements) != expected_write_sha256s
        ):
            raise _CandidateStoreMalformed()
        if any((kind, record.key) in self.records for record in records):
            from app.ingest.candidate_persistence import CandidateStoreFailure

            raise CandidateStoreFailure("conflict")
        for record in records:
            self.records[(kind, record.key)] = record

    async def create_many(
        self,
        kind: CandidateRecordKind,
        records: tuple[CandidateStoreRecord, ...],
        *,
        timeout_seconds: int,
    ) -> None:
        agreements = tuple(self.encoded_record_sizes(record) for record in records)
        await self.create_many_checked(
            kind,
            records,
            expected_encoded_size=self.encoded_create_base_size(kind)
            + sum(agreement[1] for agreement in agreements),
            expected_write_sha256s=tuple(agreement[2] for agreement in agreements),
            timeout_seconds=timeout_seconds,
        )

    def encoded_document_size(self, record: CandidateStoreRecord) -> int:
        return self.encoded_record_sizes(record)[0]

    def encoded_create_size(
        self, kind: CandidateRecordKind, records: tuple[CandidateStoreRecord, ...]
    ) -> int:
        return self.encoded_create_base_size(kind) + sum(
            self.encoded_record_sizes(record)[1] for record in records
        )

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


def _verification(store: MemoryStore, **changes: object) -> CandidateAttestationVerificationService:
    kwargs: dict[str, object] = {
        "store": store,
        "timeout_seconds": 3,
        "max_retries": 1,
        "sleep": _no_sleep,
    }
    kwargs.update(changes)
    return CandidateAttestationVerificationService(**kwargs)  # type: ignore[arg-type]


def _mutable(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _mutable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_mutable(item) for item in value]
    return value


def _exception_chain_rendered(error: BaseException) -> str:
    pending = [error]
    seen: set[int] = set()
    rendered: list[str] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        rendered.extend(
            (
                str(current),
                repr(current),
                repr(current.args),
                repr(vars(current)),
            )
        )
        if isinstance(current, CandidatePersistenceError):
            rendered.append(repr(current.errors(include_input=True)))
            rendered.append(current.json(include_input=True))
        for nested in (current.__cause__, current.__context__):
            if nested is not None:
                pending.append(nested)
    return "\n".join(rendered)


def _assert_content_free_error(
    error: CandidatePersistenceError, sensitive_values: tuple[str, ...]
) -> None:
    rendered = _exception_chain_rendered(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    for value in sensitive_values:
        assert value not in rendered


def _leaf_paths(
    value: object, prefix: tuple[str | int, ...] = ()
) -> tuple[tuple[str | int, ...], ...]:
    if isinstance(value, Mapping):
        return tuple(
            path for key, item in value.items() for path in _leaf_paths(item, (*prefix, key))
        )
    if isinstance(value, (tuple, list)):
        return tuple(
            path for index, item in enumerate(value) for path in _leaf_paths(item, (*prefix, index))
        )
    return (prefix,)


def _mutate_leaf(value: object, path: tuple[str | int, ...]) -> object:
    if not path:
        if type(value) is bool:
            return not value
        if type(value) is int:
            return value + 1
        if type(value) is float:
            return value + 0.5
        if type(value) is str:
            return value + "x"
        if value is None:
            return "changed"
        raise AssertionError(f"unsupported test leaf: {type(value)!r}")
    first, *rest = path
    if isinstance(value, Mapping) and type(first) is str:
        return {
            key: _mutate_leaf(item, tuple(rest)) if key == first else item
            for key, item in value.items()
        }
    if isinstance(value, tuple) and type(first) is int:
        return tuple(
            _mutate_leaf(item, tuple(rest)) if index == first else item
            for index, item in enumerate(value)
        )
    raise AssertionError("invalid test mutation path")


def _resign_store_attestation(store: MemoryStore, corpus: ExactCorpusReference) -> None:
    key = candidate_store_key(corpus)
    inventory_records = tuple(
        record
        for kind in ("candidate", "document", "chunk")
        for _, record in sorted(
            (
                (record_key, record)
                for (record_kind, record_key), record in store.records.items()
                if record_kind == kind
            ),
            key=lambda item: item[0].encode("utf-8"),
        )
    )
    value = cast(dict[str, object], _mutable(store.records[("attestation", key)].value))
    payload = cast(dict[str, object], value["payload"])
    payload["inventory_sha256"] = candidate_inventory_sha256(inventory_records)
    payload_bytes = canonical_json_bytes(payload)
    value["payload_sha256"] = hashlib.sha256(payload_bytes).hexdigest()
    signature = hashlib.sha256(b"fixture-only\0" + payload_bytes).digest()
    value["signature_b64url"] = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    store.records[("attestation", key)] = CandidateStoreRecord(
        kind="attestation", key=key, value=cast(Any, value)
    )


def _invoke(method: Any, *args: Any, **kwargs: Any) -> object:
    return method(*args, **kwargs)


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
    try:
        google_auth = importlib.import_module("google.auth")
    except ModuleNotFoundError:
        google_auth = None
    if google_auth is not None:
        monkeypatch.setattr(google_auth, "default", forbidden)


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


def test_inventory_framing_is_ordered_and_binds_kind_key_and_value() -> None:
    first = CandidateStoreRecord(kind="document", key="doc-a", value={"b": 2, "a": 1})
    same_different_mapping_order = CandidateStoreRecord(
        kind="document", key="doc-a", value={"a": 1, "b": 2}
    )
    second = CandidateStoreRecord(kind="chunk", key="chunk-b", value={"x": "y"})
    baseline = candidate_inventory_sha256((first, second))
    assert baseline == candidate_inventory_sha256((same_different_mapping_order, second))
    assert baseline != candidate_inventory_sha256((second, first))
    assert baseline != candidate_inventory_sha256(
        (CandidateStoreRecord(kind="candidate", key="doc-a", value=first.value), second)
    )
    assert baseline != candidate_inventory_sha256(
        (CandidateStoreRecord(kind="document", key="doc-b", value=first.value), second)
    )
    assert baseline != candidate_inventory_sha256(
        (CandidateStoreRecord(kind="document", key="doc-a", value={"a": 2}), second)
    )


def test_inventory_and_canonical_json_are_stable_across_hash_seeds() -> None:
    script = """
from app.ingest.candidate_persistence import (
    CandidateStoreRecord,
    candidate_inventory_sha256,
    canonical_json_bytes,
)
records = (
    CandidateStoreRecord(kind="document", key="doc-a", value={"z": 2, "a": "café"}),
    CandidateStoreRecord(kind="chunk", key="chunk-b", value={"embedding": (0.0, -1.0)}),
)
print(candidate_inventory_sha256(records))
print(canonical_json_bytes({"z": 2, "a": "café"}).hex())
"""
    outputs: list[str] = []
    for seed in ("1", "777"):
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONHASHSEED": seed,
            "PYTHONPATH": ".",
        }
        completed = subprocess.run(  # noqa: S603
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        outputs.append(completed.stdout)
    assert outputs[0] == outputs[1]


@pytest.mark.asyncio
async def test_every_durable_record_leaf_kind_and_key_mutation_conflicts() -> None:
    plan = _plan()
    store = MemoryStore()
    service = _service(store)
    await service.persist_and_attest_candidate(
        CandidatePersistenceRequest(contract_version="1.0", plan=plan)
    )
    for expected in tuple(store.records.values()):
        alternatives = {
            "candidate": "document",
            "document": "chunk",
            "chunk": "attestation",
            "attestation": "candidate",
        }
        mutations = [
            CandidateStoreRecord(
                kind=cast(CandidateRecordKind, alternatives[expected.kind]),
                key=expected.key,
                value=expected.value,
            ),
            CandidateStoreRecord(kind=expected.kind, key=expected.key + "x", value=expected.value),
        ]
        mutations.extend(
            CandidateStoreRecord(
                kind=expected.kind,
                key=expected.key,
                value=cast(Any, _mutate_leaf(expected.value, path)),
            )
            for path in _leaf_paths(expected.value)
        )
        for actual in mutations:
            with pytest.raises(CandidatePersistenceError) as caught:
                service._compare_existing(actual, expected)
            assert caught.value.code == "candidate_conflict"


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
            CandidatePersistenceRequest(contract_version="1.0", plan=_plan(dimensions=2049))
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
    ).persist_and_attest_candidate(CandidatePersistenceRequest(contract_version="1.0", plan=plan))
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
    verification = CandidateAttestationVerificationService(
        store=store,
        timeout_seconds=3,
        max_retries=1,
        sleep=_no_sleep,
    )
    verified = await verification.verify_attested_candidate(
        plan.corpus,
        AttestationIdentity(algorithm_id="cairn-test-sha256-v1", key_id="fixture-key-1"),
        verifier,
    )
    assert verified == VerifiedCandidateEvidence(
        corpus=plan.corpus,
        plan_sha256=plan.plan_sha256,
        semantic_manifest_sha256=plan.semantic_manifest_sha256,
        embedding_identity="fixture-embedding-v1",
        embedding_dimensions=2,
        document_count=1,
        chunk_count=1,
        inventory_sha256=resumed.inventory_sha256,
        attestation_payload_sha256=resumed.attestation_payload_sha256,
        signature_algorithm_id="cairn-test-sha256-v1",
        signing_key_id="fixture-key-1",
    )
    assert verifier.verify_calls == 1
    assert not any(call[0] == "create_many" for call in store.calls)

    store.calls.clear()
    with pytest.raises(CandidatePersistenceError) as mismatch:
        await verification.verify_attested_candidate(
            plan.corpus,
            AttestationIdentity(algorithm_id="different-algorithm", key_id="fixture-key-1"),
            verifier,
        )
    assert mismatch.value.code == "attestation_failed"
    assert store.calls == []


@pytest.mark.asyncio
async def test_unknown_header_commit_is_confirmed_not_claimed_created() -> None:
    class UnknownCommittedStore(MemoryStore):
        def __init__(self) -> None:
            super().__init__()
            self.ambiguous = True

        async def create_many_checked(
            self,
            kind: CandidateRecordKind,
            records: tuple[CandidateStoreRecord, ...],
            *,
            expected_encoded_size: int,
            expected_write_sha256s: tuple[str, ...],
            timeout_seconds: int,
        ) -> None:
            await super().create_many_checked(
                kind,
                records,
                expected_encoded_size=expected_encoded_size,
                expected_write_sha256s=expected_write_sha256s,
                timeout_seconds=timeout_seconds,
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


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown_kind", ["document", "chunk", "attestation"])
async def test_unknown_successful_child_commit_is_read_confirmed(
    unknown_kind: CandidateRecordKind,
) -> None:
    class UnknownCommittedStore(MemoryStore):
        def __init__(self) -> None:
            super().__init__()
            self.unknown = True

        async def create_many_checked(
            self,
            kind: CandidateRecordKind,
            records: tuple[CandidateStoreRecord, ...],
            *,
            expected_encoded_size: int,
            expected_write_sha256s: tuple[str, ...],
            timeout_seconds: int,
        ) -> None:
            await super().create_many_checked(
                kind,
                records,
                expected_encoded_size=expected_encoded_size,
                expected_write_sha256s=expected_write_sha256s,
                timeout_seconds=timeout_seconds,
            )
            if kind == unknown_kind and self.unknown:
                self.unknown = False
                from app.ingest.candidate_persistence import CandidateStoreFailure

                raise CandidateStoreFailure("transient")

    store = UnknownCommittedStore()
    receipt = await _service(store).persist_and_attest_candidate(
        CandidatePersistenceRequest(contract_version="1.0", plan=_plan())
    )
    assert receipt.disposition == "created"
    assert sum(call[0] == "create_many" and call[1] == unknown_kind for call in store.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown_kind", ["candidate", "document", "chunk", "attestation"])
async def test_unknown_unsuccessful_commit_retries_only_missing_records_once(
    unknown_kind: CandidateRecordKind,
) -> None:
    class UnknownUncommittedStore(MemoryStore):
        def __init__(self) -> None:
            super().__init__()
            self.unknown = True

        async def create_many_checked(
            self,
            kind: CandidateRecordKind,
            records: tuple[CandidateStoreRecord, ...],
            *,
            expected_encoded_size: int,
            expected_write_sha256s: tuple[str, ...],
            timeout_seconds: int,
        ) -> None:
            if kind == unknown_kind and self.unknown:
                self.calls.append(
                    ("create_many", kind, tuple(r.key for r in records), timeout_seconds)
                )
                self.unknown = False
                from app.ingest.candidate_persistence import CandidateStoreFailure

                raise CandidateStoreFailure("transient")
            await super().create_many_checked(
                kind,
                records,
                expected_encoded_size=expected_encoded_size,
                expected_write_sha256s=expected_write_sha256s,
                timeout_seconds=timeout_seconds,
            )

    store = UnknownUncommittedStore()
    receipt = await _service(store).persist_and_attest_candidate(
        CandidatePersistenceRequest(contract_version="1.0", plan=_plan())
    )
    assert receipt.disposition in {"created", "confirmed"}
    assert sum(call[0] == "create_many" and call[1] == unknown_kind for call in store.calls) == 2


@pytest.mark.asyncio
async def test_concurrent_same_and_conflicting_plans_never_mix_candidates() -> None:
    class HeaderBarrierStore(MemoryStore):
        def __init__(self) -> None:
            super().__init__()
            self.arrivals = 0
            self.release = asyncio.Event()

        async def create_many_checked(
            self,
            kind: CandidateRecordKind,
            records: tuple[CandidateStoreRecord, ...],
            *,
            expected_encoded_size: int,
            expected_write_sha256s: tuple[str, ...],
            timeout_seconds: int,
        ) -> None:
            if kind == "candidate" and self.arrivals < 2:
                self.arrivals += 1
                if self.arrivals == 2:
                    self.release.set()
                await self.release.wait()
            await super().create_many_checked(
                kind,
                records,
                expected_encoded_size=expected_encoded_size,
                expected_write_sha256s=expected_write_sha256s,
                timeout_seconds=timeout_seconds,
            )

    same_store = HeaderBarrierStore()
    same_request = CandidatePersistenceRequest(contract_version="1.0", plan=_plan())
    same_results = await asyncio.gather(
        _service(same_store).persist_and_attest_candidate(same_request),
        _service(same_store).persist_and_attest_candidate(same_request),
    )
    assert sorted(result.disposition for result in same_results) == ["confirmed", "created"]

    conflict_store = HeaderBarrierStore()
    first = CandidatePersistenceRequest(contract_version="1.0", plan=_plan())
    second = CandidatePersistenceRequest(
        contract_version="1.0", plan=_plan(content=b"conflicting reviewed content")
    )
    conflict_results = await asyncio.gather(
        _service(conflict_store).persist_and_attest_candidate(first),
        _service(conflict_store).persist_and_attest_candidate(second),
        return_exceptions=True,
    )
    assert sum(type(result) is CandidatePersistenceError for result in conflict_results) == 1
    assert sum(type(result) is not CandidatePersistenceError for result in conflict_results) == 1
    error = next(result for result in conflict_results if type(result) is CandidatePersistenceError)
    assert error.code == "candidate_conflict"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("corruption", "expected_code"),
    [
        ("missing", "attestation_failed"),
        ("payload_hash", "attestation_failed"),
        ("padded_base64", "attestation_failed"),
        ("signature", "attestation_failed"),
        ("unknown_field", "attestation_failed"),
        ("document_owner", "attestation_failed"),
        ("chunk_text", "attestation_failed"),
        ("chunk_vector", "attestation_failed"),
    ],
)
async def test_read_only_verifier_rejects_durable_record_and_attestation_corruption(
    corruption: str, expected_code: str
) -> None:
    plan = _plan()
    store = MemoryStore()
    await _service(store).persist_and_attest_candidate(
        CandidatePersistenceRequest(contract_version="1.0", plan=plan)
    )
    key = candidate_store_key(plan.corpus)
    if corruption == "missing":
        store.records.pop(("attestation", key))
    elif corruption.startswith("document"):
        identity = next(identity for identity in store.records if identity[0] == "document")
        record = store.records[identity]
        value = cast(dict[str, object], _mutable(record.value))
        provenance = cast(dict[str, object], value["provenance"])
        provenance["owner"] = "Changed owner"
        store.records[identity] = CandidateStoreRecord(
            kind="document", key=record.key, value=cast(Any, value)
        )
    elif corruption.startswith("chunk"):
        identity = next(identity for identity in store.records if identity[0] == "chunk")
        record = store.records[identity]
        value = dict(record.value)
        if corruption == "chunk_text":
            value["text"] = "changed text"
        else:
            value["embedding"] = (9.0, 8.0)
        store.records[identity] = CandidateStoreRecord(
            kind="chunk", key=record.key, value=cast(Any, value)
        )
    else:
        record = store.records[("attestation", key)]
        value = cast(dict[str, object], _mutable(record.value))
        if corruption == "payload_hash":
            value["payload_sha256"] = "0" * 64
        elif corruption == "padded_base64":
            value["signature_b64url"] = cast(str, value["signature_b64url"]) + "="
        elif corruption == "signature":
            value["signature_b64url"] = "A" * 43
        else:
            value["unexpected"] = True
        store.records[("attestation", key)] = CandidateStoreRecord(
            kind="attestation", key=key, value=cast(Any, value)
        )

    store.calls.clear()
    with pytest.raises(CandidatePersistenceError) as caught:
        await _verification(store).verify_attested_candidate(
            plan.corpus,
            AttestationIdentity(algorithm_id="cairn-test-sha256-v1", key_id="fixture-key-1"),
            FixtureSigner(),
        )
    assert caught.value.code == expected_code
    assert not any(call[0] == "create_many" for call in store.calls)


@pytest.mark.asyncio
async def test_read_only_verifier_reconstructs_m7_and_rejects_resigned_path_forgery() -> None:
    plan = _plan()
    store = MemoryStore()
    await _service(store).persist_and_attest_candidate(
        CandidatePersistenceRequest(contract_version="1.0", plan=plan)
    )
    document_identity = next(identity for identity in store.records if identity[0] == "document")
    document = store.records[document_identity]
    document_value = dict(document.value)
    document_value["relative_path"] = "other.md"
    store.records[document_identity] = CandidateStoreRecord(
        kind="document", key=document.key, value=document_value
    )
    chunk_identity = next(identity for identity in store.records if identity[0] == "chunk")
    chunk = store.records[chunk_identity]
    chunk_value = dict(chunk.value)
    chunk_value["source"] = "other.md"
    store.records[chunk_identity] = CandidateStoreRecord(
        kind="chunk", key=chunk.key, value=chunk_value
    )
    _resign_store_attestation(store, plan.corpus)

    with pytest.raises(CandidatePersistenceError) as caught:
        await _verification(store).verify_attested_candidate(
            plan.corpus,
            AttestationIdentity(algorithm_id="cairn-test-sha256-v1", key_id="fixture-key-1"),
            FixtureSigner(),
        )
    assert caught.value.code == "attestation_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reviewed_at",
    [
        "20260910",
        "2026-W37-4",
        "2026W374",
        "2026-02-30",
        "PRIVATE-DATE-CANARY",
    ],
)
async def test_read_only_verifier_rejects_resigned_noncanonical_reviewed_date(
    reviewed_at: str,
) -> None:
    plan = _plan()
    store = MemoryStore()
    await _service(store).persist_and_attest_candidate(
        CandidatePersistenceRequest(contract_version="1.0", plan=plan)
    )
    document_identity = next(identity for identity in store.records if identity[0] == "document")
    document = store.records[document_identity]
    document_value = cast(dict[str, object], _mutable(document.value))
    provenance = cast(dict[str, object], document_value["provenance"])
    provenance["reviewed_at"] = reviewed_at
    store.records[document_identity] = CandidateStoreRecord(
        kind="document", key=document.key, value=cast(Any, document_value)
    )
    _resign_store_attestation(store, plan.corpus)
    verifier = FixtureSigner()
    store.calls.clear()

    with pytest.raises(CandidatePersistenceError) as caught:
        await _verification(store).verify_attested_candidate(
            plan.corpus,
            AttestationIdentity(algorithm_id="cairn-test-sha256-v1", key_id="fixture-key-1"),
            verifier,
        )

    assert caught.value.code == "candidate_conflict"
    _assert_content_free_error(caught.value, (reviewed_at,))
    assert verifier.verify_calls == 0
    assert not any(call[0] == "create_many" for call in store.calls)


@pytest.mark.asyncio
async def test_read_only_verifier_accepts_exact_canonical_reviewed_date_round_trip() -> None:
    plan = _plan()
    store = MemoryStore()
    receipt = await _service(store).persist_and_attest_candidate(
        CandidatePersistenceRequest(contract_version="1.0", plan=plan)
    )
    document = next(record for (kind, _), record in store.records.items() if kind == "document")
    provenance = cast(Mapping[str, object], document.value["provenance"])
    assert provenance["reviewed_at"] == "2026-09-10"
    verifier = FixtureSigner()

    evidence = await _verification(store).verify_attested_candidate(
        plan.corpus,
        AttestationIdentity(algorithm_id="cairn-test-sha256-v1", key_id="fixture-key-1"),
        verifier,
    )

    assert evidence.plan_sha256 == plan.plan_sha256
    assert evidence.inventory_sha256 == receipt.inventory_sha256
    assert verifier.verify_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("surface", "expected_code"),
    [
        ("header", "malformed_store"),
        ("url", "candidate_conflict"),
        ("m7_path", "attestation_failed"),
        ("attestation", "attestation_failed"),
    ],
)
async def test_durable_parse_failures_retain_no_record_or_canary_chain(
    surface: str, expected_code: str
) -> None:
    plan = _plan()
    store = MemoryStore()
    await _service(store).persist_and_attest_candidate(
        CandidatePersistenceRequest(contract_version="1.0", plan=plan)
    )
    key = candidate_store_key(plan.corpus)
    document_identity = next(identity for identity in store.records if identity[0] == "document")
    chunk_identity = next(identity for identity in store.records if identity[0] == "chunk")
    document = store.records[document_identity]
    chunk = store.records[chunk_identity]
    canary = f"PRIVATE-{surface.upper()}-PARSE-CANARY"
    if surface == "header":
        header = store.records[("candidate", key)]
        store.records[("candidate", key)] = CandidateStoreRecord(
            kind="candidate",
            key=key,
            value={**dict(header.value), canary: canary},
        )
    elif surface == "url":
        document_value = cast(dict[str, object], _mutable(document.value))
        provenance = cast(dict[str, object], document_value["provenance"])
        invalid_url = f"https://docs.example.test:{canary}/guide"
        provenance["url"] = invalid_url
        store.records[document_identity] = CandidateStoreRecord(
            kind="document", key=document.key, value=cast(Any, document_value)
        )
        chunk_value = dict(chunk.value)
        chunk_value["citation_url"] = invalid_url
        store.records[chunk_identity] = CandidateStoreRecord(
            kind="chunk", key=chunk.key, value=chunk_value
        )
    elif surface == "m7_path":
        forged_path = f"{canary}.md"
        store.records[document_identity] = CandidateStoreRecord(
            kind="document",
            key=document.key,
            value={**dict(document.value), "relative_path": forged_path},
        )
        store.records[chunk_identity] = CandidateStoreRecord(
            kind="chunk",
            key=chunk.key,
            value={**dict(chunk.value), "source": forged_path},
        )
    else:
        attestation = store.records[("attestation", key)]
        store.records[("attestation", key)] = CandidateStoreRecord(
            kind="attestation",
            key=key,
            value={**dict(attestation.value), "signature_b64url": canary + "!"},
        )
    if surface != "attestation":
        _resign_store_attestation(store, plan.corpus)
    verifier = FixtureSigner()
    store.calls.clear()

    with pytest.raises(CandidatePersistenceError) as caught:
        await _verification(store).verify_attested_candidate(
            plan.corpus,
            AttestationIdentity(algorithm_id="cairn-test-sha256-v1", key_id="fixture-key-1"),
            verifier,
        )

    assert caught.value.code == expected_code
    _assert_content_free_error(
        caught.value,
        (
            canary,
            plan.plan_sha256,
            plan.semantic_manifest_sha256,
            document.key,
            chunk.key,
            plan.documents[0].provenance.source_sha256,
            "Public guide",
        ),
    )
    assert verifier.verify_calls == 0
    assert not any(call[0] == "create_many" for call in store.calls)


def test_public_corpus_and_canonical_json_failures_have_no_sensitive_chain() -> None:
    corpus_canary = "PRIVATE-CORPUS-CANARY bad"
    bypass_corpus = ExactCorpusReference.model_construct(
        kind="exact",
        corpus_id="public-docs",
        corpus_version=corpus_canary,
    )
    attestation_canary = "PRIVATE-ATTESTATION-CORPUS-CANARY bad"
    unicode_canary = "PRIVATE-UNICODE-CANARY"
    surfaces: tuple[tuple[Callable[[], object], str], ...] = (
        (lambda: candidate_store_key(bypass_corpus), corpus_canary),
        (
            lambda: CandidatePersistenceReceipt(
                contract_version="1.0",
                corpus=bypass_corpus,
                plan_sha256="0" * 64,
                disposition="created",
                document_count=1,
                chunk_count=1,
                inventory_sha256="1" * 64,
                attestation_payload_sha256="2" * 64,
            ),
            corpus_canary,
        ),
        (
            lambda: AttestationCorpus(
                kind="exact",
                corpus_id="public-docs",
                corpus_version=attestation_canary,
            ),
            attestation_canary,
        ),
        (lambda: canonical_json_bytes({"x": unicode_canary + "\ud800"}), unicode_canary),
    )

    for surface, canary in surfaces:
        with pytest.raises(CandidatePersistenceError) as caught:
            surface()
        assert caught.value.code == "invalid_plan"
        _assert_content_free_error(caught.value, (canary,))


@pytest.mark.asyncio
async def test_invalid_corpus_verifier_failure_has_no_chain_or_store_work() -> None:
    canary = "PRIVATE-VERIFIER-CORPUS-CANARY bad"
    corpus = ExactCorpusReference.model_construct(
        kind="exact",
        corpus_id="public-docs",
        corpus_version=canary,
    )
    store = MemoryStore()

    with pytest.raises(CandidatePersistenceError) as caught:
        await _verification(store).verify_attested_candidate(
            corpus,
            AttestationIdentity(algorithm_id="cairn-test-sha256-v1", key_id="fixture-key-1"),
            FixtureSigner(),
        )

    assert caught.value.code == "invalid_plan"
    _assert_content_free_error(caught.value, (canary,))
    assert store.calls == []


@pytest.mark.asyncio
async def test_malformed_durable_header_is_classified_as_malformed_store() -> None:
    plan = _plan()
    store = MemoryStore()
    await _service(store).persist_and_attest_candidate(
        CandidatePersistenceRequest(contract_version="1.0", plan=plan)
    )
    key = candidate_store_key(plan.corpus)
    header = store.records[("candidate", key)]
    store.records[("candidate", key)] = CandidateStoreRecord(
        kind="candidate", key=key, value={**dict(header.value), "unknown": True}
    )
    with pytest.raises(CandidatePersistenceError) as caught:
        await _verification(store).verify_attested_candidate(
            plan.corpus,
            AttestationIdentity(algorithm_id="cairn-test-sha256-v1", key_id="fixture-key-1"),
            FixtureSigner(),
        )
    assert caught.value.code == "malformed_store"


@pytest.mark.asyncio
async def test_malformed_store_failure_is_fixed_and_not_retried() -> None:
    class MalformedStore(MemoryStore):
        async def get(
            self, kind: CandidateRecordKind, key: str, *, timeout_seconds: int
        ) -> CandidateStoreRecord | None:
            self.calls.append(("get", kind, key, timeout_seconds))
            raise _CandidateStoreMalformed()

    store = MalformedStore()
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    with pytest.raises(CandidatePersistenceError) as caught:
        await _verification(store, sleep=sleep).verify_attested_candidate(
            _plan().corpus,
            AttestationIdentity(algorithm_id="cairn-test-sha256-v1", key_id="fixture-key-1"),
            FixtureSigner(),
        )
    assert caught.value.code == "malformed_store"
    assert len(store.calls) == 1
    assert sleeps == []


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["exception", "false", "nonbool"])
async def test_verify_only_failures_are_fixed_and_never_write(behavior: str) -> None:
    plan = _plan()
    store = MemoryStore()
    await _service(store).persist_and_attest_candidate(
        CandidatePersistenceRequest(contract_version="1.0", plan=plan)
    )

    class FailingVerifier:
        algorithm_id = "cairn-test-sha256-v1"
        key_id = "fixture-key-1"

        async def verify(self, payload: bytes, signature: bytes) -> bool:
            if behavior == "exception":
                raise RuntimeError("PRIVATE-VERIFY-EXCEPTION")
            if behavior == "nonbool":
                return cast(bool, 1)
            return False

    store.calls.clear()
    with pytest.raises(CandidatePersistenceError) as caught:
        await _verification(store).verify_attested_candidate(
            plan.corpus,
            AttestationIdentity(algorithm_id="cairn-test-sha256-v1", key_id="fixture-key-1"),
            FailingVerifier(),
        )
    assert caught.value.code == "attestation_failed"
    assert "PRIVATE-VERIFY-EXCEPTION" not in repr(caught.value.__context__)
    assert not any(call[0] == "create_many" for call in store.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "behavior", ["exception", "wrong_type", "short", "long", "false", "nonbool", "bad_id"]
)
async def test_signing_failures_never_create_or_replace_attestation(behavior: str) -> None:
    class FailingSigner(FixtureSigner):
        def __init__(self) -> None:
            super().__init__()
            self.algorithm_id = " bad-id" if behavior == "bad_id" else "cairn-test-sha256-v1"

        async def sign(self, payload: bytes) -> bytes:
            self.sign_calls += 1
            if behavior == "exception":
                raise RuntimeError("PRIVATE-SIGN-EXCEPTION")
            if behavior == "wrong_type":
                return cast(bytes, "not-bytes")
            if behavior == "short":
                return b"short"
            if behavior == "long":
                return b"x" * 4097
            return await super().sign(payload)

        async def verify(self, payload: bytes, signature: bytes) -> bool:
            self.verify_calls += 1
            if behavior == "nonbool":
                return cast(bool, 1)
            if behavior == "false":
                return False
            return signature == hashlib.sha256(b"fixture-only\0" + payload).digest()

    store = MemoryStore()
    signer = FailingSigner()
    with pytest.raises(CandidatePersistenceError) as caught:
        await _service(store, signer).persist_and_attest_candidate(
            CandidatePersistenceRequest(contract_version="1.0", plan=_plan())
        )
    assert caught.value.code == "attestation_failed"
    assert "PRIVATE-SIGN-EXCEPTION" not in repr(caught.value.__context__)
    assert not any(kind == "attestation" for kind, _ in store.records)
    if behavior == "bad_id":
        assert store.calls == []


@pytest.mark.asyncio
async def test_verifier_identity_change_fails_after_readback_without_verification() -> None:
    plan = _plan()
    store = MemoryStore()
    await _service(store).persist_and_attest_candidate(
        CandidatePersistenceRequest(contract_version="1.0", plan=plan)
    )

    class ChangingVerifier:
        key_id = "fixture-key-1"

        def __init__(self) -> None:
            self.identity_reads = 0
            self.verify_calls = 0

        @property
        def algorithm_id(self) -> str:
            self.identity_reads += 1
            return "cairn-test-sha256-v1" if self.identity_reads == 1 else "changed-algorithm"

        async def verify(self, payload: bytes, signature: bytes) -> bool:
            self.verify_calls += 1
            return True

    verifier = ChangingVerifier()
    with pytest.raises(CandidatePersistenceError) as caught:
        await _verification(store).verify_attested_candidate(
            plan.corpus,
            AttestationIdentity(algorithm_id="cairn-test-sha256-v1", key_id="fixture-key-1"),
            verifier,
        )
    assert caught.value.code == "attestation_failed"
    assert verifier.verify_calls == 0


@pytest.mark.asyncio
async def test_cancellation_stops_store_sign_verify_sleep_and_cleanup_phases() -> None:
    class CancelGetStore(MemoryStore):
        async def get(
            self, kind: CandidateRecordKind, key: str, *, timeout_seconds: int
        ) -> CandidateStoreRecord | None:
            self.calls.append(("get", kind, key, timeout_seconds))
            raise asyncio.CancelledError

    cancelled_store = CancelGetStore()
    with pytest.raises(asyncio.CancelledError):
        await _service(cancelled_store).persist_and_attest_candidate(
            CandidatePersistenceRequest(contract_version="1.0", plan=_plan())
        )
    assert sum(call[0] == "get" for call in cancelled_store.calls) == 1
    assert not any(call[0] == "create_many" for call in cancelled_store.calls)

    class CancelSign(FixtureSigner):
        async def sign(self, payload: bytes) -> bytes:
            raise asyncio.CancelledError

    sign_store = MemoryStore()
    with pytest.raises(asyncio.CancelledError):
        await _service(sign_store, CancelSign()).persist_and_attest_candidate(
            CandidatePersistenceRequest(contract_version="1.0", plan=_plan())
        )
    assert not any(kind == "attestation" for kind, _ in sign_store.records)

    plan = _plan()
    verify_store = MemoryStore()
    await _service(verify_store).persist_and_attest_candidate(
        CandidatePersistenceRequest(contract_version="1.0", plan=plan)
    )

    class CancelVerify(FixtureSigner):
        async def verify(self, payload: bytes, signature: bytes) -> bool:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _verification(verify_store).verify_attested_candidate(
            plan.corpus,
            AttestationIdentity(algorithm_id="cairn-test-sha256-v1", key_id="fixture-key-1"),
            CancelVerify(),
        )

    from app.ingest.candidate_persistence import CandidateStoreFailure

    class TransientStore(MemoryStore):
        async def get(
            self, kind: CandidateRecordKind, key: str, *, timeout_seconds: int
        ) -> CandidateStoreRecord | None:
            raise CandidateStoreFailure("transient")

    async def cancel_sleep(_: float) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _service(TransientStore(), sleep=cancel_sleep).persist_and_attest_candidate(
            CandidatePersistenceRequest(contract_version="1.0", plan=_plan())
        )

    class CancelCloseStore(MemoryStore):
        async def aclose(self) -> None:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _service(CancelCloseStore(), owns_store=True).aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "expected_code"),
    [
        ("read", "store_unavailable"),
        ("create", "store_unavailable"),
        ("sign", "attestation_failed"),
        ("verify", "attestation_failed"),
    ],
)
async def test_store_signer_and_verifier_timeouts_are_bounded_and_content_free(
    phase: str, expected_code: str
) -> None:
    canary = f"PRIVATE-{phase.upper()}-TIMEOUT-CANARY"

    async def hang() -> None:
        await asyncio.Event().wait()

    class HangingReadStore(MemoryStore):
        async def get(
            self, kind: CandidateRecordKind, key: str, *, timeout_seconds: int
        ) -> CandidateStoreRecord | None:
            await hang()
            raise AssertionError("unreachable")

    class HangingCreateStore(MemoryStore):
        async def create_many_checked(
            self,
            kind: CandidateRecordKind,
            records: tuple[CandidateStoreRecord, ...],
            *,
            expected_encoded_size: int,
            expected_write_sha256s: tuple[str, ...],
            timeout_seconds: int,
        ) -> None:
            await hang()

    class HangingSigner(FixtureSigner):
        async def sign(self, payload: bytes) -> bytes:
            await hang()
            raise AssertionError("unreachable")

    class HangingVerifier:
        algorithm_id = "cairn-test-sha256-v1"
        key_id = "fixture-key-1"

        async def verify(self, payload: bytes, signature: bytes) -> bool:
            await hang()
            raise AssertionError("unreachable")

    plan = _plan(content=canary.encode())
    request = CandidatePersistenceRequest(contract_version="1.0", plan=plan)
    if phase == "read":
        service: Any = _service(HangingReadStore(), timeout_seconds=1, max_retries=0)
        operation = service.persist_and_attest_candidate(request)
    elif phase == "create":
        service = _service(HangingCreateStore(), timeout_seconds=1, max_retries=0)
        operation = service.persist_and_attest_candidate(request)
    elif phase == "sign":
        service = _service(MemoryStore(), HangingSigner(), timeout_seconds=1, max_retries=0)
        operation = service.persist_and_attest_candidate(request)
    else:
        store = MemoryStore()
        await _service(store).persist_and_attest_candidate(request)
        service = _verification(store, timeout_seconds=1, max_retries=0)
        operation = service.verify_attested_candidate(
            plan.corpus,
            AttestationIdentity(algorithm_id="cairn-test-sha256-v1", key_id="fixture-key-1"),
            HangingVerifier(),
        )
    with pytest.raises(CandidatePersistenceError) as caught:
        await operation
    assert caught.value.code == expected_code
    assert canary not in (
        str(caught.value)
        + repr(caught.value)
        + repr(caught.value.__cause__)
        + repr(caught.value.__context__)
        + repr(service.__dict__)
    )


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


def test_outer_attestation_models_reject_bypass_mutated_nested_models() -> None:
    corpus = AttestationCorpus(kind="exact", corpus_id="public-docs", corpus_version="2026.09.10")
    embedding = AttestationEmbedding(identity="fixture-embedding-v1", dimensions=2)
    schemas = AttestationRecordSchemas(candidate="1.0", document="1.0", chunk="1.0")
    valid = {
        "attestation_version": "1.0",
        "corpus": corpus,
        "plan_contract_version": "1.0",
        "plan_sha256": "0" * 64,
        "semantic_manifest_sha256": "1" * 64,
        "embedding": embedding,
        "document_count": 1,
        "chunk_count": 1,
        "record_schemas": schemas,
        "inventory_algorithm": "cairn-candidate-inventory-v1",
        "inventory_sha256": "2" * 64,
        "signature_algorithm_id": "algorithm-1",
        "signing_key_id": "key-1",
    }
    payload = cast(Any, AttestationPayload)(**valid)
    assert payload.corpus is not corpus
    assert payload.embedding is not embedding
    assert payload.record_schemas is not schemas
    object.__setattr__(corpus, "kind", "PRIVATE-NESTED-CANARY")
    assert payload.corpus.kind == "exact"

    surfaces: tuple[Callable[[], object], ...] = (
        lambda: cast(Any, AttestationPayload)(**valid),
        lambda: AttestationPayload.model_validate(valid),
        lambda: TypeAdapter(AttestationPayload).validate_python(valid),
        lambda: cast(Any, AttestationPayload).model_construct(**valid),
        lambda: payload.model_copy(update={"corpus": corpus}),
        lambda: payload.copy(update={"corpus": corpus}),
    )
    for surface in surfaces:
        with pytest.raises(CandidatePersistenceError) as caught:
            surface()
        assert "PRIVATE-NESTED-CANARY" not in (
            str(caught.value)
            + repr(caught.value)
            + repr(caught.value.errors(include_input=True))
            + caught.value.json(include_input=True)
        )

    mutated_embedding = AttestationEmbedding(identity="fixture-embedding-v1", dimensions=2)
    object.__setattr__(mutated_embedding, "dimensions", 0)
    mutated_schemas = AttestationRecordSchemas(candidate="1.0", document="1.0", chunk="1.0")
    object.__setattr__(mutated_schemas, "candidate", "2.0")
    mutated_payload = cast(Any, AttestationPayload)(
        **{
            **valid,
            "corpus": AttestationCorpus(
                kind="exact", corpus_id="public-docs", corpus_version="2026.09.10"
            ),
        }
    )
    object.__setattr__(mutated_payload, "attestation_version", "2.0")
    mutated_record = CandidateStoreRecord(kind="candidate", key="safe", value={})
    object.__setattr__(mutated_record, "kind", "unknown")
    for surface in (
        lambda: cast(Any, AttestationPayload)(**{**valid, "embedding": mutated_embedding}),
        lambda: cast(Any, AttestationPayload)(**{**valid, "record_schemas": mutated_schemas}),
        lambda: CandidateAttestation(
            schema_version="1.0",
            payload=mutated_payload,
            payload_sha256="0" * 64,
            signature_b64url="A" * 22,
        ),
        lambda: CandidateStorePage(records=(mutated_record,), next_after_key=None),
    ):
        with pytest.raises(CandidatePersistenceError):
            surface()


def test_all_public_model_surfaces_reject_unknown_and_missing_fields_content_free() -> None:
    plan = _plan()
    corpus = AttestationCorpus(kind="exact", corpus_id="public-docs", corpus_version="2026.09.10")
    payload = AttestationPayload(
        attestation_version="1.0",
        corpus=corpus,
        plan_contract_version="1.0",
        plan_sha256="0" * 64,
        semantic_manifest_sha256="1" * 64,
        embedding=AttestationEmbedding(identity="fixture-embedding-v1", dimensions=2),
        document_count=1,
        chunk_count=1,
        record_schemas=AttestationRecordSchemas(candidate="1.0", document="1.0", chunk="1.0"),
        inventory_algorithm="cairn-candidate-inventory-v1",
        inventory_sha256="2" * 64,
        signature_algorithm_id="algorithm-1",
        signing_key_id="key-1",
    )
    models = (
        CandidatePersistenceRequest(contract_version="1.0", plan=plan),
        CandidatePersistenceReceipt(
            contract_version="1.0",
            corpus=plan.corpus,
            plan_sha256=plan.plan_sha256,
            disposition="confirmed",
            document_count=1,
            chunk_count=1,
            inventory_sha256="2" * 64,
            attestation_payload_sha256="3" * 64,
        ),
        CandidateStoreRecord(kind="candidate", key="safe", value={}),
        CandidateStorePage(records=(), next_after_key=None),
        corpus,
        payload.embedding,
        payload.record_schemas,
        payload,
        CandidateAttestation(
            schema_version="1.0",
            payload=payload,
            payload_sha256="3" * 64,
            signature_b64url="A" * 22,
        ),
        AttestationIdentity(algorithm_id="algorithm-1", key_id="key-1"),
        VerifiedCandidateEvidence(
            corpus=plan.corpus,
            plan_sha256=plan.plan_sha256,
            semantic_manifest_sha256=plan.semantic_manifest_sha256,
            embedding_identity="fixture-embedding-v1",
            embedding_dimensions=2,
            document_count=1,
            chunk_count=1,
            inventory_sha256="2" * 64,
            attestation_payload_sha256="3" * 64,
            signature_algorithm_id="algorithm-1",
            signing_key_id="key-1",
        ),
    )
    invalid_known_fields: dict[type[Any], tuple[str, object]] = {
        CandidatePersistenceRequest: ("contract_version", "2.0"),
        CandidatePersistenceReceipt: ("disposition", "unknown"),
        CandidateStoreRecord: ("kind", "unknown"),
        CandidateStorePage: ("next_after_key", " "),
        AttestationCorpus: ("corpus_id", "active"),
        AttestationEmbedding: ("dimensions", 0),
        AttestationRecordSchemas: ("candidate", "2.0"),
        AttestationPayload: ("attestation_version", "2.0"),
        CandidateAttestation: ("schema_version", "2.0"),
        AttestationIdentity: ("algorithm_id", " "),
        VerifiedCandidateEvidence: ("chunk_count", 0),
    }
    canary = "PRIVATE-MODEL-SURFACE-CANARY"
    for instance in models:
        model = type(instance)
        values = instance.model_dump(mode="python", round_trip=True)
        json_values = instance.model_dump(mode="json", round_trip=True)
        assert model.model_validate_json(instance.model_dump_json()) == instance
        missing = dict(values)
        missing.pop(next(iter(model.model_fields)))
        invalid_field, invalid_value = invalid_known_fields[model]
        invalid = {**values, invalid_field: invalid_value}
        surfaces: tuple[Callable[[], object], ...] = (
            partial(_invoke, model, **{**values, canary: canary}),
            partial(
                _invoke,
                model.model_validate,
                {**values, canary: canary},
                extra="allow",
            ),
            partial(
                _invoke,
                TypeAdapter(model).validate_python,
                {**values, canary: canary},
                extra="ignore",
            ),
            partial(
                _invoke,
                model.model_validate_json,
                json.dumps({**json_values, canary: canary}),
            ),
            partial(
                _invoke,
                model.model_construct,
                **{**values, canary: canary},
            ),
            partial(
                _invoke,
                model.construct,
                **{**values, canary: canary},
            ),
            partial(_invoke, instance.model_copy, update={canary: canary}),
            partial(_invoke, instance.copy, update={canary: canary}),
            partial(_invoke, instance.__replace__, **{canary: canary}),
            partial(_invoke, model.model_validate, missing),
            partial(_invoke, model.model_validate, invalid),
            partial(
                _invoke,
                instance.model_copy,
                update={invalid_field: invalid_value},
            ),
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


@pytest.mark.asyncio
async def test_false_full_page_cursor_cannot_be_confirmed_by_empty_terminal_page() -> None:
    expected = tuple(
        CandidateStoreRecord(kind="document", key=f"doc-{index:03d}", value={"index": index})
        for index in range(200)
    )

    class FalseCursorStore(MemoryStore):
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
            if after_key is None:
                return CandidateStorePage(records=expected, next_after_key=expected[-1].key)
            return CandidateStorePage(records=(), next_after_key=None)

    store = FalseCursorStore()
    with pytest.raises(CandidatePersistenceError) as caught:
        await _service(store)._list_exact(
            "document",
            ExactCorpusReference(corpus_id="public-docs", corpus_version="2026.09.10"),
            expected,
        )
    assert caught.value.code == "malformed_store"
    assert sum(call[0] == "list_page" for call in store.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 1, 199, 200, 201, 65_536])
async def test_exact_pagination_matrix_has_bounded_calls_and_no_terminal_empty_query(
    count: int,
) -> None:
    expected = tuple(
        CandidateStoreRecord(
            kind="document",
            key=f"doc-{index:05d}",
            value={"index": index},
        )
        for index in range(count)
    )

    class PagingStore(MemoryStore):
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
            start = 0 if after_key is None else int(after_key.removeprefix("doc-")) + 1
            lookahead = expected[start : start + limit + 1]
            records = lookahead[:limit]
            return CandidateStorePage(
                records=records,
                next_after_key=records[-1].key if len(lookahead) == limit + 1 else None,
            )

    store = PagingStore()
    actual = await _service(store)._list_exact(
        "document",
        ExactCorpusReference(corpus_id="public-docs", corpus_version="2026.09.10"),
        expected,
    )
    assert actual == expected
    expected_calls = max(1, math.ceil(count / 200))
    assert sum(call[0] == "list_page" for call in store.calls) == expected_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["missing", "extra", "repeated", "foreign"])
async def test_malformed_or_inexact_pages_fail_closed(mode: str) -> None:
    expected = tuple(
        CandidateStoreRecord(
            kind="document",
            key=f"doc-{index:03d}",
            value={"corpus_id": "public-docs", "index": index},
        )
        for index in range(201)
    )

    class AdversarialPagingStore(MemoryStore):
        async def list_page(
            self,
            kind: CandidateRecordKind,
            corpus: ExactCorpusReference,
            after_key: str | None,
            limit: int,
            *,
            timeout_seconds: int,
        ) -> CandidateStorePage:
            if mode == "missing":
                return CandidateStorePage(records=expected[:199], next_after_key=None)
            if mode == "extra":
                return CandidateStorePage(records=expected[:200], next_after_key=None)
            if after_key is None:
                return CandidateStorePage(records=expected[:200], next_after_key=expected[199].key)
            if mode == "repeated":
                return CandidateStorePage(records=(expected[199],), next_after_key=None)
            foreign = CandidateStoreRecord(
                kind="document",
                key=expected[200].key,
                value={"corpus_id": "foreign", "index": 200},
            )
            return CandidateStorePage(records=(foreign,), next_after_key=None)

    with pytest.raises(CandidatePersistenceError):
        await _service(AdversarialPagingStore())._list_exact(
            "document",
            ExactCorpusReference(corpus_id="public-docs", corpus_version="2026.09.10"),
            expected,
        )


@pytest.mark.asyncio
async def test_service_retains_no_candidate_content_or_provenance(
    caplog: pytest.LogCaptureFixture,
) -> None:
    canary = "PRIVATE-SERVICE-RETENTION-CANARY"
    store = MemoryStore()
    service = _service(store)
    await service.persist_and_attest_candidate(
        CandidatePersistenceRequest(contract_version="1.0", plan=_plan(content=canary.encode()))
    )

    retained = repr(service.__dict__)
    rendered = retained + repr(service) + caplog.text
    assert canary not in rendered
    assert "https://docs.example.test/guide" not in rendered
    assert "Public guide" not in rendered


def test_batch_planning_splits_at_400_and_detects_size_drift() -> None:
    store = MemoryStore()
    service = _service(store)
    records = tuple(
        CandidateStoreRecord(kind="document", key=f"doc_{index:064x}", value={"x": index})
        for index in range(401)
    )
    assert [len(batch) for batch in service._plan_batches("document", records)] == [400, 1]
    assert sum(call[0] == "encoded_record_sizes" for call in store.calls) == 401
    assert sum(call[0] == "encoded_create_base_size" for call in store.calls) == 1

    class MaximumCountingStore(MemoryStore):
        def __init__(self) -> None:
            super().__init__()
            self.record_size_calls = 0

        def encoded_create_base_size(self, kind: CandidateRecordKind) -> int:
            return 1

        def encoded_record_sizes(self, record: CandidateStoreRecord) -> tuple[int, int, str]:
            self.record_size_calls += 1
            return 1, 1, "0" * 64

    maximum_store = MaximumCountingStore()
    compact_record = CandidateStoreRecord(
        kind="chunk", key="c1-" + "a" * 64, value={"bounded": True}
    )
    maximum_batches = _service(maximum_store)._plan_batches("chunk", (compact_record,) * 65_536)
    assert len(maximum_batches) == 164
    assert len(maximum_batches[-1]) == 336
    assert maximum_store.record_size_calls == 65_536

    class DriftingStore(MemoryStore):
        def __init__(self) -> None:
            super().__init__()
            self.offset = 0

        def encoded_record_sizes(self, record: CandidateStoreRecord) -> tuple[int, int, str]:
            self.offset += 1
            document_size, contribution, write_sha256 = super().encoded_record_sizes(record)
            return (
                document_size,
                contribution,
                write_sha256 if self.offset % 2 else "f" * 64,
            )

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
        def encoded_record_sizes(self, record: CandidateStoreRecord) -> tuple[int, int, str]:
            _, contribution, write_sha256 = super().encoded_record_sizes(record)
            return 1_048_577, contribution, write_sha256

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
    from app.ingest.candidate_firestore import create_candidate_store
    from app.providers.gemini import GeminiProvider
    from app.providers.ollama import OllamaProvider
    from app.retrieval_firestore import create_firestore_vector_client

    for operation in (
        lambda: socket.getaddrinfo("localhost", 80),
        lambda: socket.create_connection(("127.0.0.1", 9)),
        lambda: socket.socket().connect(("127.0.0.1", 9)),
        lambda: GeminiProvider.stream(None, None),  # type: ignore[arg-type]
        lambda: OllamaProvider.stream(None, None),  # type: ignore[arg-type]
        lambda: create_firestore_vector_client("denied-project"),
        lambda: create_candidate_store("denied-project"),
    ):
        with pytest.raises(AssertionError, match="external I/O forbidden"):
            operation()

    try:
        google_auth = importlib.import_module("google.auth")
    except ModuleNotFoundError:
        with pytest.raises(AssertionError, match="external I/O forbidden"):
            create_candidate_store("denied-project")
    else:
        with pytest.raises(AssertionError, match="external I/O forbidden"):
            google_auth.default()
