"""Optional Firestore transaction adapter for the corpus lifecycle registry."""

import asyncio
import importlib
import inspect
from collections.abc import Mapping, Sequence
from typing import Any, cast

from pydantic import BaseModel

from app.corpus_lifecycle import (
    ActiveCorpusPointer,
    ActivePointerSnapshot,
    CorpusLifecycleRecord,
    CorpusLifecycleStoreFailure,
    LifecycleAuditRecord,
    LifecycleSnapshot,
    StoreMutationResult,
    active_audit_key,
    active_pointer_key,
    lifecycle_audit_key,
    lifecycle_record_key,
)
from app.retrieval_contracts import MAX_SAFE_INTEGER, ExactCorpusReference
from app.retrieval_firestore import FIRESTORE_DATABASE_ID

LIFECYCLE_COLLECTION = "cairn_corpus_lifecycle_v1"
ACTIVE_COLLECTION = "cairn_corpus_active_v1"
AUDIT_COLLECTION = "cairn_corpus_lifecycle_audit_v1"


def _mapping(value: object) -> dict[str, object]:
    if not hasattr(value, "model_dump"):
        raise CorpusLifecycleStoreFailure("malformed") from None
    result: object | None = None
    try:
        result = cast(Any, value).model_dump(mode="json")
    except Exception:
        pass
    if type(result) is not dict:
        raise CorpusLifecycleStoreFailure("malformed") from None
    return cast(dict[str, object], result)


def _parse_model[T: BaseModel](model: type[T], value: object) -> T:
    parsed: T | None = None
    try:
        parsed = cast(Any, model).model_validate(value)
    except Exception:
        pass
    if parsed is None:
        raise CorpusLifecycleStoreFailure("malformed") from None
    return parsed


