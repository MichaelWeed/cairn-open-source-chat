import asyncio
import hashlib
import importlib
import json
import os
import socket
import subprocess
import sys
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from threading import Barrier
from typing import Any, cast

import pytest
from pydantic import TypeAdapter

from app.corpus_lifecycle import (
    ACTIVE_CORPUS_POINTER_SCHEMA_VERSION,
    CORPUS_LIFECYCLE_AUDIT_SCHEMA_VERSION,
    CORPUS_LIFECYCLE_CONTRACT_VERSION,
    CORPUS_LIFECYCLE_RECORD_SCHEMA_VERSION,
    LIFECYCLE_READY_REVISION,
    LIFECYCLE_REMOVED_REVISION,
    ActiveCorpusPointer,
    ActivePointerSnapshot,
    AttestationTrustPolicy,
    CorpusLifecycleError,
    CorpusLifecycleRecord,
    CorpusLifecycleService,
    CorpusLifecycleStoreFailure,
    ExpectedActivePointer,
    LifecycleAuditRecord,
    LifecycleMutationReceipt,
    LifecycleSnapshot,
    MarkReadyRequest,
    RemoveCorpusVersionRequest,
    ResolvedActiveState,
    StoreMutationResult,
    SwitchActiveRequest,
    VerifiedLifecycleEvidence,
    active_audit_key,
    active_pointer_key,
    lifecycle_audit_key,
    lifecycle_record_key,
)
from app.ingest.candidate_persistence import (
    AttestationIdentity,
    AttestationVerifier,
    CandidateAttestationVerificationService,
    CandidatePersistenceError,
    CandidatePersistenceRequest,
    CandidatePersistenceService,
    CandidateStorePage,
    CandidateStoreRecord,
    VerifiedCandidateEvidence,
    candidate_inventory_sha256,
)
from app.ingest.planner import (
    CandidateDocumentSnapshot,
    CandidateSourceSnapshot,
    EmbeddingSpecification,
    plan_candidate,
)
from app.readiness import ReadinessEvaluator
from app.retrieval_contracts import (
    MAX_SAFE_INTEGER,
    ExactCorpusReference,
)
from app.retrieval_firestore import (
    FirestoreReadinessQuery,
    FirestoreRetrievalAdapter,
    FirestoreVectorQuery,
    FirestoreVectorRow,
)
from app.retrieval_route import (
    ExactRetrievalAdapterBinding,
    LifecycleRetrievalRouteResolver,
)


def _forbidden(*_: object, **__: object) -> Any:
    raise AssertionError("external call forbidden in lifecycle tests")


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr("app.providers.gemini.GeminiProvider.stream", _forbidden)
    monkeypatch.setattr("app.providers.ollama.OllamaProvider.stream", _forbidden)
    monkeypatch.setattr("app.retrieval_firestore.create_firestore_vector_client", _forbidden)
    monkeypatch.setattr("app.ingest.candidate_firestore.create_candidate_store", _forbidden)
    monkeypatch.setattr(
        "app.corpus_lifecycle_firestore.create_corpus_lifecycle_store", _forbidden
    )
    try:
        google_auth = importlib.import_module("google.auth")
    except ModuleNotFoundError:
        google_auth = None
    if google_auth is not None:
        monkeypatch.setattr(google_auth, "default", _forbidden)


def _direct_socket_connect() -> None:
    with socket.socket() as candidate:
        candidate.connect(("127.0.0.1", 9))


def test_no_external_guard_negative_controls() -> None:
    from app.corpus_lifecycle_firestore import create_corpus_lifecycle_store
    from app.ingest.candidate_firestore import create_candidate_store
    from app.providers.gemini import GeminiProvider
    from app.providers.ollama import OllamaProvider
    from app.retrieval_firestore import create_firestore_vector_client

    for operation in (
        lambda: socket.getaddrinfo("localhost", 0),
        lambda: socket.create_connection(("localhost", 9)),
        _direct_socket_connect,
        lambda: GeminiProvider.stream(None, None),  # type: ignore[arg-type]
        lambda: OllamaProvider.stream(None, None),  # type: ignore[arg-type]
        lambda: create_firestore_vector_client("denied-project"),
        lambda: create_candidate_store("denied-project"),
        lambda: create_corpus_lifecycle_store("denied-project"),
    ):
        with pytest.raises(AssertionError, match="external call forbidden"):
            operation()
    try:
        google_auth = importlib.import_module("google.auth")
    except ModuleNotFoundError:
        pass
    else:
        with pytest.raises(AssertionError, match="external call forbidden"):
            google_auth.default()


def _corpus(version: str = "v1") -> ExactCorpusReference:
    return ExactCorpusReference(
        kind="exact", corpus_id="public-docs", corpus_version=version
    )


def _identity() -> AttestationIdentity:
    return AttestationIdentity(
        algorithm_id="fixture-ed25519-v1", key_id="fixture-key-1"
    )


def _candidate_evidence(
    version: str = "v1", marker: str = "a"
) -> VerifiedCandidateEvidence:
    return VerifiedCandidateEvidence(
        corpus=_corpus(version),
        plan_sha256=marker * 64,
        semantic_manifest_sha256="b" * 64,
        embedding_identity="fixture-embedding-v1",
        embedding_dimensions=3,
        document_count=2,
        chunk_count=3,
        inventory_sha256="c" * 64,
        attestation_payload_sha256="d" * 64,
        signature_algorithm_id=_identity().algorithm_id,
        signing_key_id=_identity().key_id,
    )


def _lifecycle_evidence(
    version: str = "v1", marker: str = "a"
) -> VerifiedLifecycleEvidence:
    return VerifiedLifecycleEvidence.model_validate(
        _candidate_evidence(version, marker).model_dump(round_trip=True)
    )


def _record(version: str = "v1", marker: str = "a") -> CorpusLifecycleRecord:
    return CorpusLifecycleRecord(
        **_lifecycle_evidence(version, marker).model_dump(round_trip=True),
        schema_version="1.0",
        contract_version="1.0",
        state="ready",
        revision=0,
    )


def _ready_audit(record: CorpusLifecycleRecord) -> LifecycleAuditRecord:
    return LifecycleAuditRecord(
        schema_version="1.0",
        contract_version="1.0",
        action="ready",
        subject=record.corpus,
        before=None,
        after=None,
        resulting_revision=0,
        authorizing_attestation_payload_sha256=record.attestation_payload_sha256,
    )


class Verifier:
    algorithm_id = "fixture-ed25519-v1"
    key_id = "fixture-key-1"

    def __init__(self, result: object = True) -> None:
        self.verify_calls = 0
        self.result = result

    async def verify(self, payload: bytes, signature: bytes) -> Any:
        del payload, signature
        self.verify_calls += 1
        return self.result


class Policy:
    def __init__(self, *, trusted: bool = True, verify_result: object = True) -> None:
        self._trusted = trusted
        self.version_reads = 0
        self.generation_reads = 0
        self.lookups: list[AttestationIdentity] = []
        self.verifier = Verifier(verify_result)

    @property
    def policy_version(self) -> str:
        self.version_reads += 1
        return "fixture-policy-v1"

    @property
    def policy_generation(self) -> int:
        self.generation_reads += 1
        return 1

    def verifier_for(self, identity: AttestationIdentity) -> Verifier | None:
        self.lookups.append(identity)
        return self.verifier if self._trusted and identity == _identity() else None


class CandidateVerifier:
    def __init__(self) -> None:
        self.calls: list[tuple[ExactCorpusReference, AttestationIdentity, object]] = []
        self.failures: dict[str, CandidatePersistenceError] = {}
        self.overrides: dict[str, VerifiedCandidateEvidence] = {}

    async def __call__(
        self,
        corpus: ExactCorpusReference,
        identity: AttestationIdentity,
        verifier: AttestationVerifier,
    ) -> VerifiedCandidateEvidence:
        self.calls.append((corpus, identity, verifier))
        failure = self.failures.get(corpus.corpus_version)
        if failure is not None:
            raise failure
        return self.overrides.get(
            corpus.corpus_version, _candidate_evidence(corpus.corpus_version)
        )


class IntegrationSigner:
    algorithm_id = "fixture-ed25519-v1"
    key_id = "fixture-key-1"

    def __init__(self) -> None:
        self.sign_calls = 0

    async def sign(self, payload: bytes) -> bytes:
        self.sign_calls += 1
        return hashlib.sha256(payload).digest()

    async def verify(self, payload: bytes, signature: bytes) -> bool:
        return signature == hashlib.sha256(payload).digest()


class CandidateMemoryStore:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], CandidateStoreRecord] = {}
        self.calls: list[tuple[object, ...]] = []

    def encoded_create_base_size(self, kind: str) -> int:
        self.calls.append(("encoded_create_base_size", kind))
        return 16

    def encoded_record_sizes(
        self, record: CandidateStoreRecord
    ) -> tuple[int, int, str]:
        self.calls.append(("encoded_record_sizes", record.kind))
        size = len(record.model_dump_json().encode("utf-8"))
        return size, size + 64, candidate_inventory_sha256((record,))

    def encoded_document_size(self, record: CandidateStoreRecord) -> int:
        return self.encoded_record_sizes(record)[0]

    def encoded_create_size(
        self, kind: str, records: tuple[CandidateStoreRecord, ...]
    ) -> int:
        return self.encoded_create_base_size(kind) + sum(
            self.encoded_record_sizes(record)[1] for record in records
        )

    async def get(
        self, kind: str, key: str, *, timeout_seconds: int
    ) -> CandidateStoreRecord | None:
        self.calls.append(("get", kind, timeout_seconds))
        return self.records.get((kind, key))

    async def get_many(
        self, kind: str, keys: tuple[str, ...], *, timeout_seconds: int
    ) -> tuple[CandidateStoreRecord | None, ...]:
        self.calls.append(("get_many", kind, len(keys), timeout_seconds))
        return tuple(self.records.get((kind, key)) for key in keys)

    async def create_many_checked(
        self,
        kind: str,
        records: tuple[CandidateStoreRecord, ...],
        *,
        expected_encoded_size: int,
        expected_write_sha256s: tuple[str, ...],
        timeout_seconds: int,
    ) -> None:
        self.calls.append(("create_many", kind, len(records), timeout_seconds))
        sizes = tuple(self.encoded_record_sizes(record) for record in records)
        assert expected_encoded_size == self.encoded_create_base_size(kind) + sum(
            size[1] for size in sizes
        )
        assert expected_write_sha256s == tuple(size[2] for size in sizes)
        assert not any((kind, record.key) in self.records for record in records)
        for record in records:
            self.records[(kind, record.key)] = record

    async def create_many(
        self,
        kind: str,
        records: tuple[CandidateStoreRecord, ...],
        *,
        timeout_seconds: int,
    ) -> None:
        sizes = tuple(self.encoded_record_sizes(record) for record in records)
        await self.create_many_checked(
            kind,
            records,
            expected_encoded_size=self.encoded_create_base_size(kind)
            + sum(size[1] for size in sizes),
            expected_write_sha256s=tuple(size[2] for size in sizes),
            timeout_seconds=timeout_seconds,
        )

    async def list_page(
        self,
        kind: str,
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
            key=lambda record: record.key.encode("utf-8"),
        )
        if after_key is not None:
            records = [record for record in records if record.key > after_key]
        page = tuple(records[:limit])
        return CandidateStorePage(
            records=page,
            next_after_key=page[-1].key if len(records) > limit else None,
        )

    async def aclose(self) -> None:
        self.calls.append(("close",))


def _integration_plan(version: str = "v1") -> Any:
    content = b"Public integration fixture"
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
    ).encode("utf-8")
    return plan_candidate(
        corpus=_corpus(version),
        source=CandidateSourceSnapshot(
            manifest_bytes=manifest,
            documents=(
                CandidateDocumentSnapshot(relative_path="guide.md", content=content),
            ),
        ),
        embedding=EmbeddingSpecification(
            identity="fixture-embedding-v1", dimensions=3
        ),
        embed=lambda values: tuple((1.0, 2.0, 3.0) for _ in values),
    )


