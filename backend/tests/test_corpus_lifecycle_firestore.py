import asyncio
import importlib
import socket
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.corpus_lifecycle import (
    ActiveCorpusPointer,
    ActivePointerSnapshot,
    CorpusLifecycleRecord,
    CorpusLifecycleStoreFailure,
    LifecycleAuditRecord,
    LifecycleSnapshot,
    VerifiedLifecycleEvidence,
    lifecycle_audit_key,
    lifecycle_record_key,
)
from app.corpus_lifecycle_firestore import (
    ACTIVE_COLLECTION,
    AUDIT_COLLECTION,
    LIFECYCLE_COLLECTION,
    FirestoreSdkCorpusLifecycleStore,
    create_corpus_lifecycle_store,
)
from app.ingest.candidate_persistence import AttestationIdentity
from app.retrieval_contracts import ExactCorpusReference


def _forbidden(*_: object, **__: object) -> Any:
    raise AssertionError("external call forbidden in lifecycle Firestore tests")


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


class Snapshot:
    def __init__(
        self,
        key: str,
        value: Mapping[str, object] | None,
        *,
        exists: object | None = None,
    ) -> None:
        self.id = key
        self.exists = value is not None if exists is None else exists
        self._value = value

    def to_dict(self) -> Mapping[str, object] | None:
        return self._value


class Reference:
    def __init__(self, client: "Client", collection: str, key: str) -> None:
        self.client = client
        self.collection = collection
        self.id = key


class Collection:
    def __init__(self, client: "Client", name: str) -> None:
        self.client = client
        self.name = name

    def document(self, key: str) -> Reference:
        self.client.calls.append(("document", self.name, key))
        return Reference(self.client, self.name, key)


class FakeAlreadyExists(Exception):
    pass


class FakeAborted(Exception):
    pass


class FakeDeadlineExceeded(Exception):
    pass


class FakeServiceUnavailable(Exception):
    pass


class FakePermissionDenied(Exception):
    pass


class Transaction:
    def __init__(self, client: "Client") -> None:
        self.client = client
        self._client = client
        self._id: bytes | None = None
        self.reads: dict[tuple[str, str], int] = {}
        self.writes: list[tuple[str, Reference, Mapping[str, object]]] = []

    @property
    def in_progress(self) -> bool:
        return self._id is not None

    @property
    def _write_pbs(self) -> list[tuple[str, Reference, Mapping[str, object]]]:
        return self.writes

    def _options_protobuf(self, retry_id: bytes | None) -> Mapping[str, object]:
        return {"retry_id": retry_id}

    def _clean_up(self) -> None:
        self._id = None

    async def get(
        self, reference: Reference, *, retry: None, timeout: int
    ) -> object:
        if not self.in_progress:
            raise AssertionError("transaction must be begun before read")
        self.client.calls.append(
            ("transaction_get", reference.collection, reference.id, retry, timeout)
        )
        if self.client.get_failure is not None:
            raise self.client.get_failure
        identity = (reference.collection, reference.id)
        self.reads[identity] = self.client.revisions.get(identity, 0)
        malformed = self.client.malformed_exists.get(identity)
        snapshot = Snapshot(
            reference.id, self.client.records.get(identity), exists=malformed
        )

        async def snapshots() -> Any:
            yield snapshot

        return snapshots()

    def create(self, reference: Reference, value: Mapping[str, object]) -> None:
        self.client.calls.append(("create", reference.collection, reference.id))
        self.writes.append(("create", reference, dict(value)))

    def set(self, reference: Reference, value: Mapping[str, object]) -> None:
        self.client.calls.append(("set", reference.collection, reference.id))
        self.writes.append(("set", reference, dict(value)))