class FirestoreSdkCorpusLifecycleStore:
    """Exact-key Firestore adapter with application-owned transaction policy."""

    def __init__(
        self,
        client: object,
        *,
        exceptions: object,
        owns_client: bool = False,
    ) -> None:
        if type(owns_client) is not bool:
            raise CorpusLifecycleStoreFailure("malformed") from None
        self._client = client
        self._exceptions = exceptions
        self._owns_client = owns_client
        self._closed = False
        self._close_lock = asyncio.Lock()

    def _reference(self, collection: str, key: str) -> Any:
        return cast(Any, self._client).collection(collection).document(key)

    def _transaction(self) -> Any:
        return cast(Any, self._client).transaction(max_attempts=1)

    async def _begin(self, transaction: Any, timeout_seconds: int) -> None:
        client = cast(Any, transaction)._client
        response = await client._firestore_api.begin_transaction(
            request={
                "database": client._database_string,
                "options": transaction._options_protobuf(None),
            },
            metadata=client._rpc_metadata,
            retry=None,
            timeout=timeout_seconds,
        )
        transaction_id = cast(Any, response).transaction
        if type(transaction_id) is not bytes or not transaction_id:
            raise CorpusLifecycleStoreFailure("malformed") from None
        transaction._id = transaction_id

    def _clean_up(self, transaction: Any) -> None:
        try:
            transaction._clean_up()
        except Exception:
            pass

    def _in_progress(self, transaction: Any) -> bool:
        try:
            return cast(Any, transaction).in_progress is True
        except Exception:
            return False

    async def _rollback(self, transaction: Any, timeout_seconds: int) -> None:
        if not self._in_progress(transaction):
            self._clean_up(transaction)
            return
        client = cast(Any, transaction)._client
        transaction_id = cast(Any, transaction)._id
        try:
            await client._firestore_api.rollback(
                request={
                    "database": client._database_string,
                    "transaction": transaction_id,
                },
                metadata=client._rpc_metadata,
                retry=None,
                timeout=timeout_seconds,
            )
        finally:
            self._clean_up(transaction)

    async def _finish_without_commit(
        self, transaction: Any, timeout_seconds: int
    ) -> CorpusLifecycleStoreFailure | None:
        failure: CorpusLifecycleStoreFailure | None = None
        try:
            await self._rollback(transaction, timeout_seconds)
        except asyncio.CancelledError:
            raise
        except CorpusLifecycleStoreFailure as error:
            failure = CorpusLifecycleStoreFailure(error.code)
        except Exception as error:
            failure = self._normalized(error, during_commit=False)
        return failure

    async def _get(self, transaction: Any, reference: Any, timeout_seconds: int) -> object:
        result = cast(Any, transaction)._client.get_all(
            [reference],
            transaction=transaction,
            retry=None,
            timeout=timeout_seconds,
        )
        if inspect.isawaitable(result):
            result = await result
        if hasattr(result, "__aiter__"):
            items = [item async for item in result]
            if len(items) != 1:
                raise CorpusLifecycleStoreFailure("malformed") from None
            return items[0]
        raise CorpusLifecycleStoreFailure("malformed") from None

    def _snapshot(
        self,
        snapshot: object,
        *,
        expected_key: str,
    ) -> Mapping[str, object] | None:
        value: object = None
        failed = False
        try:
            exists = cast(Any, snapshot).exists
            key = cast(Any, snapshot).id
            if type(exists) is not bool or type(key) is not str or key != expected_key:
                raise ValueError
            if not exists:
                return None
            value = cast(Any, snapshot).to_dict()
            if type(value) is not dict:
                raise ValueError
        except Exception:
            failed = True
        if failed:
            raise CorpusLifecycleStoreFailure("malformed") from None
        return cast(Mapping[str, object], value)

    async def _read_value(
        self,
        transaction: Any,
        collection: str,
        key: str,
        timeout_seconds: int,
    ) -> Mapping[str, object] | None:
        snapshot = await self._get(
            transaction,
            self._reference(collection, key),
            timeout_seconds,
        )
        return self._snapshot(snapshot, expected_key=key)

    async def _read_lifecycle_in_transaction(
        self,
        transaction: Any,
        corpus: ExactCorpusReference,
        timeout_seconds: int,
    ) -> LifecycleSnapshot:
        record_key = lifecycle_record_key(corpus)
        audit_zero_key = lifecycle_audit_key(corpus, 0)
        audit_one_key = lifecycle_audit_key(corpus, 1)
        raw_record = await self._read_value(
            transaction, LIFECYCLE_COLLECTION, record_key, timeout_seconds
        )
        raw_audit_zero = await self._read_value(
            transaction, AUDIT_COLLECTION, audit_zero_key, timeout_seconds
        )
        raw_audit_one = await self._read_value(
            transaction, AUDIT_COLLECTION, audit_one_key, timeout_seconds
        )
        if raw_record is None:
            if raw_audit_zero is not None or raw_audit_one is not None:
                raise CorpusLifecycleStoreFailure("malformed") from None
            return LifecycleSnapshot(record=None, audit=None)
        record = _parse_model(CorpusLifecycleRecord, raw_record)
        if record.corpus != corpus:
            raise CorpusLifecycleStoreFailure("malformed") from None
        if record.state == "ready":
            if raw_audit_zero is None or raw_audit_one is not None:
                raise CorpusLifecycleStoreFailure("malformed") from None
            audit = _parse_model(LifecycleAuditRecord, raw_audit_zero)
            return _parse_model(
                LifecycleSnapshot,
                {"record": record, "audit": audit},
            )
        if raw_audit_zero is None or raw_audit_one is None:
            raise CorpusLifecycleStoreFailure("malformed") from None
        historical = record.model_copy(update={"state": "ready", "revision": 0})
        _parse_model(
            LifecycleSnapshot,
            {
                "record": historical,
                "audit": _parse_model(LifecycleAuditRecord, raw_audit_zero),
            },
        )
        return _parse_model(
            LifecycleSnapshot,
            {
                "record": record,
                "audit": _parse_model(LifecycleAuditRecord, raw_audit_one),
            },
        )

    async def _read_active_in_transaction(
        self,
        transaction: Any,
        corpus_id: str,
        timeout_seconds: int,
    ) -> ActivePointerSnapshot:
        pointer_key = active_pointer_key(corpus_id)
        raw_pointer = await self._read_value(
            transaction, ACTIVE_COLLECTION, pointer_key, timeout_seconds
        )
        if raw_pointer is None:
            orphan = await self._read_value(
                transaction,
                AUDIT_COLLECTION,
                active_audit_key(corpus_id, 0),
                timeout_seconds,
            )
            if orphan is not None:
                raise CorpusLifecycleStoreFailure("malformed") from None
            return ActivePointerSnapshot(
                pointer=None,
                audit=None,
                target_lifecycle=None,
            )
        pointer = _parse_model(ActiveCorpusPointer, raw_pointer)
        if pointer.corpus_id != corpus_id:
            raise CorpusLifecycleStoreFailure("malformed") from None
        raw_audit = await self._read_value(
            transaction,
            AUDIT_COLLECTION,
            active_audit_key(corpus_id, pointer.revision),
            timeout_seconds,
        )
        if raw_audit is None:
            raise CorpusLifecycleStoreFailure("malformed") from None
        audit = _parse_model(LifecycleAuditRecord, raw_audit)
        if pointer.revision > 0:
            raw_first_audit = await self._read_value(
                transaction,
                AUDIT_COLLECTION,
                active_audit_key(corpus_id, 0),
                timeout_seconds,
            )
            if raw_first_audit is None:
                raise CorpusLifecycleStoreFailure("malformed") from None
            first_audit = _parse_model(LifecycleAuditRecord, raw_first_audit)
            if (
                first_audit.action != "promote"
                or first_audit.before is not None
                or first_audit.after is None
                or first_audit.after.corpus_id != corpus_id
                or first_audit.resulting_revision != 0
            ):
                raise CorpusLifecycleStoreFailure("malformed") from None
        target = await self._read_lifecycle_in_transaction(
            transaction,
            pointer.target,
            timeout_seconds,
        )
        return _parse_model(
            ActivePointerSnapshot,
            {"pointer": pointer, "audit": audit, "target_lifecycle": target},
        )

    async def read_lifecycle_snapshot(
        self,
        corpus: ExactCorpusReference,
        *,
        timeout_seconds: int,
    ) -> LifecycleSnapshot:
        transaction = self._transaction()
        result: LifecycleSnapshot | None = None
        failure: CorpusLifecycleStoreFailure | None = None
        try:
            await self._begin(transaction, timeout_seconds)
            result = await self._read_lifecycle_in_transaction(
                transaction, corpus, timeout_seconds
            )
        except asyncio.CancelledError:
            self._clean_up(transaction)
            raise
        except CorpusLifecycleStoreFailure as error:
            failure = CorpusLifecycleStoreFailure(error.code)
        except Exception as error:
            failure = self._normalized(error, during_commit=False)
        cleanup_failure = await self._finish_without_commit(transaction, timeout_seconds)
        if failure is None:
            failure = cleanup_failure
        if failure is not None:
            raise failure from None
        if result is None:
            raise CorpusLifecycleStoreFailure("malformed") from None
        return result
    async def read_active_snapshot(
        self,
        corpus_id: str,
        *,
        timeout_seconds: int,
    ) -> ActivePointerSnapshot:
        transaction = self._transaction()
        result: ActivePointerSnapshot | None = None
        failure: CorpusLifecycleStoreFailure | None = None
        try:
            await self._begin(transaction, timeout_seconds)
            result = await self._read_active_in_transaction(
                transaction, corpus_id, timeout_seconds
            )
        except asyncio.CancelledError:
            self._clean_up(transaction)
            raise
        except CorpusLifecycleStoreFailure as error:
            failure = CorpusLifecycleStoreFailure(error.code)
        except Exception as error:
            failure = self._normalized(error, during_commit=False)
        cleanup_failure = await self._finish_without_commit(transaction, timeout_seconds)
        if failure is None:
            failure = cleanup_failure
        if failure is not None:
            raise failure from None
        if result is None:
            raise CorpusLifecycleStoreFailure("malformed") from None
        return result

    async def _commit(self, transaction: Any, timeout_seconds: int) -> None:
        failure: CorpusLifecycleStoreFailure | None = None
        try:
            client = cast(Any, transaction)._client
            response = await client._firestore_api.commit(
                request={
                    "database": client._database_string,
                    "writes": transaction._write_pbs,
                    "transaction": transaction._id,
                },
                metadata=client._rpc_metadata,
                retry=None,
                timeout=timeout_seconds,
            )
            write_results = cast(Any, response).write_results
            if not isinstance(write_results, Sequence):
                raise CorpusLifecycleStoreFailure("malformed") from None
            transaction.write_results = list(write_results)
            transaction.commit_time = cast(Any, response).commit_time
        except asyncio.CancelledError:
            self._clean_up(transaction)
            raise
        except Exception as error:
            if isinstance(error, CorpusLifecycleStoreFailure):
                failure = CorpusLifecycleStoreFailure(error.code)
                conflict = False
            elif self._is_conflict(error):
                conflict = True
            else:
                conflict = False
                failure = self._normalized(error, during_commit=True)
        else:
            conflict = False
        self._clean_up(transaction)
        if conflict:
            raise _TransactionConflict() from None
        if failure is not None:
            raise failure from None

    async def commit_ready(
        self,
        expected_absent: bool,
        replacement: CorpusLifecycleRecord,
        audit: LifecycleAuditRecord,
        *,
        timeout_seconds: int,
    ) -> StoreMutationResult:
        if expected_absent is not True:
            raise CorpusLifecycleStoreFailure("malformed") from None
        replacement = _parse_model(CorpusLifecycleRecord, _mapping(replacement))
        audit = _parse_model(LifecycleAuditRecord, _mapping(audit))
        _parse_model(
            LifecycleSnapshot,
            {"record": replacement, "audit": audit},
        )
        if (
            replacement.state != "ready"
            or replacement.revision != 0
        ):
            raise CorpusLifecycleStoreFailure("malformed") from None
        transaction = self._transaction()
        result: StoreMutationResult | None = None
        failure: CorpusLifecycleStoreFailure | None = None
        try:
            await self._begin(transaction, timeout_seconds)
            current = await self._read_lifecycle_in_transaction(
                transaction, replacement.corpus, timeout_seconds
            )
            if current.record is not None:
                result = "conflict"
            else:
                transaction.create(
                    self._reference(
                        LIFECYCLE_COLLECTION,
                        lifecycle_record_key(replacement.corpus),
                    ),
                    _mapping(replacement),
                )
                transaction.create(
                    self._reference(
                        AUDIT_COLLECTION,
                        lifecycle_audit_key(replacement.corpus, 0),
                    ),
                    _mapping(audit),
                )
                await self._commit(transaction, timeout_seconds)
                result = "applied"
        except asyncio.CancelledError:
            self._clean_up(transaction)
            raise
        except _TransactionConflict:
            result = "conflict"
        except CorpusLifecycleStoreFailure as error:
            failure = CorpusLifecycleStoreFailure(error.code)
        except Exception as error:
            failure = self._normalized(error, during_commit=False)
        cleanup_failure = await self._finish_without_commit(transaction, timeout_seconds)
        if failure is None:
            failure = cleanup_failure
        if failure is not None:
            raise failure from None
        if result is None:
            raise CorpusLifecycleStoreFailure("malformed") from None
        return result

    async def compare_and_swap_active(
        self,
        expected: ActivePointerSnapshot,
        replacement: ActiveCorpusPointer,
        target_ready: CorpusLifecycleRecord,
        audit: LifecycleAuditRecord,
        *,
        timeout_seconds: int,
    ) -> StoreMutationResult:
        expected = _parse_model(ActivePointerSnapshot, _mapping(expected))
        replacement = _parse_model(ActiveCorpusPointer, _mapping(replacement))
        target_ready = _parse_model(CorpusLifecycleRecord, _mapping(target_ready))
        audit = _parse_model(LifecycleAuditRecord, _mapping(audit))
        intended = _parse_model(
            ActivePointerSnapshot,
            {
                "pointer": replacement,
                "audit": audit,
                "target_lifecycle": {
                    "record": target_ready,
                    "audit": {
                        "schema_version": "1.0",
                        "contract_version": "1.0",
                        "action": "ready",
                        "subject": target_ready.corpus,
                        "before": None,
                        "after": None,
                        "resulting_revision": 0,
                        "authorizing_attestation_payload_sha256": (
                            target_ready.attestation_payload_sha256
                        ),
                    },
                },
            },
        )
        expected_pointer = expected.pointer
        if expected_pointer is None:
            valid_transition = (
                replacement.revision == 0
                and audit.action == "promote"
                and audit.before is None
            )
        else:
            valid_transition = (
                expected_pointer.corpus_id == replacement.corpus_id
                and expected_pointer.revision < MAX_SAFE_INTEGER
                and replacement.revision == expected_pointer.revision + 1
                and replacement.target != expected_pointer.target
                and audit.before == expected_pointer.target
            )
        if (
            not valid_transition
            or replacement.target != target_ready.corpus
            or replacement.attestation_payload_sha256
            != target_ready.attestation_payload_sha256
            or audit.after != replacement.target
            or audit.resulting_revision != replacement.revision
            or audit.authorizing_attestation_payload_sha256
            != replacement.attestation_payload_sha256
        ):
            raise CorpusLifecycleStoreFailure("malformed") from None
        transaction = self._transaction()
        result: StoreMutationResult | None = None
        failure: CorpusLifecycleStoreFailure | None = None
        try:
            await self._begin(transaction, timeout_seconds)
            current = await self._read_active_in_transaction(
                transaction, replacement.corpus_id, timeout_seconds
            )
            durable_target = await self._read_lifecycle_in_transaction(
                transaction, replacement.target, timeout_seconds
            )
            if current != expected or durable_target != intended.target_lifecycle:
                result = "conflict"
            else:
                reference = self._reference(
                    ACTIVE_COLLECTION, active_pointer_key(replacement.corpus_id)
                )
                if current.pointer is None:
                    transaction.create(reference, _mapping(replacement))
                else:
                    transaction.set(reference, _mapping(replacement))
                transaction.create(
                    self._reference(
                        AUDIT_COLLECTION,
                        active_audit_key(replacement.corpus_id, replacement.revision),
                    ),
                    _mapping(audit),
                )
                await self._commit(transaction, timeout_seconds)
                result = "applied"
        except asyncio.CancelledError:
            self._clean_up(transaction)
            raise
        except _TransactionConflict:
            result = "conflict"
        except CorpusLifecycleStoreFailure as error:
            failure = CorpusLifecycleStoreFailure(error.code)
        except Exception as error:
            failure = self._normalized(error, during_commit=False)
        cleanup_failure = await self._finish_without_commit(transaction, timeout_seconds)
        if failure is None:
            failure = cleanup_failure
        if failure is not None:
            raise failure from None
        if result is None:
            raise CorpusLifecycleStoreFailure("malformed") from None
        return result

    async def compare_and_remove(
        self,
        expected_ready: LifecycleSnapshot,
        replacement: CorpusLifecycleRecord,
        expected_active: ActivePointerSnapshot,
        audit: LifecycleAuditRecord,
        *,
        timeout_seconds: int,
    ) -> StoreMutationResult:
        expected_ready = _parse_model(LifecycleSnapshot, _mapping(expected_ready))
        replacement = _parse_model(CorpusLifecycleRecord, _mapping(replacement))
        expected_active = _parse_model(ActivePointerSnapshot, _mapping(expected_active))
        audit = _parse_model(LifecycleAuditRecord, _mapping(audit))
        _parse_model(
            LifecycleSnapshot,
            {"record": replacement, "audit": audit},
        )
        ready_record = expected_ready.record
        if (
            ready_record is None
            or ready_record.state != "ready"
            or ready_record.revision != 0
            or ready_record.corpus != replacement.corpus
            or replacement.state != "logically_removed"
            or replacement.revision != 1
            or replacement.evidence != ready_record.evidence
            or (
                expected_active.pointer is not None
                and expected_active.pointer.corpus_id != replacement.corpus.corpus_id
            )
        ):
            raise CorpusLifecycleStoreFailure("malformed") from None
        transaction = self._transaction()
        result: StoreMutationResult | None = None
        failure: CorpusLifecycleStoreFailure | None = None
        try:
            await self._begin(transaction, timeout_seconds)
            current = await self._read_lifecycle_in_transaction(
                transaction, replacement.corpus, timeout_seconds
            )
            active = await self._read_active_in_transaction(
                transaction, replacement.corpus.corpus_id, timeout_seconds
            )
            if current != expected_ready or active != expected_active:
                result = "conflict"
            elif (
                active.pointer is not None
                and active.pointer.target == replacement.corpus
            ):
                result = "conflict"
            else:
                transaction.set(
                    self._reference(
                        LIFECYCLE_COLLECTION,
                        lifecycle_record_key(replacement.corpus),
                    ),
                    _mapping(replacement),
                )
                transaction.create(
                    self._reference(
                        AUDIT_COLLECTION,
                        lifecycle_audit_key(replacement.corpus, 1),
                    ),
                    _mapping(audit),
                )
                await self._commit(transaction, timeout_seconds)
                result = "applied"
        except asyncio.CancelledError:
            self._clean_up(transaction)
            raise
        except _TransactionConflict:
            result = "conflict"
        except CorpusLifecycleStoreFailure as error:
            failure = CorpusLifecycleStoreFailure(error.code)
        except Exception as error:
            failure = self._normalized(error, during_commit=False)
        cleanup_failure = await self._finish_without_commit(transaction, timeout_seconds)
        if failure is None:
            failure = cleanup_failure
        if failure is not None:
            raise failure from None
        if result is None:
            raise CorpusLifecycleStoreFailure("malformed") from None
        return result

    def _exception_types(
        self,
    ) -> tuple[
        tuple[type[BaseException], ...],
        tuple[type[BaseException], ...],
        tuple[type[BaseException], ...],
    ]:
        exceptions_obj: Any = self._exceptions
        already_exists = (exceptions_obj.AlreadyExists,)
        aborted = (exceptions_obj.Aborted,)
        deadline = (exceptions_obj.DeadlineExceeded,)
        unavailable = (exceptions_obj.ServiceUnavailable,)
        return already_exists + aborted, deadline, unavailable

    def _is_conflict(self, error: Exception) -> bool:
        conflict, _, _ = self._exception_types()
        return isinstance(error, conflict)

    def _normalized(self, error: Exception, *, during_commit: bool) -> CorpusLifecycleStoreFailure:
        _, deadline, unavailable = self._exception_types()
        if during_commit and isinstance(error, deadline + unavailable):
            return CorpusLifecycleStoreFailure("commit_outcome_unknown")
        if isinstance(error, deadline + unavailable):
            return CorpusLifecycleStoreFailure("retryable")
        return CorpusLifecycleStoreFailure("permanent")

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            if self._owns_client:
                try:
                    result = cast(Any, self._client).close()
                    if inspect.isawaitable(result):
                        await result
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass


class _TransactionConflict(Exception):
    pass


def create_corpus_lifecycle_store(project_id: str) -> FirestoreSdkCorpusLifecycleStore:
    """Construct an owned SDK adapter without performing a request."""
    try:
        sdk = importlib.import_module("google.cloud.firestore_v1")
        exceptions = importlib.import_module("google.api_core.exceptions")
    except ModuleNotFoundError:
        raise RuntimeError(
            "Corpus lifecycle requires the optional 'firestore' dependency profile"
        ) from None
    client = sdk.AsyncClient(project=project_id, database=FIRESTORE_DATABASE_ID)
    return FirestoreSdkCorpusLifecycleStore(
        client,
        exceptions=exceptions,
        owns_client=True,
    )