class MemoryStore:
    def __init__(self) -> None:
        self.lifecycle: dict[ExactCorpusReference, LifecycleSnapshot] = {}
        self.active: dict[str, ActivePointerSnapshot] = {}
        self.calls: list[tuple[object, ...]] = []
        self.failures: dict[str, list[CorpusLifecycleStoreFailure]] = {}
        self.pre_failures: dict[str, list[CorpusLifecycleStoreFailure]] = {}
        self.close_calls = 0
        self._lock = asyncio.Lock()

    def fail_next(self, operation: str, failure: CorpusLifecycleStoreFailure) -> None:
        self.failures.setdefault(operation, []).append(failure)

    def fail_before_next(
        self, operation: str, failure: CorpusLifecycleStoreFailure
    ) -> None:
        self.pre_failures.setdefault(operation, []).append(failure)

    def _raise_failure(self, operation: str) -> None:
        failures = self.failures.get(operation, [])
        if failures:
            raise failures.pop(0)

    def _raise_pre_failure(self, operation: str) -> None:
        failures = self.pre_failures.get(operation, [])
        if failures:
            raise failures.pop(0)

    async def read_lifecycle_snapshot(
        self, corpus: ExactCorpusReference, *, timeout_seconds: int
    ) -> LifecycleSnapshot:
        self.calls.append(("read_lifecycle", corpus, timeout_seconds))
        self._raise_failure("read_lifecycle")
        return self.lifecycle.get(corpus, LifecycleSnapshot(record=None, audit=None))

    async def read_active_snapshot(
        self, corpus_id: str, *, timeout_seconds: int
    ) -> ActivePointerSnapshot:
        self.calls.append(("read_active", corpus_id, timeout_seconds))
        self._raise_failure("read_active")
        return self.active.get(
            corpus_id,
            ActivePointerSnapshot(pointer=None, audit=None, target_lifecycle=None),
        )

    async def commit_ready(
        self,
        expected_absent: bool,
        replacement: CorpusLifecycleRecord,
        audit: LifecycleAuditRecord,
        *,
        timeout_seconds: int,
    ) -> StoreMutationResult:
        self.calls.append(
            ("commit_ready", replacement.corpus, expected_absent, timeout_seconds)
        )
        self._raise_pre_failure("commit_ready")
        failure = self.failures.get("commit_ready", [])
        async with self._lock:
            if self.lifecycle.get(replacement.corpus) is not None:
                return "conflict"
            self.lifecycle[replacement.corpus] = LifecycleSnapshot(
                record=replacement, audit=audit
            )
            if failure:
                raise failure.pop(0)
            return "applied"

    async def compare_and_swap_active(
        self,
        expected: ActivePointerSnapshot,
        replacement: ActiveCorpusPointer,
        target_ready: CorpusLifecycleRecord,
        audit: LifecycleAuditRecord,
        *,
        timeout_seconds: int,
    ) -> StoreMutationResult:
        self.calls.append(("switch", replacement.target, timeout_seconds))
        self._raise_pre_failure("switch")
        failure = self.failures.get("switch", [])
        async with self._lock:
            current = self.active.get(
                replacement.corpus_id,
                ActivePointerSnapshot(pointer=None, audit=None, target_lifecycle=None),
            )
            target_snapshot = self.lifecycle.get(target_ready.corpus)
            if current != expected or target_snapshot != LifecycleSnapshot(
                record=target_ready, audit=_ready_audit(target_ready)
            ):
                return "conflict"
            self.active[replacement.corpus_id] = ActivePointerSnapshot(
                pointer=replacement, audit=audit, target_lifecycle=target_snapshot
            )
            if failure:
                raise failure.pop(0)
            return "applied"

    async def compare_and_remove(
        self,
        expected_ready: LifecycleSnapshot,
        replacement: CorpusLifecycleRecord,
        expected_active: ActivePointerSnapshot,
        audit: LifecycleAuditRecord,
        *,
        timeout_seconds: int,
    ) -> StoreMutationResult:
        self.calls.append(("remove", replacement.corpus, timeout_seconds))
        self._raise_pre_failure("remove")
        failure = self.failures.get("remove", [])
        async with self._lock:
            current = self.lifecycle.get(replacement.corpus)
            active = self.active.get(
                replacement.corpus.corpus_id,
                ActivePointerSnapshot(pointer=None, audit=None, target_lifecycle=None),
            )
            if current != expected_ready or active != expected_active:
                return "conflict"
            if active.pointer is not None and active.pointer.target == replacement.corpus:
                return "conflict"
            self.lifecycle[replacement.corpus] = LifecycleSnapshot(
                record=replacement, audit=audit
            )
            if failure:
                raise failure.pop(0)
            return "applied"

    async def aclose(self) -> None:
        self.close_calls += 1
        self._raise_failure("close")


class MutationBarrierStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.armed = False
        self._arrivals = 0
        self._release = asyncio.Event()

    async def _barrier(self) -> None:
        if not self.armed:
            return
        self._arrivals += 1
        if self._arrivals == 2:
            self._release.set()
        await self._release.wait()

    async def compare_and_swap_active(
        self,
        expected: ActivePointerSnapshot,
        replacement: ActiveCorpusPointer,
        target_ready: CorpusLifecycleRecord,
        audit: LifecycleAuditRecord,
        *,
        timeout_seconds: int,
    ) -> StoreMutationResult:
        await self._barrier()
        return await super().compare_and_swap_active(
            expected,
            replacement,
            target_ready,
            audit,
            timeout_seconds=timeout_seconds,
        )

    async def compare_and_remove(
        self,
        expected_ready: LifecycleSnapshot,
        replacement: CorpusLifecycleRecord,
        expected_active: ActivePointerSnapshot,
        audit: LifecycleAuditRecord,
        *,
        timeout_seconds: int,
    ) -> StoreMutationResult:
        await self._barrier()
        return await super().compare_and_remove(
            expected_ready,
            replacement,
            expected_active,
            audit,
            timeout_seconds=timeout_seconds,
        )


class ReadBarrierStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.armed = False
        self._arrivals = 0
        self._release = asyncio.Event()

    async def read_lifecycle_snapshot(
        self, corpus: ExactCorpusReference, *, timeout_seconds: int
    ) -> LifecycleSnapshot:
        snapshot = await super().read_lifecycle_snapshot(
            corpus, timeout_seconds=timeout_seconds
        )
        if self.armed:
            self._arrivals += 1
            if self._arrivals == 2:
                self._release.set()
            await self._release.wait()
        return snapshot


def _service(
    store: MemoryStore | None = None,
    verifier: CandidateVerifier | None = None,
    *,
    max_retries: int = 1,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    owns_store: bool = False,
) -> tuple[CorpusLifecycleService, MemoryStore, CandidateVerifier]:
    selected_store = store or MemoryStore()
    selected_verifier = verifier or CandidateVerifier()
    return (
        CorpusLifecycleService(
            store=selected_store,
            verify_attested_candidate=selected_verifier,
            expected_embedding_identity="fixture-embedding-v1",
            expected_embedding_dimensions=3,
            timeout_seconds=7,
            max_retries=cast(Any, max_retries),
            sleep=sleep,
            owns_store=owns_store,
        ),
        selected_store,
        selected_verifier,
    )


def _ready_request(version: str) -> MarkReadyRequest:
    return MarkReadyRequest(
        contract_version="1.0", corpus=_corpus(version), trusted_identity=_identity()
    )


def _switch(
    target: str,
    *,
    action: str = "promote",
    expected: tuple[str, int] | None = None,
) -> SwitchActiveRequest:
    return SwitchActiveRequest(
        contract_version="1.0",
        action=cast(Any, action),
        target=_corpus(target),
        expected=(
            None
            if expected is None
            else ExpectedActivePointer(target=_corpus(expected[0]), revision=expected[1])
        ),
    )


def _assert_error_is_content_free(error: BaseException, *canaries: str) -> None:
    seen: set[int] = set()
    pending: list[object] = [error]
    rendered: list[str] = []
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        rendered.extend((str(value), repr(value)))
        if isinstance(value, BaseException):
            pending.extend((value.args, value.__cause__, value.__context__, vars(value)))
            if hasattr(value, "errors"):
                pending.append(cast(Any, value).errors(include_input=True))
            if hasattr(value, "json"):
                pending.append(cast(Any, value).json(include_input=True))
        elif isinstance(value, Mapping):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, (tuple, list, set, frozenset)):
            pending.extend(value)
    combined = "\n".join(rendered)
    for canary in canaries:
        assert canary not in combined


def test_corpus_lifecycle_contract_constants_are_frozen() -> None:
    assert (
        CORPUS_LIFECYCLE_CONTRACT_VERSION,
        CORPUS_LIFECYCLE_RECORD_SCHEMA_VERSION,
        ACTIVE_CORPUS_POINTER_SCHEMA_VERSION,
        CORPUS_LIFECYCLE_AUDIT_SCHEMA_VERSION,
        LIFECYCLE_READY_REVISION,
        LIFECYCLE_REMOVED_REVISION,
    ) == ("1.0", "1.0", "1.0", "1.0", 0, 1)


def test_public_and_local_compatibility_surfaces_match_accepted_base_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for subprocess_name in ("call", "check_call", "check_output", "Popen", "run"):
        monkeypatch.setattr(subprocess, subprocess_name, _forbidden)
    root = Path(__file__).resolve().parents[2]
    # Raw-byte SHA-256 values from accepted base
    # e1183d48126e1728df380b6cc5ab4cec52ca5048. Keeping the oracle in the
    # test makes it work in history-free archives and shallow CI checkouts.
    accepted_sha256 = {
        "backend/app/capabilities.json": (
            # KAN-44a changes only operations.hosted_readiness, whose exact
            # one-leaf transition is independently frozen in test_capabilities.py.
            "9d2644dfbc6e378920f865d24ed6ceb5abba1864604ce2603005bf454ee83bf8"
        ),
        "backend/app/capabilities.py": (
            "b8fce7afe24633a86226f5f2fa75619de37a074fff83cfef57d9d59dea6f71df"
        ),
        "backend/app/api/capabilities.py": (
            "a53ee240361d25234d8dfe103d60ea675cea3f1bbaa1add8247b6de714b954fb"
        ),
        "backend/app/api/contracts.py": (
            "1895eaf57db82f12eac3855de603e3e686c1e75c010430addca537ed260a2cb1"
        ),
        "backend/app/providers/contracts.py": (
            "b73e171d8921aadc61a9e21df744b813742f65c8d478bf89c8b06a803000040c"
        ),
        "backend/app/retrieval_contracts.py": (
            "fae47b51302018c364a5423b800f754c39f7beba138505399e49b728ca9831af"
        ),
        "backend/app/retrieval_firestore.py": (
            "ca2a9ec03c5b89b9478feabef92b55825b3765492b0a0850df1e860f1a0388cc"
        ),
        "backend/app/vectorstore.py": (
            "2c17b0f70422e1e2e243ad77aee01171b948d062e608db5b1503b72863312fb7"
        ),
        "backend/app/ingest/planner.py": (
            "ca6c987bc03cf3cef8d89384bd5c6b15d71007b83ebb11b3bdace03a90f746e0"
        ),
        "backend/app/ingest/candidate_persistence.py": (
            "e825bebafdabb2339e1d722cddfd46b0f96e0f23dd6502a387203b15d47d70d8"
        ),
        "backend/app/ingest/provenance.py": (
            "24fb9031d05ed5c1b0862bef446b7ea3188e9f3bb57ff27675ad6d0bb9e7f094"
        ),
    }
    assert set(accepted_sha256) == {
        "backend/app/capabilities.json",
        "backend/app/capabilities.py",
        "backend/app/api/capabilities.py",
        "backend/app/api/contracts.py",
        "backend/app/providers/contracts.py",
        "backend/app/retrieval_contracts.py",
        "backend/app/retrieval_firestore.py",
        "backend/app/vectorstore.py",
        "backend/app/ingest/planner.py",
        "backend/app/ingest/candidate_persistence.py",
        "backend/app/ingest/provenance.py",
    }
    for relative, expected in accepted_sha256.items():
        accepted = (root / relative).read_bytes()
        assert hashlib.sha256(accepted).hexdigest() == expected, relative
        corrupted = (
            bytes((accepted[0] ^ 1,)) + accepted[1:] if accepted else b"one-byte-change"
        )
        assert hashlib.sha256(corrupted).hexdigest() != expected, relative