class Client:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], Mapping[str, object]] = {}
        self.revisions: dict[tuple[str, str], int] = {}
        self.calls: list[tuple[object, ...]] = []
        self.malformed_exists: dict[tuple[str, str], object] = {}
        self.begin_failure: BaseException | None = None
        self.get_failure: BaseException | None = None
        self.commit_failure: BaseException | None = None
        self.rollback_failure: BaseException | None = None
        self.close_calls = 0
        self.lock = asyncio.Lock()
        self._database_string = "projects/fixture/databases/(default)"
        self._rpc_metadata = (("fixture", "metadata"),)
        self._firestore_api = Api(self)
        self.last_transaction: Transaction | None = None

    def collection(self, name: str) -> Collection:
        self.calls.append(("collection", name))
        return Collection(self, name)

    async def get_all(
        self,
        references: list[Reference],
        *,
        transaction: Transaction,
        retry: None,
        timeout: int,
    ) -> Any:
        assert transaction.in_progress
        for reference in references:
            self.calls.append(
                ("transaction_get", reference.collection, reference.id, retry, timeout)
            )
            if self.get_failure is not None:
                raise self.get_failure
            identity = (reference.collection, reference.id)
            transaction.reads[identity] = self.revisions.get(identity, 0)
            malformed = self.malformed_exists.get(identity)
            yield Snapshot(
                reference.id, self.records.get(identity), exists=malformed
            )

    def transaction(self, *, max_attempts: int) -> Transaction:
        self.calls.append(("transaction", max_attempts))
        transaction = Transaction(self)
        self.last_transaction = transaction
        return transaction

    def close(self) -> None:
        self.close_calls += 1


class Api:
    def __init__(self, client: Client) -> None:
        self.client = client

    async def begin_transaction(
        self,
        *,
        request: Mapping[str, object],
        metadata: object,
        retry: None,
        timeout: int,
    ) -> object:
        transaction = cast(Transaction, self.client.last_transaction)
        assert transaction._id is None
        self.client.calls.append(
            ("transaction_begin", request, metadata, retry, timeout)
        )
        if self.client.begin_failure is not None:
            raise self.client.begin_failure
        return SimpleNamespace(transaction=b"fixture-transaction-id")

    async def commit(
        self,
        *,
        request: Mapping[str, object],
        metadata: object,
        retry: None,
        timeout: int,
    ) -> object:
        transaction = cast(Transaction, self.client.last_transaction)
        assert request["transaction"] == transaction._id
        assert request["writes"] is transaction._write_pbs
        self.client.calls.append(("transaction_commit", retry, timeout))
        if self.client.commit_failure is not None:
            failure = self.client.commit_failure
            self.client.commit_failure = None
            raise failure
        async with self.client.lock:
            if any(
                self.client.revisions.get(identity, 0) != revision
                for identity, revision in transaction.reads.items()
            ):
                raise FakeAborted("race")
            for operation, reference, value in transaction.writes:
                identity = (reference.collection, reference.id)
                if operation == "create" and identity in self.client.records:
                    raise FakeAlreadyExists("exists")
                self.client.records[identity] = value
                self.client.revisions[identity] = (
                    self.client.revisions.get(identity, 0) + 1
                )
        return SimpleNamespace(write_results=(), commit_time=None)

    async def rollback(
        self,
        *,
        request: Mapping[str, object],
        metadata: object,
        retry: None,
        timeout: int,
    ) -> object:
        transaction = cast(Transaction, self.client.last_transaction)
        assert request["transaction"] == transaction._id
        self.client.calls.append(("transaction_rollback", retry, timeout))
        if self.client.rollback_failure is not None:
            raise self.client.rollback_failure
        return SimpleNamespace()


class FakeExceptions:
    AlreadyExists = FakeAlreadyExists
    Aborted = FakeAborted
    DeadlineExceeded = FakeDeadlineExceeded
    ServiceUnavailable = FakeServiceUnavailable
    PermissionDenied = FakePermissionDenied


class FakeSdk:
    class AsyncClient(Client):
        def __init__(self, *, project: str, database: str) -> None:
            del project, database
            super().__init__()


def _corpus(version: str = "v1") -> ExactCorpusReference:
    return ExactCorpusReference(
        kind="exact", corpus_id="public-docs", corpus_version=version
    )


def _identity() -> AttestationIdentity:
    return AttestationIdentity(algorithm_id="fixture-ed25519-v1", key_id="fixture-key-1")