@pytest.mark.parametrize(
    "model,instance",
    [
        (VerifiedLifecycleEvidence, _lifecycle_evidence()),
        (CorpusLifecycleRecord, _record()),
        (
            ActiveCorpusPointer,
            ActiveCorpusPointer(
                schema_version="1.0",
                contract_version="1.0",
                corpus_id="public-docs",
                target=_corpus(),
                revision=0,
                target_lifecycle_revision=0,
                attestation_payload_sha256="d" * 64,
            ),
        ),
        (LifecycleAuditRecord, _ready_audit(_record())),
        (MarkReadyRequest, _ready_request("v1")),
        (ExpectedActivePointer, ExpectedActivePointer(target=_corpus(), revision=0)),
        (SwitchActiveRequest, _switch("v1")),
        (
            RemoveCorpusVersionRequest,
            RemoveCorpusVersionRequest(
                contract_version="1.0",
                corpus=_corpus(),
                expected_lifecycle_revision=0,
            ),
        ),
        (
            LifecycleMutationReceipt,
            LifecycleMutationReceipt(
                contract_version="1.0",
                action="ready",
                disposition="applied",
                subject=_corpus(),
                target=None,
                resulting_revision=0,
            ),
        ),
        (
            LifecycleSnapshot,
            LifecycleSnapshot(record=_record(), audit=_ready_audit(_record())),
        ),
        (
            ActivePointerSnapshot,
            ActivePointerSnapshot(pointer=None, audit=None, target_lifecycle=None),
        ),
        (
            ResolvedActiveState,
            ResolvedActiveState(
                target=_corpus(),
                pointer_revision=0,
                lifecycle_revision=0,
                evidence=_lifecycle_evidence(),
            ),
        ),
    ],
)
def test_public_models_are_strict_frozen_content_free_and_round_trip(
    model: type[Any], instance: Any, caplog: pytest.LogCaptureFixture
) -> None:
    canary = f"PRIVATE-{model.__name__}-CANARY"
    malformed = "{" + canary
    adapter = TypeAdapter(model)
    values = instance.model_dump(mode="python", round_trip=True)
    json_values = instance.model_dump(mode="json", round_trip=True)
    structural_json = json.dumps({**json_values, canary: canary})
    failing_routes: tuple[Callable[[], object], ...] = (
        lambda: model(**{**values, canary: canary}),
        lambda: model.model_validate({**values, canary: canary}, extra="allow"),
        lambda: model.model_validate_json(malformed),
        lambda: model.model_validate_json(structural_json),
        lambda: model.model_validate_strings({**json_values, canary: canary}),
        lambda: adapter.validate_python({**values, canary: canary}),
        lambda: adapter.validate_json(malformed),
        lambda: adapter.validate_json(structural_json),
        lambda: adapter.validate_strings({**json_values, canary: canary}),
        lambda: model.model_construct(**{**values, canary: canary}),
        lambda: model.construct(**{**values, canary: canary}),
        lambda: instance.model_copy(update={canary: canary}),
        lambda: instance.copy(update={canary: canary}),
        lambda: instance.__replace__(**{canary: canary}),
    )
    for route in failing_routes:
        with pytest.raises(CorpusLifecycleError) as caught:
            route()
        _assert_error_is_content_free(caught.value, canary, malformed)
    with pytest.raises(CorpusLifecycleError) as frozen:
        setattr(instance, next(iter(values)), canary)
    _assert_error_is_content_free(frozen.value, canary)
    dumped = instance.model_dump_json()
    assert model(**values) == instance
    assert model.model_validate(values) == instance
    assert model.model_validate_json(dumped) == instance
    assert model.model_validate_strings(json_values) == instance
    assert adapter.validate_python(values) == instance
    assert adapter.validate_json(dumped) == instance
    assert adapter.validate_strings(json_values) == instance
    assert model.model_construct(**values) == instance
    assert model.construct(**values) == instance
    assert instance.model_copy() == instance
    assert instance.copy() == instance
    assert instance.__replace__() == instance
    assert model.model_rebuild(force=True) is True
    with pytest.raises(CorpusLifecycleError) as rebuilt:
        TypeAdapter(model).validate_json(malformed)
    _assert_error_is_content_free(rebuilt.value, canary, malformed)
    assert caplog.records == []


def test_nested_dependency_models_are_revalidated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    forged_corpus = ExactCorpusReference.model_construct(
        kind="exact", corpus_id="active", corpus_version="latest"
    )
    forged_identity = _identity()
    object.__setattr__(forged_identity, "algorithm_id", " fixture ")
    object.__setattr__(forged_identity, "key_id", " fixture ")
    with pytest.raises(CorpusLifecycleError) as corpus_error:
        MarkReadyRequest(
            contract_version="1.0",
            corpus=forged_corpus,
            trusted_identity=_identity(),
        )
    with pytest.raises(CorpusLifecycleError) as identity_error:
        MarkReadyRequest(
            contract_version="1.0",
            corpus=_corpus(),
            trusted_identity=forged_identity,
        )
    _assert_error_is_content_free(corpus_error.value, "active", "latest")
    _assert_error_is_content_free(identity_error.value, " fixture ")

    forged_corpus = _corpus()
    object.__setattr__(forged_corpus, "__pydantic_extra__", {"PRIVATE": "CORPUS"})
    forged_identity = _identity()
    object.__setattr__(forged_identity, "__pydantic_extra__", {"PRIVATE": "IDENTITY"})
    for corpus, identity, canary in (
        (forged_corpus, _identity(), "CORPUS"),
        (_corpus(), forged_identity, "IDENTITY"),
    ):
        with pytest.raises(CorpusLifecycleError) as hidden_extra:
            MarkReadyRequest(
                contract_version="1.0", corpus=corpus, trusted_identity=identity
            )
        _assert_error_is_content_free(hidden_extra.value, canary)

    request = _ready_request("v1")
    malformed_identity = request.model_dump(mode="json", round_trip=True)
    malformed_identity["trusted_identity"] = {
        "algorithm_id": " PRIVATE-IDENTITY-CANARY ",
        "key_id": "fixture-key-1",
    }
    _assert_invalid_payload_on_all_routes(
        MarkReadyRequest,
        request,
        malformed_identity,
        "PRIVATE-IDENTITY-CANARY",
    )

    forged_identity = _identity()
    object.__setattr__(
        forged_identity,
        "__pydantic_extra__",
        {"PRIVATE-FORGED-IDENTITY": "PRIVATE-FORGED-IDENTITY"},
    )
    forged_payload = request.model_dump(mode="python", round_trip=True)
    forged_payload["trusted_identity"] = forged_identity
    identity_routes: tuple[Callable[[], object], ...] = (
        lambda: MarkReadyRequest(**forged_payload),
        lambda: MarkReadyRequest.model_validate(forged_payload),
        lambda: TypeAdapter(MarkReadyRequest).validate_python(forged_payload),
        lambda: MarkReadyRequest.model_construct(**forged_payload),
        lambda: MarkReadyRequest.construct(**forged_payload),
        lambda: request.model_copy(update=forged_payload),
        lambda: request.copy(update=forged_payload),
        lambda: request.__replace__(**forged_payload),
    )
    for route in identity_routes:
        with pytest.raises(CorpusLifecycleError) as caught:
            route()
        _assert_error_is_content_free(caught.value, "PRIVATE-FORGED-IDENTITY")
    assert caplog.records == []


@pytest.mark.parametrize(
    "model,values,field,bad",
    [
        (
            VerifiedLifecycleEvidence,
            _lifecycle_evidence().model_dump(round_trip=True),
            "embedding_dimensions",
            True,
        ),
        (
            VerifiedLifecycleEvidence,
            _lifecycle_evidence().model_dump(round_trip=True),
            "document_count",
            "2",
        ),
        (
            VerifiedLifecycleEvidence,
            _lifecycle_evidence().model_dump(round_trip=True),
            "plan_sha256",
            "A" * 64,
        ),
        (
            VerifiedLifecycleEvidence,
            _lifecycle_evidence().model_dump(round_trip=True),
            "signing_key_id",
            " key ",
        ),
        (
            CorpusLifecycleRecord,
            _record().model_dump(round_trip=True),
            "state",
            "removed",
        ),
        (
            CorpusLifecycleRecord,
            _record().model_dump(round_trip=True),
            "revision",
            True,
        ),
        (
            ExpectedActivePointer,
            ExpectedActivePointer(target=_corpus(), revision=0).model_dump(
                round_trip=True
            ),
            "revision",
            True,
        ),
        (
            ActiveCorpusPointer,
            ActiveCorpusPointer(
                schema_version="1.0",
                contract_version="1.0",
                corpus_id="public-docs",
                target=_corpus(),
                revision=0,
                target_lifecycle_revision=0,
                attestation_payload_sha256="d" * 64,
            ).model_dump(round_trip=True),
            "corpus_id",
            7,
        ),
        (
            ActiveCorpusPointer,
            ActiveCorpusPointer(
                schema_version="1.0",
                contract_version="1.0",
                corpus_id="public-docs",
                target=_corpus(),
                revision=0,
                target_lifecycle_revision=0,
                attestation_payload_sha256="d" * 64,
            ).model_dump(round_trip=True),
            "target_lifecycle_revision",
            False,
        ),
        (
            RemoveCorpusVersionRequest,
            RemoveCorpusVersionRequest(
                contract_version="1.0",
                corpus=_corpus(),
                expected_lifecycle_revision=0,
            ).model_dump(round_trip=True),
            "expected_lifecycle_revision",
            False,
        ),
        (
            ResolvedActiveState,
            ResolvedActiveState(
                target=_corpus(),
                pointer_revision=0,
                lifecycle_revision=0,
                evidence=_lifecycle_evidence(),
            ).model_dump(round_trip=True),
            "lifecycle_revision",
            False,
        ),
    ],
)
def test_public_models_reject_coercion_boolean_hash_identity_and_state(
    model: type[Any],
    values: dict[str, object],
    field: str,
    bad: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    canary = "PRIVATE-INVALID-FIELD"
    instance = model.model_validate(values)
    corrupted = {**values, field: bad}
    corrupted_json = json.dumps(corrupted)
    adapter = TypeAdapter(model)
    routes: tuple[Callable[[], object], ...] = (
        lambda: model(**corrupted),
        lambda: model.model_validate(corrupted),
        lambda: model.model_validate_json(corrupted_json),
        lambda: model.model_validate_strings(corrupted),
        lambda: adapter.validate_python(corrupted),
        lambda: adapter.validate_json(corrupted_json),
        lambda: adapter.validate_strings(corrupted),
        lambda: model.model_construct(**corrupted),
        lambda: model.construct(**corrupted),
        lambda: instance.model_copy(update={field: bad}),
        lambda: instance.copy(update={field: bad}),
        lambda: instance.__replace__(**{field: bad}),
    )
    for route in routes:
        with pytest.raises(CorpusLifecycleError) as caught:
            route()
        _assert_error_is_content_free(caught.value, canary, str(bad))
    assert caplog.records == []


def _replace_first_exact_corpus(value: object, replacement: object) -> tuple[object, bool]:
    if isinstance(value, Mapping):
        if set(("kind", "corpus_id", "corpus_version")).issubset(value):
            return replacement, True
        copied: dict[object, object] = {}
        replaced = False
        for key, nested in value.items():
            if replaced:
                copied[key] = nested
            else:
                copied[key], replaced = _replace_first_exact_corpus(nested, replacement)
        return copied, replaced
    if isinstance(value, tuple):
        tuple_items: list[object] = []
        replaced = False
        for nested in value:
            if replaced:
                tuple_items.append(nested)
            else:
                tuple_value, replaced = _replace_first_exact_corpus(
                    nested, replacement
                )
                tuple_items.append(tuple_value)
        return tuple(tuple_items), replaced
    if isinstance(value, list):
        list_items: list[object] = []
        replaced = False
        for nested in value:
            if replaced:
                list_items.append(nested)
            else:
                list_value, replaced = _replace_first_exact_corpus(nested, replacement)
                list_items.append(list_value)
        return list_items, replaced
    return value, False


def _assert_invalid_payload_on_all_routes(
    model: type[Any], instance: Any, payload: dict[str, object], canary: str
) -> None:
    encoded = json.dumps(payload)
    adapter = TypeAdapter(model)
    routes: tuple[Callable[[], object], ...] = (
        lambda: model(**payload),
        lambda: model.model_validate(payload),
        lambda: model.model_validate_json(encoded),
        lambda: model.model_validate_strings(payload),
        lambda: adapter.validate_python(payload),
        lambda: adapter.validate_json(encoded),
        lambda: adapter.validate_strings(payload),
        lambda: model.model_construct(**payload),
        lambda: model.construct(**payload),
        lambda: instance.model_copy(update=payload),
        lambda: instance.copy(update=payload),
        lambda: instance.__replace__(**payload),
    )
    for route in routes:
        with pytest.raises(CorpusLifecycleError) as caught:
            route()
        _assert_error_is_content_free(caught.value, canary)


def _assert_invalid_python_payload_routes(
    model: type[Any], instance: Any, payload: dict[str, object], canary: str
) -> None:
    adapter = TypeAdapter(model)
    routes: tuple[Callable[[], object], ...] = (
        lambda: model(**payload),
        lambda: model.model_validate(payload),
        lambda: adapter.validate_python(payload),
        lambda: model.model_construct(**payload),
        lambda: model.construct(**payload),
        lambda: instance.model_copy(update=payload),
        lambda: instance.copy(update=payload),
        lambda: instance.__replace__(**payload),
    )
    for route in routes:
        with pytest.raises(CorpusLifecycleError) as caught:
            route()
        _assert_error_is_content_free(caught.value, canary)


def test_every_corpus_containing_model_revalidates_nested_corpus_on_all_routes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ready = _record()
    instances = (
        _lifecycle_evidence(),
        ready,
        ActiveCorpusPointer(
            schema_version="1.0",
            contract_version="1.0",
            corpus_id="public-docs",
            target=_corpus(),
            revision=0,
            target_lifecycle_revision=0,
            attestation_payload_sha256="d" * 64,
        ),
        _ready_audit(ready),
        _ready_request("v1"),
        ExpectedActivePointer(target=_corpus(), revision=0),
        _switch("v1"),
        RemoveCorpusVersionRequest(
            contract_version="1.0", corpus=_corpus(), expected_lifecycle_revision=0
        ),
        LifecycleMutationReceipt(
            contract_version="1.0",
            action="ready",
            disposition="applied",
            subject=_corpus(),
            target=None,
            resulting_revision=0,
        ),
        LifecycleSnapshot(record=ready, audit=_ready_audit(ready)),
        _pointer_snapshot(),
        ResolvedActiveState(
            target=_corpus(),
            pointer_revision=0,
            lifecycle_revision=0,
            evidence=_lifecycle_evidence(),
        ),
    )
    bad_reference = {
        "kind": "exact",
        "corpus_id": "PRIVATE-NESTED-CORPUS",
        "corpus_version": "latest",
    }
    for instance in instances:
        model = type(instance)
        raw = instance.model_dump(mode="json", round_trip=True)
        corrupted, replaced = _replace_first_exact_corpus(raw, bad_reference)
        assert replaced
        _assert_invalid_payload_on_all_routes(
            model, instance, cast(dict[str, object], corrupted), "PRIVATE-NESTED-CORPUS"
        )
    assert caplog.records == []


def test_every_corpus_containing_model_rejects_forged_exact_instances(
    caplog: pytest.LogCaptureFixture,
) -> None:
    forged = _corpus()
    object.__setattr__(
        forged, "__pydantic_extra__", {"PRIVATE-FORGED-CORPUS": "PRIVATE-FORGED-CORPUS"}
    )
    ready = _record()
    instances = (
        _lifecycle_evidence(),
        ready,
        ActiveCorpusPointer(
            schema_version="1.0",
            contract_version="1.0",
            corpus_id="public-docs",
            target=_corpus(),
            revision=0,
            target_lifecycle_revision=0,
            attestation_payload_sha256="d" * 64,
        ),
        _ready_audit(ready),
        _ready_request("v1"),
        ExpectedActivePointer(target=_corpus(), revision=0),
        _switch("v1"),
        RemoveCorpusVersionRequest(
            contract_version="1.0", corpus=_corpus(), expected_lifecycle_revision=0
        ),
        LifecycleMutationReceipt(
            contract_version="1.0",
            action="ready",
            disposition="applied",
            subject=_corpus(),
            target=None,
            resulting_revision=0,
        ),
        LifecycleSnapshot(record=ready, audit=_ready_audit(ready)),
        _pointer_snapshot(),
        ResolvedActiveState(
            target=_corpus(),
            pointer_revision=0,
            lifecycle_revision=0,
            evidence=_lifecycle_evidence(),
        ),
    )
    for instance in instances:
        model: type[Any] = type(instance)
        raw = instance.model_dump(mode="python", round_trip=True)
        corrupted, replaced = _replace_first_exact_corpus(raw, forged)
        assert replaced
        payload = cast(dict[str, object], corrupted)
        _assert_invalid_python_payload_routes(
            model, instance, payload, "PRIVATE-FORGED-CORPUS"
        )
    assert caplog.records == []


def test_keys_are_domain_separated_path_safe_and_hash_seed_stable() -> None:
    corpus = _corpus()
    keys = (
        lifecycle_record_key(corpus),
        active_pointer_key(corpus.corpus_id),
        lifecycle_audit_key(corpus, 0),
        lifecycle_audit_key(corpus, 1),
        active_audit_key(corpus.corpus_id, 0),
    )
    assert len(set(keys)) == len(keys)
    assert all(len(key) < 256 and "/" not in key for key in keys)
    script = (
        "from app.corpus_lifecycle import *; "
        "from app.retrieval_contracts import ExactCorpusReference; "
        "c=ExactCorpusReference(kind='exact',corpus_id='public-docs',"
        "corpus_version='v1'); "
        "print((lifecycle_record_key(c),active_pointer_key(c.corpus_id),"
        "lifecycle_audit_key(c,0),active_audit_key(c.corpus_id,0)))"
    )
    outputs = []
    for seed in ("1", "8675309"):
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH=".")
        outputs.append(
            subprocess.check_output([sys.executable, "-c", script], env=env, text=True)
        )
    assert outputs[0] == outputs[1]


@pytest.mark.asyncio
async def test_complete_lifecycle_ready_promote_rollback_remove_and_replay() -> None:
    service, store, verifier = _service()
    policy = Policy()
    ready_a = await service.mark_ready(_ready_request("v1"), policy)
    assert ready_a.disposition == "applied"
    assert await service.mark_ready(_ready_request("v1"), policy) == ready_a.model_copy(
        update={"disposition": "confirmed"}
    )
    assert store.active == {}
    ready_b = await service.mark_ready(_ready_request("v2"), policy)
    assert ready_b.disposition == "applied"

    first = await service.switch_active(_switch("v1"), policy)
    assert first.resulting_revision == 0
    assert first.disposition == "applied"
    verifier_calls_before_replay = len(verifier.calls)
    writes_before_replay = len(
        [call for call in store.calls if call[0] in {"commit_ready", "switch", "remove"}]
    )
    assert await service.switch_active(_switch("v1"), policy) == first.model_copy(
        update={"disposition": "confirmed"}
    )
    assert len(verifier.calls) == verifier_calls_before_replay + 1
    assert len(
        [call for call in store.calls if call[0] in {"commit_ready", "switch", "remove"}]
    ) == writes_before_replay
    second = await service.switch_active(_switch("v2", expected=("v1", 0)), policy)
    assert second.resulting_revision == 1
    rollback = await service.switch_active(
        _switch("v1", action="rollback", expected=("v2", 1)), policy
    )
    assert rollback.resulting_revision == 2

    with pytest.raises(CorpusLifecycleError) as active_error:
        await service.remove_version(
            RemoveCorpusVersionRequest(
                contract_version="1.0",
                corpus=_corpus("v1"),
                expected_lifecycle_revision=0,
            )
        )
    assert active_error.value.code == "active_version_forbidden"
    await service.switch_active(_switch("v2", expected=("v1", 2)), policy)
    remove_request = RemoveCorpusVersionRequest(
        contract_version="1.0",
        corpus=_corpus("v1"),
        expected_lifecycle_revision=0,
    )
    assert (await service.remove_version(remove_request)).disposition == "applied"
    assert (await service.remove_version(remove_request)).disposition == "confirmed"
    removed = cast(CorpusLifecycleRecord, store.lifecycle[_corpus("v1")].record)
    assert removed.state == "logically_removed"
    assert removed.revision == 1

    writes = len(
        [call for call in store.calls if call[0] in {"commit_ready", "switch", "remove"}]
    )
    with pytest.raises(CorpusLifecycleError) as ready_again:
        await service.mark_ready(_ready_request("v1"), policy)
    assert ready_again.value.code == "lifecycle_conflict"
    with pytest.raises(CorpusLifecycleError) as activate_removed:
        await service.switch_active(_switch("v1", expected=("v2", 3)), policy)
    assert activate_removed.value.code == "lifecycle_conflict"
    assert len(
        [call for call in store.calls if call[0] in {"commit_ready", "switch", "remove"}]
    ) == writes


@pytest.mark.asyncio
async def test_untrusted_and_m8_failure_stop_before_lifecycle_access() -> None:
    service, store, verifier = _service()
    with pytest.raises(CorpusLifecycleError) as untrusted:
        await service.mark_ready(_ready_request("v1"), Policy(trusted=False))
    assert untrusted.value.code == "attestation_untrusted"
    assert verifier.calls == []
    assert store.calls == []

    verifier.failures["partial"] = CandidatePersistenceError("attestation_failed")
    with pytest.raises(CorpusLifecycleError) as unavailable:
        await service.mark_ready(_ready_request("partial"), Policy())
    assert unavailable.value.code == "candidate_unavailable"
    assert store.calls == []


@pytest.mark.asyncio
async def test_bound_m8_verifier_performs_full_readback_without_sign_or_write() -> None:
    candidate_store = CandidateMemoryStore()
    signer = IntegrationSigner()
    persistence = CandidatePersistenceService(
        store=cast(Any, candidate_store),
        signer=signer,
        expected_embedding_identity="fixture-embedding-v1",
        expected_embedding_dimensions=3,
        timeout_seconds=3,
        max_retries=1,
        sleep=asyncio.sleep,
    )
    plan = _integration_plan()
    await persistence.persist_and_attest_candidate(
        CandidatePersistenceRequest(contract_version="1.0", plan=plan)
    )
    sign_calls = signer.sign_calls
    call_boundary = len(candidate_store.calls)
    m8 = CandidateAttestationVerificationService(
        store=cast(Any, candidate_store),
        timeout_seconds=3,
        max_retries=1,
        sleep=asyncio.sleep,
    )
    verification_calls = 0

    async def verify(
        corpus: ExactCorpusReference,
        identity: AttestationIdentity,
        verifier: AttestationVerifier,
    ) -> VerifiedCandidateEvidence:
        nonlocal verification_calls
        verification_calls += 1
        return await m8.verify_attested_candidate(corpus, identity, verifier)

    lifecycle_store = MemoryStore()
    service = CorpusLifecycleService(
        store=lifecycle_store,
        verify_attested_candidate=verify,
        expected_embedding_identity="fixture-embedding-v1",
        expected_embedding_dimensions=3,
        timeout_seconds=3,
        max_retries=1,
        sleep=asyncio.sleep,
    )
    policy = Policy()
    receipt = await service.mark_ready(_ready_request("v1"), policy)
    assert receipt.disposition == "applied"
    assert verification_calls == 1
    assert policy.verifier.verify_calls == 1
    assert signer.sign_calls == sign_calls
    m8_calls = candidate_store.calls[call_boundary:]
    assert [call[0] for call in m8_calls].count("get") == 2
    assert [call[0] for call in m8_calls].count("list_page") == 2
    assert not any(call[0] == "create_many" for call in m8_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("verify_result", [False, 1, "true", None])
async def test_bound_m8_verifier_false_or_nonboolean_creates_no_lifecycle(
    verify_result: object,
) -> None:
    candidate_store = CandidateMemoryStore()
    persistence = CandidatePersistenceService(
        store=cast(Any, candidate_store),
        signer=IntegrationSigner(),
        expected_embedding_identity="fixture-embedding-v1",
        expected_embedding_dimensions=3,
        timeout_seconds=3,
        max_retries=1,
        sleep=asyncio.sleep,
    )
    await persistence.persist_and_attest_candidate(
        CandidatePersistenceRequest(contract_version="1.0", plan=_integration_plan())
    )
    m8 = CandidateAttestationVerificationService(
        store=cast(Any, candidate_store),
        timeout_seconds=3,
        max_retries=1,
        sleep=asyncio.sleep,
    )
    lifecycle_store = MemoryStore()
    service = CorpusLifecycleService(
        store=lifecycle_store,
        verify_attested_candidate=m8.verify_attested_candidate,
        expected_embedding_identity="fixture-embedding-v1",
        expected_embedding_dimensions=3,
        timeout_seconds=3,
        max_retries=1,
        sleep=asyncio.sleep,
    )
    policy = Policy(verify_result=verify_result)
    with pytest.raises(CorpusLifecycleError) as caught:
        await service.mark_ready(_ready_request("v1"), policy)
    assert caught.value.code == "candidate_unavailable"
    assert policy.verifier.verify_calls == 1
    assert lifecycle_store.calls == []


@pytest.mark.asyncio
async def test_embedding_and_stored_evidence_mismatches_create_no_state() -> None:
    service, store, verifier = _service()
    verifier.overrides["v1"] = _candidate_evidence().model_copy(
        update={"embedding_identity": "other-embedding"}
    )
    with pytest.raises(CorpusLifecycleError) as embedding:
        await service.mark_ready(_ready_request("v1"), Policy())
    assert embedding.value.code == "candidate_unavailable"
    assert store.calls == []

    verifier.overrides["v1"] = _candidate_evidence().model_copy(
        update={"embedding_dimensions": 4}
    )
    with pytest.raises(CorpusLifecycleError) as dimensions:
        await service.mark_ready(_ready_request("v1"), Policy())
    assert dimensions.value.code == "candidate_unavailable"
    assert store.calls == []

    verifier.overrides.clear()
    await service.mark_ready(_ready_request("v1"), Policy())
    verifier.overrides["v1"] = _candidate_evidence(marker="e")
    writes = len([call for call in store.calls if call[0] == "switch"])
    with pytest.raises(CorpusLifecycleError) as evidence:
        await service.switch_active(_switch("v1"), Policy())
    assert evidence.value.code == "lifecycle_conflict"
    assert len([call for call in store.calls if call[0] == "switch"]) == writes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    ["absent_header", "header_only", "partial", "unattested", "foreign", "malformed"],
)
async def test_bound_m8_incomplete_or_bad_attestation_has_no_lifecycle_effect(
    corruption: str,
) -> None:
    source = CandidateMemoryStore()
    signer = IntegrationSigner()
    persistence = CandidatePersistenceService(
        store=cast(Any, source),
        signer=signer,
        expected_embedding_identity="fixture-embedding-v1",
        expected_embedding_dimensions=3,
        timeout_seconds=3,
        max_retries=1,
        sleep=asyncio.sleep,
    )
    await persistence.persist_and_attest_candidate(
        CandidatePersistenceRequest(contract_version="1.0", plan=_integration_plan())
    )
    candidate_store = CandidateMemoryStore()
    candidate_store.records = dict(source.records)
    if corruption == "absent_header":
        header_key = next(key for key in candidate_store.records if key[0] == "candidate")
        del candidate_store.records[header_key]
    elif corruption == "header_only":
        candidate_store.records = {
            key: value for key, value in candidate_store.records.items() if key[0] == "candidate"
        }
    elif corruption == "partial":
        chunk_key = next(key for key in candidate_store.records if key[0] == "chunk")
        del candidate_store.records[chunk_key]
    elif corruption == "unattested":
        attestation_key = next(
            key for key in candidate_store.records if key[0] == "attestation"
        )
        del candidate_store.records[attestation_key]
    else:
        attestation_key = next(
            key for key in candidate_store.records if key[0] == "attestation"
        )
        attestation = candidate_store.records[attestation_key]
        value = cast(dict[str, Any], dict(attestation.value))
        if corruption == "foreign":
            payload = dict(cast(Mapping[str, object], value["payload"]))
            payload["corpus_version"] = "PRIVATE-FOREIGN"
            value["payload"] = payload
        else:
            value["signature_b64url"] = "PRIVATE-MALFORMED"
        candidate_store.records[attestation_key] = CandidateStoreRecord.model_construct(
            kind="attestation", key=attestation.key, value=value
        )
    m8 = CandidateAttestationVerificationService(
        store=cast(Any, candidate_store),
        timeout_seconds=3,
        max_retries=1,
        sleep=asyncio.sleep,
    )
    verification_calls = 0

    async def verify(
        corpus: ExactCorpusReference,
        identity: AttestationIdentity,
        verifier: AttestationVerifier,
    ) -> VerifiedCandidateEvidence:
        nonlocal verification_calls
        verification_calls += 1
        return await m8.verify_attested_candidate(corpus, identity, verifier)

    lifecycle_store = MemoryStore()
    service = CorpusLifecycleService(
        store=lifecycle_store,
        verify_attested_candidate=verify,
        expected_embedding_identity="fixture-embedding-v1",
        expected_embedding_dimensions=3,
        timeout_seconds=3,
        max_retries=1,
        sleep=asyncio.sleep,
    )
    with pytest.raises(CorpusLifecycleError) as caught:
        await service.mark_ready(_ready_request("v1"), Policy())
    expected = (
        "malformed_store" if corruption in {"header_only", "partial"} else "candidate_unavailable"
    )
    assert caught.value.code == expected
    _assert_error_is_content_free(caught.value, "PRIVATE")
    assert verification_calls == 1
    assert lifecycle_store.calls == []
    assert not any(call[0] == "create_many" for call in candidate_store.calls)