def _evidence(version: str = "v1") -> VerifiedLifecycleEvidence:
    identity = _identity()
    return VerifiedLifecycleEvidence(
        corpus=_corpus(version),
        plan_sha256="a" * 64,
        semantic_manifest_sha256="b" * 64,
        embedding_identity="fixture-embedding-v1",
        embedding_dimensions=3,
        document_count=2,
        chunk_count=3,
        inventory_sha256="c" * 64,
        attestation_payload_sha256="d" * 64,
        signature_algorithm_id=identity.algorithm_id,
        signing_key_id=identity.key_id,
    )


def _record(version: str = "v1") -> CorpusLifecycleRecord:
    return CorpusLifecycleRecord.model_validate(
        {
            **_evidence(version).model_dump(round_trip=True),
            "schema_version": "1.0",
            "contract_version": "1.0",
            "state": "ready",
            "revision": 0,
        }
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


def _store(client: Client, *, owns_client: bool = False) -> FirestoreSdkCorpusLifecycleStore:
    return FirestoreSdkCorpusLifecycleStore(
        client,
        exceptions=FakeExceptions,
        owns_client=owns_client,
    )


def _put(client: Client, collection: str, key: str, value: object) -> None:
    assert hasattr(value, "model_dump")
    client.records[(collection, key)] = cast(Any, value).model_dump(mode="json")
    client.revisions[(collection, key)] = 1


def _assert_content_free(error: BaseException, canary: str) -> None:
    rendered = repr((error, error.args, vars(error), error.__cause__, error.__context__))
    assert canary not in rendered


@pytest.mark.asyncio
async def test_absent_reads_are_exact_bounded_transactions() -> None:
    client = Client()
    store = _store(client)
    lifecycle = await store.read_lifecycle_snapshot(_corpus(), timeout_seconds=7)
    active = await store.read_active_snapshot("public-docs", timeout_seconds=7)
    assert lifecycle == LifecycleSnapshot(record=None, audit=None)
    assert active == ActivePointerSnapshot(
        pointer=None, audit=None, target_lifecycle=None
    )
    assert client.calls.count(("transaction", 1)) == 2
    gets = [call for call in client.calls if call[0] == "transaction_get"]
    assert all(call[-2:] == (None, 7) for call in gets)
    assert {call[1] for call in gets} <= {
        LIFECYCLE_COLLECTION,
        ACTIVE_COLLECTION,
        AUDIT_COLLECTION,
    }
    assert not any(call[0] in {"list", "delete", "update"} for call in client.calls)
    assert [call[0] for call in client.calls].count("transaction_begin") == 2
    assert [call[0] for call in client.calls].count("transaction_rollback") == 2
    assert all(
        call[-2:] == (None, 7)
        for call in client.calls
        if call[0] in {"transaction_begin", "transaction_get", "transaction_rollback"}
    )


@pytest.mark.asyncio
async def test_ready_and_removed_snapshot_require_complete_audit_history() -> None:
    client = Client()
    store = _store(client)
    ready = _record()
    ready_audit = _ready_audit(ready)
    _put(client, LIFECYCLE_COLLECTION, lifecycle_record_key(ready.corpus), ready)
    _put(client, AUDIT_COLLECTION, lifecycle_audit_key(ready.corpus, 0), ready_audit)
    assert await store.read_lifecycle_snapshot(
        ready.corpus, timeout_seconds=3
    ) == LifecycleSnapshot(record=ready, audit=ready_audit)

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
    _put(client, LIFECYCLE_COLLECTION, lifecycle_record_key(ready.corpus), removed)
    _put(client, AUDIT_COLLECTION, lifecycle_audit_key(ready.corpus, 1), remove_audit)
    assert await store.read_lifecycle_snapshot(
        ready.corpus, timeout_seconds=3
    ) == LifecycleSnapshot(record=removed, audit=remove_audit)

    del client.records[(AUDIT_COLLECTION, lifecycle_audit_key(ready.corpus, 0))]
    with pytest.raises(CorpusLifecycleStoreFailure) as orphan:
        await store.read_lifecycle_snapshot(ready.corpus, timeout_seconds=3)
    assert orphan.value.code == "malformed"


@pytest.mark.asyncio
async def test_orphan_audits_and_non_boolean_exists_fail_malformed() -> None:
    client = Client()
    store = _store(client)
    record = _record()
    _put(client, AUDIT_COLLECTION, lifecycle_audit_key(record.corpus, 0), _ready_audit(record))
    with pytest.raises(CorpusLifecycleStoreFailure) as orphan:
        await store.read_lifecycle_snapshot(record.corpus, timeout_seconds=3)
    assert orphan.value.code == "malformed"

    client.records.clear()
    key = (LIFECYCLE_COLLECTION, lifecycle_record_key(record.corpus))
    client.malformed_exists[key] = "false-private-canary"
    with pytest.raises(CorpusLifecycleStoreFailure) as malformed:
        await store.read_lifecycle_snapshot(record.corpus, timeout_seconds=3)
    assert malformed.value.code == "malformed"
    _assert_content_free(malformed.value, "private-canary")


@pytest.mark.asyncio
async def test_commit_ready_is_atomic_create_only_and_conflicts() -> None:
    client = Client()
    store = _store(client)
    record = _record()
    audit = _ready_audit(record)
    assert await store.commit_ready(
        True, record, audit, timeout_seconds=5
    ) == "applied"
    assert await store.commit_ready(
        True, record, audit, timeout_seconds=5
    ) == "conflict"
    assert client.records[(LIFECYCLE_COLLECTION, lifecycle_record_key(record.corpus))]
    assert client.records[(AUDIT_COLLECTION, lifecycle_audit_key(record.corpus, 0))]
    assert all(
        call[-2:] == (None, 5)
        for call in client.calls
        if call[0]
        in {
            "transaction_begin",
            "transaction_get",
            "transaction_commit",
            "transaction_rollback",
        }
    )

    mismatched_audit = _ready_audit(_record("v2"))
    calls_before = len(client.calls)
    with pytest.raises(CorpusLifecycleStoreFailure) as malformed:
        await store.commit_ready(
            True, record, mismatched_audit, timeout_seconds=5
        )
    assert malformed.value.code == "malformed"
    assert len(client.calls) == calls_before


@pytest.mark.asyncio
async def test_active_cas_and_remove_reread_every_precondition_atomically() -> None:
    client = Client()
    store = _store(client)
    record = _record()
    ready_audit = _ready_audit(record)
    assert await store.commit_ready(True, record, ready_audit, timeout_seconds=5) == "applied"
    absent = ActivePointerSnapshot(pointer=None, audit=None, target_lifecycle=None)
    pointer = ActiveCorpusPointer(
        schema_version="1.0",
        contract_version="1.0",
        corpus_id="public-docs",
        target=record.corpus,
        revision=0,
        target_lifecycle_revision=0,
        attestation_payload_sha256=record.attestation_payload_sha256,
    )
    promote = LifecycleAuditRecord(
        schema_version="1.0",
        contract_version="1.0",
        action="promote",
        subject=None,
        before=None,
        after=record.corpus,
        resulting_revision=0,
        authorizing_attestation_payload_sha256=record.attestation_payload_sha256,
    )
    assert await store.compare_and_swap_active(
        absent, pointer, record, promote, timeout_seconds=5
    ) == "applied"
    active = await store.read_active_snapshot("public-docs", timeout_seconds=5)
    assert active.pointer == pointer
    assert active.audit == promote
    assert active.target_lifecycle == LifecycleSnapshot(
        record=record, audit=ready_audit
    )

    removed = record.model_copy(update={"state": "logically_removed", "revision": 1})
    remove = LifecycleAuditRecord(
        schema_version="1.0",
        contract_version="1.0",
        action="remove",
        subject=record.corpus,
        before=None,
        after=None,
        resulting_revision=1,
        authorizing_attestation_payload_sha256=record.attestation_payload_sha256,
    )
    expected_ready = LifecycleSnapshot(record=record, audit=ready_audit)
    assert await store.compare_and_remove(
        expected_ready, removed, active, remove, timeout_seconds=5
    ) == "conflict"


@pytest.mark.asyncio
async def test_active_cas_rejects_non_exact_transition_before_transaction() -> None:
    client = Client()
    store = _store(client)
    first = _record("v1")
    target = _record("v2")
    absent = ActivePointerSnapshot(pointer=None, audit=None, target_lifecycle=None)

    def pointer(revision: int) -> ActiveCorpusPointer:
        return ActiveCorpusPointer(
            schema_version="1.0",
            contract_version="1.0",
            corpus_id="public-docs",
            target=target.corpus,
            revision=revision,
            target_lifecycle_revision=0,
            attestation_payload_sha256=target.attestation_payload_sha256,
        )

    def audit(before: ExactCorpusReference, revision: int) -> LifecycleAuditRecord:
        return LifecycleAuditRecord(
            schema_version="1.0",
            contract_version="1.0",
            action="promote",
            subject=None,
            before=before,
            after=target.corpus,
            resulting_revision=revision,
            authorizing_attestation_payload_sha256=target.attestation_payload_sha256,
        )

    with pytest.raises(CorpusLifecycleStoreFailure) as absent_jump:
        await store.compare_and_swap_active(
            absent, pointer(1), target, audit(first.corpus, 1), timeout_seconds=5
        )
    assert absent_jump.value.code == "malformed"

    existing_pointer = ActiveCorpusPointer(
        schema_version="1.0",
        contract_version="1.0",
        corpus_id="public-docs",
        target=first.corpus,
        revision=0,
        target_lifecycle_revision=0,
        attestation_payload_sha256=first.attestation_payload_sha256,
    )
    existing_audit = LifecycleAuditRecord(
        schema_version="1.0",
        contract_version="1.0",
        action="promote",
        subject=None,
        before=None,
        after=first.corpus,
        resulting_revision=0,
        authorizing_attestation_payload_sha256=first.attestation_payload_sha256,
    )
    existing = ActivePointerSnapshot(
        pointer=existing_pointer,
        audit=existing_audit,
        target_lifecycle=LifecycleSnapshot(
            record=first, audit=_ready_audit(first)
        ),
    )
    for replacement, transition_audit in (
        (pointer(2), audit(first.corpus, 2)),
        (pointer(1), audit(_corpus("v3"), 1)),
    ):
        with pytest.raises(CorpusLifecycleStoreFailure) as malformed:
            await store.compare_and_swap_active(
                existing,
                replacement,
                target,
                transition_audit,
                timeout_seconds=5,
            )
        assert malformed.value.code == "malformed"
    assert not any(call[0] == "transaction" for call in client.calls)


@pytest.mark.asyncio
async def test_inactive_remove_updates_state_and_creates_audit_without_delete() -> None:
    client = Client()
    store = _store(client)
    record = _record()
    ready_audit = _ready_audit(record)
    await store.commit_ready(True, record, ready_audit, timeout_seconds=5)
    absent = ActivePointerSnapshot(pointer=None, audit=None, target_lifecycle=None)
    removed = record.model_copy(update={"state": "logically_removed", "revision": 1})
    remove = LifecycleAuditRecord(
        schema_version="1.0",
        contract_version="1.0",
        action="remove",
        subject=record.corpus,
        before=None,
        after=None,
        resulting_revision=1,
        authorizing_attestation_payload_sha256=record.attestation_payload_sha256,
    )
    expected = LifecycleSnapshot(record=record, audit=ready_audit)
    assert await store.compare_and_remove(
        expected, removed, absent, remove, timeout_seconds=5
    ) == "applied"
    assert not any(call[0] == "delete" for call in client.calls)
    snapshot = await store.read_lifecycle_snapshot(record.corpus, timeout_seconds=5)
    assert cast(CorpusLifecycleRecord, snapshot.record).state == "logically_removed"

    mismatched_remove = remove.model_copy(
        update={"authorizing_attestation_payload_sha256": "e" * 64}
    )
    calls_before = len(client.calls)
    with pytest.raises(CorpusLifecycleStoreFailure) as malformed:
        await store.compare_and_remove(
            expected,
            removed,
            absent,
            mismatched_remove,
            timeout_seconds=5,
        )
    assert malformed.value.code == "malformed"
    assert len(client.calls) == calls_before


@pytest.mark.asyncio
async def test_transport_classification_and_cancellation_are_content_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.corpus_lifecycle_firestore.importlib.import_module",
        lambda _: FakeExceptions,
    )
    cases = (
        (FakeServiceUnavailable("private-transient"), "retryable"),
        (FakeDeadlineExceeded("private-timeout"), "retryable"),
        (FakePermissionDenied("private-permanent"), "permanent"),
    )
    for failure, code in cases:
        client = Client()
        client.get_failure = failure
        with pytest.raises(CorpusLifecycleStoreFailure) as caught:
            await _store(client).read_lifecycle_snapshot(_corpus(), timeout_seconds=3)
        assert caught.value.code == code
        _assert_content_free(caught.value, "private")

    cancellation = asyncio.CancelledError("private-cancel")
    client = Client()
    client.get_failure = cancellation
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await _store(client).read_lifecycle_snapshot(_corpus(), timeout_seconds=3)
    assert cancelled.value is cancellation

    commit_client = Client()
    commit_client.commit_failure = FakeDeadlineExceeded("private-commit")
    record = _record()
    with pytest.raises(CorpusLifecycleStoreFailure) as uncertain:
        await _store(commit_client).commit_ready(
            True, record, _ready_audit(record), timeout_seconds=3
        )
    assert uncertain.value.code == "commit_outcome_unknown"
    _assert_content_free(uncertain.value, "private-commit")


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["begin", "get", "commit", "rollback"])
async def test_cancellation_at_every_transaction_phase_is_unchanged(
    phase: str,
) -> None:
    cancellation = asyncio.CancelledError(f"private-{phase}-cancel")
    client = Client()
    if phase == "begin":
        client.begin_failure = cancellation
    elif phase == "get":
        client.get_failure = cancellation
    elif phase == "rollback":
        client.rollback_failure = cancellation
    else:
        client.commit_failure = cancellation
    store = _store(client)
    with pytest.raises(asyncio.CancelledError) as caught:
        if phase == "commit":
            record = _record()
            await store.commit_ready(
                True, record, _ready_audit(record), timeout_seconds=3
            )
        else:
            await store.read_lifecycle_snapshot(_corpus(), timeout_seconds=3)
    assert caught.value is cancellation
    calls = [call[0] for call in client.calls]
    if phase in {"begin", "get", "commit"}:
        assert "transaction_rollback" not in calls


@pytest.mark.asyncio
async def test_owned_and_borrowed_client_cleanup() -> None:
    owned_client = Client()
    owned = _store(owned_client, owns_client=True)
    await owned.aclose()
    await owned.aclose()
    assert owned_client.close_calls == 1
    borrowed_client = Client()
    await _store(borrowed_client).aclose()
    assert borrowed_client.close_calls == 0


def test_factory_is_construction_only_and_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def module(name: str) -> object:
        calls.append(name)
        return FakeSdk

    monkeypatch.setattr(importlib, "import_module", module)
    store = create_corpus_lifecycle_store("fixture-project")
    assert isinstance(store, FirestoreSdkCorpusLifecycleStore)
    assert calls == ["google.cloud.firestore_v1", "google.api_core.exceptions"]
    assert not any(
        call[0] == "transaction" for call in cast(Any, store)._client.calls
    )


def test_collection_names_are_fixed() -> None:
    assert (
        LIFECYCLE_COLLECTION,
        ACTIVE_COLLECTION,
        AUDIT_COLLECTION,
    ) == (
        "cairn_corpus_lifecycle_v1",
        "cairn_corpus_active_v1",
        "cairn_corpus_lifecycle_audit_v1",
    )