@pytest.mark.asyncio
async def test_incomplete_prefix_cannot_be_removed_and_candidate_is_untouched() -> None:
    candidate_store = CandidateMemoryStore()
    m8 = CandidateAttestationVerificationService(
        store=cast(Any, candidate_store),
        timeout_seconds=3,
        max_retries=1,
        sleep=asyncio.sleep,
    )
    service, store, _ = _service()
    service = CorpusLifecycleService(
        store=store,
        verify_attested_candidate=m8.verify_attested_candidate,
        expected_embedding_identity="fixture-embedding-v1",
        expected_embedding_dimensions=3,
        timeout_seconds=3,
        max_retries=1,
        sleep=asyncio.sleep,
    )
    with pytest.raises(CorpusLifecycleError) as caught:
        await service.remove_version(
            RemoveCorpusVersionRequest(
                contract_version="1.0",
                corpus=_corpus("v1"),
                expected_lifecycle_revision=0,
            )
        )
    assert caught.value.code == "lifecycle_conflict"
    assert candidate_store.calls == []
    assert not any(
        call[0] in {"create_many", "delete", "update", "list"}
        for call in candidate_store.calls
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "nested,expected",
    [
        ("attestation_failed", "candidate_unavailable"),
        ("store_unavailable", "store_unavailable"),
        ("candidate_conflict", "malformed_store"),
        ("store_bounds_exceeded", "malformed_store"),
        ("malformed_store", "malformed_store"),
        ("invalid_plan", "malformed_store"),
        ("unsupported_embedding", "malformed_store"),
    ],
)
async def test_m8_error_mapping_is_fixed(nested: str, expected: str) -> None:
    service, _, verifier = _service()
    verifier.failures["v1"] = CandidatePersistenceError(cast(Any, nested))
    with pytest.raises(CorpusLifecycleError) as caught:
        await service.mark_ready(_ready_request("v1"), Policy())
    assert caught.value.code == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["policy", "verifier", "candidate", "store", "sleep"])
async def test_public_failures_do_not_retain_raw_exception_canaries(
    source: str, caplog: pytest.LogCaptureFixture
) -> None:
    canary = f"PRIVATE-{source}-EXCEPTION-CANARY"
    service, store, _ = _service()
    invoke: Callable[[], Awaitable[LifecycleMutationReceipt]]

    class RaisingPolicy(Policy):
        @property
        def policy_version(self) -> str:
            raise ValueError(canary)

    if source == "policy":
        invoke = partial(service.mark_ready, _ready_request("v1"), RaisingPolicy())
    elif source == "verifier":
        class RaisingLookup(Policy):
            def verifier_for(self, identity: AttestationIdentity) -> Verifier | None:
                del identity
                raise ValueError(canary)

        invoke = partial(service.mark_ready, _ready_request("v1"), RaisingLookup())
    elif source == "candidate":
        async def raising_candidate(*args: object) -> VerifiedCandidateEvidence:
            del args
            raise RuntimeError(canary)

        service = CorpusLifecycleService(
            store=store,
            verify_attested_candidate=cast(Any, raising_candidate),
            expected_embedding_identity="fixture-embedding-v1",
            expected_embedding_dimensions=3,
            timeout_seconds=7,
            max_retries=1,
            sleep=asyncio.sleep,
        )
        invoke = partial(service.mark_ready, _ready_request("v1"), Policy())
    elif source == "store":
        async def raising_read(*args: object, **kwargs: object) -> LifecycleSnapshot:
            del args, kwargs
            raise RuntimeError(canary)

        store.read_lifecycle_snapshot = raising_read  # type: ignore[method-assign]
        invoke = partial(service.mark_ready, _ready_request("v1"), Policy())
    else:
        async def raising_sleep(_: float) -> None:
            raise RuntimeError(canary)

        service, store, _ = _service(sleep=raising_sleep)
        store.fail_next("read_lifecycle", CorpusLifecycleStoreFailure("retryable"))
        invoke = partial(service.mark_ready, _ready_request("v1"), Policy())
    with pytest.raises(CorpusLifecycleError) as caught:
        await invoke()
    _assert_error_is_content_free(caught.value, canary)
    assert canary not in repr(service.__dict__)
    assert canary not in caplog.text
    assert caplog.records == []


@pytest.mark.asyncio
async def test_switch_race_has_one_winner_and_one_conflict() -> None:
    store = MutationBarrierStore()
    service, _, _ = _service(store)
    policy = Policy()
    for version in ("v1", "v2", "v3"):
        await service.mark_ready(_ready_request(version), policy)
    await service.switch_active(_switch("v1"), policy)
    store.armed = True
    results = await asyncio.gather(
        service.switch_active(_switch("v2", expected=("v1", 0)), policy),
        service.switch_active(_switch("v3", expected=("v1", 0)), policy),
        return_exceptions=True,
    )
    receipts = [item for item in results if isinstance(item, LifecycleMutationReceipt)]
    errors = [item for item in results if isinstance(item, CorpusLifecycleError)]
    assert len(receipts) == 1
    assert len(errors) == 1
    assert errors[0].code == "lifecycle_conflict"
    assert cast(ActiveCorpusPointer, store.active["public-docs"].pointer).revision == 1


@pytest.mark.asyncio
async def test_promotion_removal_barrier_has_one_valid_winner() -> None:
    store = MutationBarrierStore()
    service, _, _ = _service(store)
    policy = Policy()
    for version in ("v1", "v2"):
        await service.mark_ready(_ready_request(version), policy)
    await service.switch_active(_switch("v1"), policy)
    store.armed = True
    removal = RemoveCorpusVersionRequest(
        contract_version="1.0",
        corpus=_corpus("v2"),
        expected_lifecycle_revision=0,
    )
    results = await asyncio.gather(
        service.switch_active(_switch("v2", expected=("v1", 0)), policy),
        service.remove_version(removal),
        return_exceptions=True,
    )
    assert sum(isinstance(item, LifecycleMutationReceipt) for item in results) == 1
    errors = [item for item in results if isinstance(item, CorpusLifecycleError)]
    assert len(errors) == 1 and errors[0].code == "lifecycle_conflict"
    active = cast(ActiveCorpusPointer, store.active["public-docs"].pointer)
    target = cast(CorpusLifecycleRecord, store.lifecycle[_corpus("v2")].record)
    assert not (active.target == _corpus("v2") and target.state == "logically_removed")


@pytest.mark.asyncio
async def test_ready_removal_barrier_preserves_terminal_state() -> None:
    store = ReadBarrierStore()
    service, _, _ = _service(store)
    policy = Policy()
    await service.mark_ready(_ready_request("v1"), policy)
    store.armed = True
    removal = RemoveCorpusVersionRequest(
        contract_version="1.0",
        corpus=_corpus("v1"),
        expected_lifecycle_revision=0,
    )
    ready_result, remove_result = await asyncio.gather(
        service.mark_ready(_ready_request("v1"), policy),
        service.remove_version(removal),
    )
    assert ready_result.disposition == "confirmed"
    assert remove_result.disposition == "applied"
    assert cast(CorpusLifecycleRecord, store.lifecycle[_corpus("v1")].record).state == (
        "logically_removed"
    )


@pytest.mark.asyncio
async def test_policy_is_captured_once_and_current_rejection_stops_target_scan() -> None:
    service, _, verifier = _service()
    setup_policy = Policy()
    await service.mark_ready(_ready_request("v1"), setup_policy)
    await service.mark_ready(_ready_request("v2"), setup_policy)
    first_policy = Policy()
    await service.switch_active(_switch("v1"), first_policy)
    assert (first_policy.version_reads, first_policy.generation_reads) == (1, 1)

    later_policy = Policy()
    before = len(verifier.calls)
    await service.switch_active(_switch("v2", expected=("v1", 0)), later_policy)
    assert (later_policy.version_reads, later_policy.generation_reads) == (1, 1)
    assert len(later_policy.lookups) == 2
    assert len(verifier.calls) == before + 1

    stale_policy = Policy()
    before = len(verifier.calls)
    with pytest.raises(CorpusLifecycleError) as stale:
        await service.switch_active(_switch("v1", expected=("v3", 0)), stale_policy)
    assert stale.value.code == "lifecycle_conflict"
    assert len(verifier.calls) == before
    assert (stale_policy.version_reads, stale_policy.generation_reads) == (1, 1)

    revoked_policy = Policy(trusted=False)
    before = len(verifier.calls)
    with pytest.raises(CorpusLifecycleError) as revoked:
        await service.switch_active(
            _switch("v1", action="rollback", expected=("v2", 1)), revoked_policy
        )
    assert revoked.value.code == "attestation_untrusted"
    assert len(verifier.calls) == before
    assert (revoked_policy.version_reads, revoked_policy.generation_reads) == (1, 1)


@pytest.mark.asyncio
async def test_uncertain_commit_confirms_and_retryable_read_sleeps_once() -> None:
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    service, store, _ = _service(sleep=sleep)
    store.fail_next("read_lifecycle", CorpusLifecycleStoreFailure("retryable"))
    store.fail_next("commit_ready", CorpusLifecycleStoreFailure("commit_outcome_unknown"))
    receipt = await service.mark_ready(_ready_request("v1"), Policy())
    assert receipt.disposition == "confirmed"
    assert sleeps == [0.1]
    assert sum(call[0] == "commit_ready" for call in store.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["commit_ready", "switch", "remove"])
async def test_uncertain_success_confirms_every_mutation(operation: str) -> None:
    service, store, _ = _service()
    policy = Policy()
    if operation == "commit_ready":
        invoke: Callable[[], Awaitable[LifecycleMutationReceipt]] = partial(
            service.mark_ready, _ready_request("v1"), policy
        )
    elif operation == "switch":
        await service.mark_ready(_ready_request("v1"), policy)
        invoke = partial(service.switch_active, _switch("v1"), policy)
    else:
        await service.mark_ready(_ready_request("v1"), policy)
        invoke = partial(
            service.remove_version,
            RemoveCorpusVersionRequest(
                contract_version="1.0",
                corpus=_corpus("v1"),
                expected_lifecycle_revision=0,
            ),
        )
    boundary = len(store.calls)
    store.fail_next(operation, CorpusLifecycleStoreFailure("commit_outcome_unknown"))
    receipt = await invoke()
    assert receipt.disposition == "confirmed"
    assert sum(call[0] == operation for call in store.calls) == 1
    expected_traces = {
        "commit_ready": ["read_lifecycle", "commit_ready", "read_lifecycle"],
        "switch": ["read_active", "read_lifecycle", "switch", "read_active"],
        "remove": ["read_lifecycle", "read_active", "remove", "read_lifecycle"],
    }
    assert [call[0] for call in store.calls[boundary:]] == expected_traces[operation]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["commit_ready", "switch", "remove"])
async def test_uncertain_mismatched_confirmation_is_a_conflict(operation: str) -> None:
    service, store, _ = _service(max_retries=1)
    policy = Policy()
    if operation == "commit_ready":
        request = _ready_request("v1")

        async def corrupt_ready(
            expected_absent: bool,
            replacement: CorpusLifecycleRecord,
            audit: LifecycleAuditRecord,
            *,
            timeout_seconds: int,
        ) -> StoreMutationResult:
            del expected_absent, audit, timeout_seconds
            different = _record(replacement.corpus.corpus_version, marker="e")
            store.lifecycle[replacement.corpus] = LifecycleSnapshot(
                record=different, audit=_ready_audit(different)
            )
            store.calls.append(("commit_ready", replacement.corpus, True, 7))
            raise CorpusLifecycleStoreFailure("commit_outcome_unknown")

        store.commit_ready = corrupt_ready  # type: ignore[method-assign]
        invoke: Callable[[], Awaitable[LifecycleMutationReceipt]] = partial(
            service.mark_ready, request, policy
        )
    elif operation == "switch":
        await service.mark_ready(_ready_request("v1"), policy)

        async def corrupt_switch(
            expected: ActivePointerSnapshot,
            replacement: ActiveCorpusPointer,
            target_ready: CorpusLifecycleRecord,
            audit: LifecycleAuditRecord,
            *,
            timeout_seconds: int,
        ) -> StoreMutationResult:
            del expected, target_ready, audit, timeout_seconds
            store.active[replacement.corpus_id] = _pointer_snapshot(version="v2")
            store.calls.append(("switch", replacement.target, 7))
            raise CorpusLifecycleStoreFailure("commit_outcome_unknown")

        store.compare_and_swap_active = corrupt_switch  # type: ignore[method-assign]
        invoke = partial(service.switch_active, _switch("v1"), policy)
    else:
        await service.mark_ready(_ready_request("v1"), policy)

        async def corrupt_remove(
            expected_ready: LifecycleSnapshot,
            replacement: CorpusLifecycleRecord,
            expected_active: ActivePointerSnapshot,
            audit: LifecycleAuditRecord,
            *,
            timeout_seconds: int,
        ) -> StoreMutationResult:
            del expected_ready, expected_active, audit, timeout_seconds
            different_ready = _record(replacement.corpus.corpus_version, marker="e")
            different = different_ready.model_copy(
                update={"state": "logically_removed", "revision": 1}
            )
            store.lifecycle[replacement.corpus] = LifecycleSnapshot(
                record=different,
                audit=LifecycleAuditRecord(
                    schema_version="1.0",
                    contract_version="1.0",
                    action="remove",
                    subject=different.corpus,
                    before=None,
                    after=None,
                    resulting_revision=1,
                    authorizing_attestation_payload_sha256=(
                        different.attestation_payload_sha256
                    ),
                ),
            )
            store.calls.append(("remove", replacement.corpus, 7))
            raise CorpusLifecycleStoreFailure("commit_outcome_unknown")

        store.compare_and_remove = corrupt_remove  # type: ignore[method-assign]
        invoke = partial(
            service.remove_version,
            RemoveCorpusVersionRequest(
                contract_version="1.0",
                corpus=_corpus("v1"),
                expected_lifecycle_revision=0,
            ),
        )
    boundary = len(store.calls)
    with pytest.raises(CorpusLifecycleError) as caught:
        await invoke()
    assert caught.value.code == "lifecycle_conflict"
    expected_traces = {
        "commit_ready": ["read_lifecycle", "commit_ready", "read_lifecycle"],
        "switch": ["read_active", "read_lifecycle", "switch", "read_active"],
        "remove": ["read_lifecycle", "read_active", "remove", "read_lifecycle"],
    }
    assert [call[0] for call in store.calls[boundary:]] == expected_traces[operation]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["commit_ready", "switch", "remove"])
@pytest.mark.parametrize(
    "failure_code,expected,succeeds",
    [
        ("retryable", None, True),
        ("permanent", "store_unavailable", False),
        ("malformed", "malformed_store", False),
    ],
)
async def test_mutation_retry_and_nonretry_matrix(
    operation: str, failure_code: str, expected: str | None, succeeds: bool
) -> None:
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    service, store, _ = _service(sleep=sleep)
    policy = Policy()
    if operation == "commit_ready":
        invoke: Callable[[], Awaitable[LifecycleMutationReceipt]] = partial(
            service.mark_ready, _ready_request("v1"), policy
        )
    elif operation == "switch":
        await service.mark_ready(_ready_request("v1"), policy)
        invoke = partial(service.switch_active, _switch("v1"), policy)
    else:
        await service.mark_ready(_ready_request("v1"), policy)
        invoke = partial(
            service.remove_version,
            RemoveCorpusVersionRequest(
                contract_version="1.0",
                corpus=_corpus("v1"),
                expected_lifecycle_revision=0,
            ),
        )
    store.fail_before_next(
        operation, CorpusLifecycleStoreFailure(cast(Any, failure_code))
    )
    if succeeds:
        assert (await invoke()).disposition == "applied"
        assert sleeps == [0.1]
        assert sum(call[0] == operation for call in store.calls) == 2
    else:
        with pytest.raises(CorpusLifecycleError) as caught:
            await invoke()
        assert caught.value.code == expected
        assert sleeps == []
        assert sum(call[0] == operation for call in store.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["commit_ready", "switch", "remove"])
async def test_definite_conflict_confirms_once_without_retry_or_sleep(
    operation: str,
) -> None:
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    service, store, _ = _service(sleep=sleep)
    policy = Policy()
    if operation == "commit_ready":
        async def conflict_ready(
            expected_absent: bool,
            replacement: CorpusLifecycleRecord,
            audit: LifecycleAuditRecord,
            *,
            timeout_seconds: int,
        ) -> StoreMutationResult:
            del audit
            store.calls.append(
                ("commit_ready", replacement.corpus, expected_absent, timeout_seconds)
            )
            return "conflict"

        store.commit_ready = conflict_ready  # type: ignore[method-assign]
        invoke: Callable[[], Awaitable[LifecycleMutationReceipt]] = partial(
            service.mark_ready, _ready_request("v1"), policy
        )
    elif operation == "switch":
        await service.mark_ready(_ready_request("v1"), policy)

        async def conflict_switch(
            expected: ActivePointerSnapshot,
            replacement: ActiveCorpusPointer,
            target_ready: CorpusLifecycleRecord,
            audit: LifecycleAuditRecord,
            *,
            timeout_seconds: int,
        ) -> StoreMutationResult:
            del expected, target_ready, audit
            store.calls.append(("switch", replacement.target, timeout_seconds))
            return "conflict"

        store.compare_and_swap_active = conflict_switch  # type: ignore[method-assign]
        invoke = partial(service.switch_active, _switch("v1"), policy)
    else:
        await service.mark_ready(_ready_request("v1"), policy)

        async def conflict_remove(
            expected_ready: LifecycleSnapshot,
            replacement: CorpusLifecycleRecord,
            expected_active: ActivePointerSnapshot,
            audit: LifecycleAuditRecord,
            *,
            timeout_seconds: int,
        ) -> StoreMutationResult:
            del expected_ready, expected_active, audit
            store.calls.append(("remove", replacement.corpus, timeout_seconds))
            return "conflict"

        store.compare_and_remove = conflict_remove  # type: ignore[method-assign]
        invoke = partial(
            service.remove_version,
            RemoveCorpusVersionRequest(
                contract_version="1.0",
                corpus=_corpus("v1"),
                expected_lifecycle_revision=0,
            ),
        )
    boundary = len(store.calls)
    with pytest.raises(CorpusLifecycleError) as caught:
        await invoke()
    assert caught.value.code == "lifecycle_conflict"
    assert sleeps == []
    expected_traces = {
        "commit_ready": ["read_lifecycle", "commit_ready", "read_lifecycle"],
        "switch": ["read_active", "read_lifecycle", "switch", "read_active"],
        "remove": ["read_lifecycle", "read_active", "remove", "read_lifecycle"],
    }
    assert [call[0] for call in store.calls[boundary:]] == expected_traces[operation]


@pytest.mark.asyncio
async def test_resolve_active_is_registry_only_and_revocation_fails_closed() -> None:
    service, store, verifier = _service()
    policy = Policy()
    await service.mark_ready(_ready_request("v1"), policy)
    await service.switch_active(_switch("v1"), policy)
    verifier.calls.clear()
    resolved = await service.resolve_active_state("public-docs", policy)
    assert resolved.target == _corpus("v1")
    assert resolved.pointer_revision == 0
    assert verifier.calls == []
    mutations = len([call for call in store.calls if call[0] in {"switch", "remove"}])
    with pytest.raises(CorpusLifecycleError) as revoked:
        await service.resolve_active_state("public-docs", Policy(trusted=False))
    assert revoked.value.code == "attestation_untrusted"
    assert len([call for call in store.calls if call[0] in {"switch", "remove"}]) == mutations


@pytest.mark.asyncio
async def test_readiness_stays_on_one_lifecycle_snapshot_during_a_to_b_switch() -> None:
    class PausingVerifier(CandidateVerifier):
        def __init__(self) -> None:
            super().__init__()
            self.armed = False
            self.paused = False
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def __call__(
            self,
            corpus: ExactCorpusReference,
            identity: AttestationIdentity,
            verifier: AttestationVerifier,
        ) -> VerifiedCandidateEvidence:
            result = await super().__call__(corpus, identity, verifier)
            if self.armed and not self.paused and corpus == _corpus("v1"):
                self.paused = True
                self.entered.set()
                await self.release.wait()
            return result

    class VectorClient:
        def __init__(self, *, fails: bool) -> None:
            self.fails = fails
            self.readiness_calls = 0

        async def vector_get(
            self, request: FirestoreVectorQuery
        ) -> tuple[FirestoreVectorRow, ...]:
            del request
            raise AssertionError("readiness must not retrieve vectors")

        async def readiness_get(self, request: FirestoreReadinessQuery) -> None:
            del request
            self.readiness_calls += 1
            if self.fails:
                raise RuntimeError("B store unavailable")

        async def aclose(self) -> None:
            raise AssertionError("borrowed vector client must not close")

    class Factory:
        def __init__(self) -> None:
            self.clients: dict[str, VectorClient] = {}
            self.calls: list[str] = []

        def adapter_for(
            self,
            scope: ExactCorpusReference,
            *,
            embedding_identity: str,
            embedding_dimensions: int,
        ) -> ExactRetrievalAdapterBinding:
            assert embedding_identity == "fixture-embedding-v1"
            assert embedding_dimensions == 3
            self.calls.append(scope.corpus_version)
            client = self.clients.setdefault(
                scope.corpus_version,
                VectorClient(fails=scope.corpus_version == "v2"),
            )
            adapter = FirestoreRetrievalAdapter(
                client=client,
                embedding_function=lambda values: [[1.0, 2.0, 3.0] for _ in values],
                scope=scope,
                embedding_identity=embedding_identity,
                embedding_dimensions=embedding_dimensions,
                distance_measure="cosine",
                timeout_seconds=3,
                max_retries=0,
                owns_client=False,
            )
            return ExactRetrievalAdapterBinding(
                scope=scope,
                embedding_identity=embedding_identity,
                embedding_dimensions=embedding_dimensions,
                adapter=adapter,
            )

    candidate = PausingVerifier()
    service, _, _ = _service(verifier=candidate)
    policy = Policy()
    await service.mark_ready(_ready_request("v1"), policy)
    await service.mark_ready(_ready_request("v2"), policy)
    await service.switch_active(_switch("v1"), policy)

    factory = Factory()
    resolver = LifecycleRetrievalRouteResolver(
        corpus_id="public-docs",
        active_state_resolver=service,
        verify_attested_candidate=candidate,
        trust_policy_supplier=lambda: policy,
        expected_embedding_identity="fixture-embedding-v1",
        expected_embedding_dimensions=3,
        adapter_factory=factory,
    )
    evaluator = ReadinessEvaluator(
        database_probe=lambda: True,
        corpus_probe=lambda: True,
        local_vector_probe=lambda: (_ for _ in ()).throw(
            AssertionError("lifecycle readiness must not use local heartbeat")
        ),
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

    candidate.armed = True
    in_flight = asyncio.create_task(evaluator.evaluate())
    await asyncio.wait_for(candidate.entered.wait(), timeout=1)
    await service.switch_active(_switch("v2", expected=("v1", 0)), policy)
    candidate.release.set()
    first = await in_flight
    second = await evaluator.evaluate()

    assert (first.checks[1].state, first.checks[6].state) == ("ready", "ready")
    assert (second.checks[1].state, second.checks[6].state) == (
        "unknown",
        "unknown",
    )
    assert factory.calls == ["v1", "v2"]
    assert factory.clients["v1"].readiness_calls == 1
    assert factory.clients["v2"].readiness_calls == 1


@pytest.mark.asyncio
async def test_resolve_active_absent_removed_and_malformed_are_read_only() -> None:
    service, store, verifier = _service()
    with pytest.raises(CorpusLifecycleError) as absent:
        await service.resolve_active_state("public-docs", Policy())
    assert absent.value.code == "no_active_version"
    assert verifier.calls == []

    ready = _record()
    removed = ready.model_copy(update={"state": "logically_removed", "revision": 1})
    pointer = ActiveCorpusPointer(
        schema_version="1.0",
        contract_version="1.0",
        corpus_id="public-docs",
        target=ready.corpus,
        revision=0,
        target_lifecycle_revision=0,
        attestation_payload_sha256=ready.attestation_payload_sha256,
    )
    audit = LifecycleAuditRecord(
        schema_version="1.0",
        contract_version="1.0",
        action="promote",
        subject=None,
        before=None,
        after=ready.corpus,
        resulting_revision=0,
        authorizing_attestation_payload_sha256=ready.attestation_payload_sha256,
    )
    remove_audit = LifecycleAuditRecord(
        schema_version="1.0",
        contract_version="1.0",
        action="remove",
        subject=ready.corpus,
        before=None,
        after=None,
        resulting_revision=1,
        authorizing_attestation_payload_sha256=ready.attestation_payload_sha256,
    )
    malformed_values = (
        {
            "pointer": pointer,
            "audit": audit,
            "target_lifecycle": LifecycleSnapshot(
                record=removed, audit=remove_audit
            ),
        },
        {"pointer": pointer, "audit": None, "target_lifecycle": None},
    )
    for malformed_value in malformed_values:
        async def malformed_read(
            corpus_id: str, *, timeout_seconds: int, value: object = malformed_value
        ) -> Any:
            del corpus_id, timeout_seconds
            return value

        store.read_active_snapshot = malformed_read  # type: ignore[method-assign]
        with pytest.raises(CorpusLifecycleError) as malformed:
            await service.resolve_active_state("public-docs", Policy())
        assert malformed.value.code == "malformed_store"
    assert verifier.calls == []
    assert not any(call[0] in {"commit_ready", "switch", "remove"} for call in store.calls)


@pytest.mark.asyncio
async def test_owned_and_borrowed_cleanup_is_idempotent() -> None:
    owned_service, owned, _ = _service(owns_store=True)
    await owned_service.aclose()
    await owned_service.aclose()
    assert owned.close_calls == 1
    borrowed_service, borrowed, _ = _service()
    await borrowed_service.aclose()
    assert borrowed.close_calls == 0
    with pytest.raises(CorpusLifecycleError) as closed:
        await borrowed_service.resolve_active_state("public-docs", Policy())
    assert closed.value.code == "store_unavailable"

    candidate_store = CandidateMemoryStore()
    m8 = CandidateAttestationVerificationService(
        store=cast(Any, candidate_store),
        timeout_seconds=3,
        max_retries=1,
        sleep=asyncio.sleep,
        owns_store=True,
    )
    lifecycle_store = MemoryStore()
    composed = CorpusLifecycleService(
        store=lifecycle_store,
        verify_attested_candidate=m8.verify_attested_candidate,
        expected_embedding_identity="fixture-embedding-v1",
        expected_embedding_dimensions=3,
        timeout_seconds=3,
        max_retries=1,
        sleep=asyncio.sleep,
        owns_store=True,
    )
    await composed.aclose()
    assert lifecycle_store.close_calls == 1
    assert not any(call[0] == "close" for call in candidate_store.calls)
    await m8.aclose()
    await m8.aclose()
    assert [call[0] for call in candidate_store.calls].count("close") == 1


@pytest.mark.asyncio
async def test_cancellation_during_verify_sleep_and_close_propagates_unchanged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cancellation = asyncio.CancelledError("private-cancellation-canary")
    verify_events: list[str] = []
    verify_sleeps: list[float] = []

    async def unexpected_verify_sleep(delay: float) -> None:
        verify_sleeps.append(delay)

    async def cancel_verify(
        corpus: ExactCorpusReference,
        identity: AttestationIdentity,
        verifier: AttestationVerifier,
    ) -> VerifiedCandidateEvidence:
        del corpus, identity, verifier
        verify_events.append("verify")
        raise cancellation

    store = MemoryStore()
    service = CorpusLifecycleService(
        store=store,
        verify_attested_candidate=cancel_verify,
        expected_embedding_identity="fixture-embedding-v1",
        expected_embedding_dimensions=3,
        timeout_seconds=7,
        max_retries=1,
        sleep=unexpected_verify_sleep,
    )
    with pytest.raises(asyncio.CancelledError) as verify_cancelled:
        await service.mark_ready(_ready_request("v1"), Policy())
    assert verify_cancelled.value is cancellation
    assert verify_events == ["verify"]
    assert verify_sleeps == []
    assert store.calls == []
    assert store.close_calls == 0

    sleep_cancel = asyncio.CancelledError("private-sleep-canary")

    sleep_events: list[str] = []

    async def cancel_sleep(_: float) -> None:
        sleep_events.append("sleep")
        raise sleep_cancel

    retry_service, retry_store, _ = _service(sleep=cancel_sleep)
    retry_store.fail_next("read_lifecycle", CorpusLifecycleStoreFailure("retryable"))
    sleep_boundary = len(retry_store.calls)
    with pytest.raises(asyncio.CancelledError) as sleep_cancelled:
        await retry_service.mark_ready(_ready_request("v1"), Policy())
    assert sleep_cancelled.value is sleep_cancel
    assert sleep_events == ["sleep"]
    assert [call[0] for call in retry_store.calls[sleep_boundary:]] == [
        "read_lifecycle"
    ]
    assert retry_store.close_calls == 0

    close_cancel = asyncio.CancelledError("private-close-canary")

    close_events: list[str] = []

    async def cancelled_close() -> None:
        close_events.append("close")
        raise close_cancel

    close_store = MemoryStore()
    close_store.aclose = cancelled_close  # type: ignore[method-assign]
    close_service, _, _ = _service(close_store, owns_store=True)
    with pytest.raises(asyncio.CancelledError) as close_cancelled:
        await close_service.aclose()
    assert close_cancelled.value is close_cancel
    assert close_events == ["close"]
    assert close_store.calls == []
    assert caplog.records == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase", ["lifecycle_read", "active_read", "ready_tx", "switch_tx", "remove_tx"]
)
async def test_cancellation_at_every_service_store_phase_stops_work(
    phase: str, caplog: pytest.LogCaptureFixture
) -> None:
    cancellation = asyncio.CancelledError(f"PRIVATE-{phase}-CANCEL")
    events: list[str] = []
    sleeps: list[float] = []

    async def unexpected_sleep(delay: float) -> None:
        sleeps.append(delay)

    service, store, _ = _service(sleep=unexpected_sleep)
    policy = Policy()
    if phase != "lifecycle_read":
        await service.mark_ready(_ready_request("v1"), policy)

    async def cancel_lifecycle(
        corpus: ExactCorpusReference, *, timeout_seconds: int
    ) -> LifecycleSnapshot:
        del corpus, timeout_seconds
        events.append("lifecycle_read")
        raise cancellation

    async def cancel_active(
        corpus_id: str, *, timeout_seconds: int
    ) -> ActivePointerSnapshot:
        del corpus_id, timeout_seconds
        events.append("active_read")
        raise cancellation

    async def cancel_ready(*args: object, **kwargs: object) -> StoreMutationResult:
        del args, kwargs
        events.append(phase)
        raise cancellation

    if phase == "lifecycle_read":
        store.read_lifecycle_snapshot = cancel_lifecycle  # type: ignore[method-assign]
        invoke: Callable[[], Awaitable[LifecycleMutationReceipt]] = partial(
            service.mark_ready, _ready_request("v1"), policy
        )
    elif phase == "active_read":
        store.read_active_snapshot = cancel_active  # type: ignore[method-assign]
        invoke = partial(service.switch_active, _switch("v1"), policy)
    elif phase == "ready_tx":
        store.commit_ready = cancel_ready  # type: ignore[method-assign]
        invoke = partial(service.mark_ready, _ready_request("v2"), policy)
    elif phase == "switch_tx":
        store.compare_and_swap_active = cancel_ready  # type: ignore[method-assign]
        invoke = partial(service.switch_active, _switch("v1"), policy)
    else:
        store.compare_and_remove = cancel_ready  # type: ignore[method-assign]
        invoke = partial(
            service.remove_version,
            RemoveCorpusVersionRequest(
                contract_version="1.0",
                corpus=_corpus("v1"),
                expected_lifecycle_revision=0,
            ),
        )
    before = len(store.calls)
    with pytest.raises(asyncio.CancelledError) as caught:
        await invoke()
    assert caught.value is cancellation
    assert events == [phase]
    prerequisite_reads = {
        "lifecycle_read": [],
        "active_read": [],
        "ready_tx": ["read_lifecycle"],
        "switch_tx": ["read_active", "read_lifecycle"],
        "remove_tx": ["read_lifecycle", "read_active"],
    }
    assert [call[0] for call in store.calls[before:]] == prerequisite_reads[phase]
    assert sleeps == []
    assert store.close_calls == 0
    assert caplog.records == []


@pytest.mark.asyncio
async def test_cancellation_during_uncertain_confirmation_is_unchanged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cancellation = asyncio.CancelledError("PRIVATE-CONFIRM-CANCEL")
    service, store, _ = _service()
    original_read = store.read_lifecycle_snapshot
    reads = 0

    async def read_then_cancel(
        corpus: ExactCorpusReference, *, timeout_seconds: int
    ) -> LifecycleSnapshot:
        nonlocal reads
        reads += 1
        if reads == 2:
            raise cancellation
        return await original_read(corpus, timeout_seconds=timeout_seconds)

    store.read_lifecycle_snapshot = read_then_cancel  # type: ignore[method-assign]
    sleeps: list[float] = []

    async def unexpected_sleep(delay: float) -> None:
        sleeps.append(delay)

    service._sleep = unexpected_sleep
    boundary = len(store.calls)
    store.fail_next("commit_ready", CorpusLifecycleStoreFailure("commit_outcome_unknown"))
    with pytest.raises(asyncio.CancelledError) as caught:
        await service.mark_ready(_ready_request("v1"), Policy())
    assert caught.value is cancellation
    assert reads == 2
    assert sum(call[0] == "commit_ready" for call in store.calls) == 1
    assert [call[0] for call in store.calls[boundary:]] == [
        "read_lifecycle",
        "commit_ready",
    ]
    assert sleeps == []
    assert store.close_calls == 0
    assert caplog.records == []


def test_rebuilds_are_serialized_for_all_public_models() -> None:
    models = (
        VerifiedLifecycleEvidence,
        CorpusLifecycleRecord,
        ActiveCorpusPointer,
        LifecycleAuditRecord,
        MarkReadyRequest,
        ExpectedActivePointer,
        SwitchActiveRequest,
        RemoveCorpusVersionRequest,
        LifecycleMutationReceipt,
        LifecycleSnapshot,
        ActivePointerSnapshot,
        ResolvedActiveState,
    )
    for model in models:
        adapter = TypeAdapter(model)
        barrier = Barrier(2)

        def rebuild(
            selected_model: type[Any] = model,
            selected_barrier: Barrier = barrier,
        ) -> None:
            selected_barrier.wait()
            for _ in range(5):
                assert selected_model.model_rebuild(force=True) is True

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (executor.submit(rebuild), executor.submit(rebuild))
            for future in futures:
                future.result()
        canary = "PRIVATE-REBUILD-CANARY"
        for surface in (
            partial(model.model_validate_json, "{" + canary),
            partial(adapter.validate_json, "{" + canary),
            partial(TypeAdapter(model).validate_json, "{" + canary),
        ):
            with pytest.raises(CorpusLifecycleError) as caught:
                surface()
            _assert_error_is_content_free(caught.value, canary)


def _pointer_snapshot(
    *,
    version: str = "v1",
    revision: int = 0,
    action: str = "promote",
    before: ExactCorpusReference | None = None,
) -> ActivePointerSnapshot:
    record = _record(version)
    pointer = ActiveCorpusPointer(
        schema_version="1.0",
        contract_version="1.0",
        corpus_id="public-docs",
        target=record.corpus,
        revision=revision,
        target_lifecycle_revision=0,
        attestation_payload_sha256=record.attestation_payload_sha256,
    )
    audit = LifecycleAuditRecord(
        schema_version="1.0",
        contract_version="1.0",
        action=cast(Any, action),
        subject=None,
        before=before,
        after=record.corpus,
        resulting_revision=revision,
        authorizing_attestation_payload_sha256=record.attestation_payload_sha256,
    )
    return ActivePointerSnapshot(
        pointer=pointer,
        audit=audit,
        target_lifecycle=LifecycleSnapshot(record=record, audit=_ready_audit(record)),
    )


@pytest.mark.parametrize(
    "revision,action,before",
    [
        (0, "rollback", None),
        (0, "promote", _corpus("foreign")),
        (1, "promote", None),
        (1, "rollback", None),
        (1, "rollback", _corpus("v1")),
    ],
)
def test_active_snapshot_rejects_non_exact_audit_history(
    revision: int, action: str, before: ExactCorpusReference | None
) -> None:
    with pytest.raises(CorpusLifecycleError):
        _pointer_snapshot(
            version="v1", revision=revision, action=action, before=before
        )


@pytest.mark.parametrize(
    "action,revision",
    [("ready", 1), ("remove", 0), ("rollback", 0)],
)
def test_receipt_revision_shape_is_strict(action: str, revision: int) -> None:
    with pytest.raises(CorpusLifecycleError):
        LifecycleMutationReceipt(
            contract_version="1.0",
            action=cast(Any, action),
            disposition="confirmed",
            subject=_corpus() if action in {"ready", "remove"} else None,
            target=None if action in {"ready", "remove"} else _corpus(),
            resulting_revision=revision,
        )


@pytest.mark.asyncio
async def test_switch_replay_reverifies_candidate_and_exact_audit() -> None:
    service, store, verifier = _service()
    policy = Policy()
    await service.mark_ready(_ready_request("v1"), policy)
    await service.switch_active(_switch("v1"), policy)
    verifier.failures["v1"] = CandidatePersistenceError("attestation_failed")
    with pytest.raises(CorpusLifecycleError) as unavailable:
        await service.switch_active(_switch("v1"), policy)
    assert unavailable.value.code == "candidate_unavailable"

    verifier.failures.clear()
    valid = store.active["public-docs"]
    bad_audit = cast(LifecycleAuditRecord, valid.audit).model_copy(
        update={"authorizing_attestation_payload_sha256": "e" * 64}
    )
    object.__setattr__(valid, "audit", bad_audit)
    with pytest.raises(CorpusLifecycleError) as malformed:
        await service.switch_active(_switch("v1"), policy)
    assert malformed.value.code == "malformed_store"


@pytest.mark.asyncio
async def test_m8_evidence_requires_exact_model_and_selected_signer_identity() -> None:
    service, store, verifier = _service()
    forged = _candidate_evidence().model_copy(update={"signing_key_id": "other-key"})
    verifier.overrides["v1"] = forged
    with pytest.raises(CorpusLifecycleError) as signer_error:
        await service.mark_ready(_ready_request("v1"), Policy())
    assert signer_error.value.code == "malformed_store"
    assert store.calls == []

    class Lookalike:
        pass

    lookalike = Lookalike()
    for name, value in _candidate_evidence().model_dump(round_trip=True).items():
        setattr(lookalike, name, value)

    async def return_lookalike(*_: object) -> Any:
        return lookalike

    lookalike_service = CorpusLifecycleService(
        store=store,
        verify_attested_candidate=cast(Any, return_lookalike),
        expected_embedding_identity="fixture-embedding-v1",
        expected_embedding_dimensions=3,
        timeout_seconds=7,
        max_retries=0,
        sleep=asyncio.sleep,
    )
    with pytest.raises(CorpusLifecycleError) as type_error:
        await lookalike_service.mark_ready(_ready_request("v1"), Policy())
    assert type_error.value.code == "malformed_store"
    assert store.calls == []


@pytest.mark.asyncio
async def test_m8_evidence_with_hidden_extra_is_rejected_content_free() -> None:
    service, store, verifier = _service()
    forged = _candidate_evidence()
    object.__setattr__(
        forged,
        "__pydantic_extra__",
        {"private-extra-canary": "private-extra-canary"},
    )
    verifier.overrides["v1"] = forged
    with pytest.raises(CorpusLifecycleError) as caught:
        await service.mark_ready(_ready_request("v1"), Policy())
    assert caught.value.code == "malformed_store"
    _assert_error_is_content_free(caught.value, "private-extra-canary")
    assert store.calls == []


@pytest.mark.asyncio
async def test_removed_replay_still_rejects_active_target() -> None:
    service, store, _ = _service()
    policy = Policy()
    await service.mark_ready(_ready_request("v1"), policy)
    await service.switch_active(_switch("v1"), policy)
    ready_snapshot = store.lifecycle[_corpus("v1")]
    ready = cast(CorpusLifecycleRecord, ready_snapshot.record)
    removed = ready.model_copy(update={"state": "logically_removed", "revision": 1})
    remove_audit = LifecycleAuditRecord(
        schema_version="1.0",
        contract_version="1.0",
        action="remove",
        subject=ready.corpus,
        before=None,
        after=None,
        resulting_revision=1,
        authorizing_attestation_payload_sha256=ready.attestation_payload_sha256,
    )
    store.lifecycle[ready.corpus] = LifecycleSnapshot(
        record=removed, audit=remove_audit
    )
    request = RemoveCorpusVersionRequest(
        contract_version="1.0",
        corpus=ready.corpus,
        expected_lifecycle_revision=0,
    )
    with pytest.raises(CorpusLifecycleError) as caught:
        await service.remove_version(request)
    assert caught.value.code == "active_version_forbidden"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["commit_ready", "switch", "remove"])
@pytest.mark.parametrize("max_retries", [0, 1])
async def test_unconfirmed_unknown_commit_is_store_unavailable(
    operation: str, max_retries: int
) -> None:
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    service, store, _ = _service(max_retries=max_retries, sleep=sleep)
    policy = Policy()
    if operation == "commit_ready":
        ready_request = _ready_request("v1")
        invoke: Callable[[], Awaitable[LifecycleMutationReceipt]] = partial(
            service.mark_ready, ready_request, policy
        )
    elif operation == "switch":
        await service.mark_ready(_ready_request("v1"), policy)
        switch_request = _switch("v1")
        invoke = partial(service.switch_active, switch_request, policy)
    else:
        await service.mark_ready(_ready_request("v1"), policy)
        remove_request = RemoveCorpusVersionRequest(
            contract_version="1.0",
            corpus=_corpus("v1"),
            expected_lifecycle_revision=0,
        )
        invoke = partial(service.remove_version, remove_request)
    boundary = len(store.calls)
    for _ in range(max_retries + 1):
        store.fail_before_next(
            operation, CorpusLifecycleStoreFailure("commit_outcome_unknown")
        )
    with pytest.raises(CorpusLifecycleError) as caught:
        await invoke()
    assert caught.value.code == "store_unavailable"
    assert sleeps == ([0.1] if max_retries else [])
    prefixes = {
        "commit_ready": ["read_lifecycle"],
        "switch": ["read_active", "read_lifecycle"],
        "remove": ["read_lifecycle", "read_active"],
    }
    confirm = "read_active" if operation == "switch" else "read_lifecycle"
    expected_calls = prefixes[operation] + [operation, confirm] * (max_retries + 1)
    assert [call[0] for call in store.calls[boundary:]] == expected_calls


@pytest.mark.asyncio
async def test_foreign_scope_snapshots_are_malformed_before_further_work() -> None:
    service, store, verifier = _service()
    policy = Policy()
    foreign_record = _record("foreign")
    foreign_lifecycle = LifecycleSnapshot(
        record=foreign_record,
        audit=_ready_audit(foreign_record),
    )

    async def foreign_lifecycle_read(
        corpus: ExactCorpusReference, *, timeout_seconds: int
    ) -> LifecycleSnapshot:
        del corpus, timeout_seconds
        return foreign_lifecycle

    store.read_lifecycle_snapshot = foreign_lifecycle_read  # type: ignore[method-assign]
    with pytest.raises(CorpusLifecycleError) as lifecycle_error:
        await service.mark_ready(_ready_request("v1"), policy)
    assert lifecycle_error.value.code == "malformed_store"

    other_corpus = ExactCorpusReference(
        kind="exact", corpus_id="other-docs", corpus_version="foreign"
    )
    other_record = _record("foreign").model_copy(update={"corpus": other_corpus})
    other_ready = LifecycleSnapshot(
        record=other_record,
        audit=_ready_audit(other_record),
    )
    other_pointer = ActiveCorpusPointer(
        schema_version="1.0",
        contract_version="1.0",
        corpus_id="other-docs",
        target=other_corpus,
        revision=0,
        target_lifecycle_revision=0,
        attestation_payload_sha256=other_record.attestation_payload_sha256,
    )
    other_audit = LifecycleAuditRecord(
        schema_version="1.0",
        contract_version="1.0",
        action="promote",
        subject=None,
        before=None,
        after=other_corpus,
        resulting_revision=0,
        authorizing_attestation_payload_sha256=other_record.attestation_payload_sha256,
    )
    foreign_active = ActivePointerSnapshot(
        pointer=other_pointer,
        audit=other_audit,
        target_lifecycle=other_ready,
    )

    async def foreign_active_read(
        corpus_id: str, *, timeout_seconds: int
    ) -> ActivePointerSnapshot:
        del corpus_id, timeout_seconds
        return foreign_active

    store.read_active_snapshot = foreign_active_read  # type: ignore[method-assign]
    verifier.calls.clear()
    with pytest.raises(CorpusLifecycleError) as active_error:
        await service.resolve_active_state("public-docs", policy)
    assert active_error.value.code == "malformed_store"
    assert verifier.calls == []


@pytest.mark.asyncio
async def test_lone_and_mismatched_store_state_fails_without_repair() -> None:
    service, store, verifier = _service()
    record = _record()
    malformed_values = (
        {"record": record, "audit": None},
        {
            "record": record,
            "audit": _ready_audit(_record("v2")),
        },
        {"record": None, "audit": _ready_audit(record)},
    )
    for value in malformed_values:
        async def malformed_read(
            corpus: ExactCorpusReference, *, timeout_seconds: int, raw: object = value
        ) -> Any:
            del corpus, timeout_seconds
            return raw

        store.read_lifecycle_snapshot = malformed_read  # type: ignore[method-assign]
        before = len(store.calls)
        with pytest.raises(CorpusLifecycleError) as caught:
            await service.mark_ready(_ready_request("v1"), Policy())
        assert caught.value.code == "malformed_store"
        assert not any(
            call[0] in {"commit_ready", "switch", "remove"}
            for call in store.calls[before:]
        )
    assert len(verifier.calls) == len(malformed_values)


@pytest.mark.asyncio
async def test_different_corpora_have_disjoint_state_reads_writes_and_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = MemoryStore()

    async def exact_verification(
        corpus: ExactCorpusReference,
        identity: AttestationIdentity,
        verifier: AttestationVerifier,
    ) -> VerifiedCandidateEvidence:
        del identity, verifier
        return _candidate_evidence(corpus.corpus_version).model_copy(
            update={"corpus": corpus}
        )

    service = CorpusLifecycleService(
        store=store,
        verify_attested_candidate=exact_verification,
        expected_embedding_identity="fixture-embedding-v1",
        expected_embedding_dimensions=3,
        timeout_seconds=7,
        max_retries=1,
        sleep=asyncio.sleep,
    )
    corpus_a = _corpus("v1")
    corpus_b = ExactCorpusReference(
        kind="exact", corpus_id="other-docs", corpus_version="v1"
    )
    for corpus in (corpus_a, corpus_b):
        ready_boundary = len(store.calls)
        await service.mark_ready(
            MarkReadyRequest(
                contract_version="1.0",
                corpus=corpus,
                trusted_identity=_identity(),
            ),
            Policy(),
        )
        assert store.calls[ready_boundary:] == [
            ("read_lifecycle", corpus, 7),
            ("commit_ready", corpus, True, 7),
        ]
        switch_boundary = len(store.calls)
        await service.switch_active(
            SwitchActiveRequest(
                contract_version="1.0",
                action="promote",
                target=corpus,
                expected=None,
            ),
            Policy(),
        )
        assert store.calls[switch_boundary:] == [
            ("read_active", corpus.corpus_id, 7),
            ("read_lifecycle", corpus, 7),
            ("switch", corpus, 7),
        ]
    assert set(store.active) == {"public-docs", "other-docs"}
    assert lifecycle_record_key(corpus_a) != lifecycle_record_key(corpus_b)
    assert active_pointer_key(corpus_a.corpus_id) != active_pointer_key(corpus_b.corpus_id)
    store.fail_next("read_active", CorpusLifecycleStoreFailure("permanent"))
    failed_boundary = len(store.calls)
    with pytest.raises(CorpusLifecycleError):
        await service.resolve_active_state("public-docs", Policy())
    assert store.calls[failed_boundary:] == [("read_active", "public-docs", 7)]
    resolved_boundary = len(store.calls)
    resolved_b = await service.resolve_active_state("other-docs", Policy())
    assert resolved_b.target == corpus_b
    assert store.calls[resolved_boundary:] == [("read_active", "other-docs", 7)]
    assert "public-docs" not in repr(service.__dict__)
    assert "other-docs" not in repr(service.__dict__)
    assert "public-docs" not in caplog.text
    assert "other-docs" not in caplog.text
    assert caplog.records == []


def test_invalid_revisions_actions_and_relationships_fail_closed() -> None:
    with pytest.raises(CorpusLifecycleError):
        CorpusLifecycleRecord(
            **_lifecycle_evidence().model_dump(round_trip=True),
            schema_version="1.0",
            contract_version="1.0",
            state="ready",
            revision=1,
        )
    with pytest.raises(CorpusLifecycleError):
        _switch("v1", action="rollback")
    with pytest.raises(CorpusLifecycleError):
        ExpectedActivePointer(target=_corpus(), revision=True)
    with pytest.raises(CorpusLifecycleError):
        _switch("v2", expected=("v1", MAX_SAFE_INTEGER))
    with pytest.raises(CorpusLifecycleError):
        LifecycleSnapshot(record=_record(), audit=None)


def test_trust_policy_protocol_shape_is_consumable() -> None:
    policy: AttestationTrustPolicy = Policy()
    assert policy.policy_version == "fixture-policy-v1"
    assert policy.policy_generation == 1
    assert isinstance(policy.verifier_for(_identity()), Verifier)
