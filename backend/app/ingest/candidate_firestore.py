"""Optional Firestore transport for immutable candidate persistence.

SDK objects are translated at this boundary.  Constructing the wrapper performs no
request; Cairn does not construct it during application startup in this milestone.
"""

import asyncio
import hashlib
import importlib
import inspect
import math
from collections.abc import Mapping
from typing import Any, cast

from app.ingest.candidate_persistence import (
    ATTESTATION_COLLECTION,
    CANDIDATE_COLLECTION,
    CANDIDATE_READ_LOOKAHEAD_SIZE,
    CANDIDATE_READ_PAGE_SIZE,
    DOCUMENT_COLLECTION,
    FIRESTORE_DOCUMENT_MAX_BYTES,
    CandidateRecordKind,
    CandidateStoreFailure,
    CandidateStorePage,
    CandidateStoreRecord,
    StrictStoreValue,
    _CandidateStoreMalformed,
)
from app.retrieval_contracts import ExactCorpusReference
from app.retrieval_firestore import FIRESTORE_CHUNKS_COLLECTION, FIRESTORE_DATABASE_ID

_COLLECTIONS: dict[CandidateRecordKind, str] = {
    "candidate": CANDIDATE_COLLECTION,
    "document": DOCUMENT_COLLECTION,
    "chunk": FIRESTORE_CHUNKS_COLLECTION,
    "attestation": ATTESTATION_COLLECTION,
}


def _plain(value: StrictStoreValue) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_plain(item) for item in value)
    return value


class FirestoreSdkCandidateStore:
    """Create-only Firestore store with exact local protobuf size accounting."""

    def __init__(self, client: object, *, sdk: object, helpers: object) -> None:
        self._client = client
        self._sdk = sdk
        self._helpers = helpers
        self._closed = False

    def _reference(self, kind: CandidateRecordKind, key: str) -> Any:
        return cast(Any, self._client).collection(_COLLECTIONS[kind]).document(key)

    def _write_value(self, record: CandidateStoreRecord) -> dict[str, object]:
        value = cast(dict[str, object], _plain(record.value))
        if record.kind == "chunk":
            embedding = record.value.get("embedding")
            if not isinstance(embedding, tuple) or any(
                type(item) is not float or not math.isfinite(item) for item in embedding
            ):
                raise CandidateStoreFailure("permanent") from None
            value["embedding"] = cast(Any, self._sdk).vector.Vector(list(embedding))
        return value

    def _writes(
        self, kind: CandidateRecordKind, records: tuple[CandidateStoreRecord, ...]
    ) -> tuple[Any, ...]:
        writes: list[Any] = []
        for record in records:
            if record.kind != kind:
                raise CandidateStoreFailure("permanent") from None
            reference = self._reference(kind, record.key)
            generated = cast(Any, self._helpers).pbs_for_create(
                reference._document_path, self._write_value(record)
            )
            if len(generated) != 1:
                raise CandidateStoreFailure("permanent") from None
            writes.append(generated[0])
        return tuple(writes)

    def _commit_size(self, writes: tuple[Any, ...]) -> int:
        request = cast(Any, self._sdk).types.CommitRequest(
            database=cast(Any, self._client)._database_string,
            writes=writes,
        )
        return cast(int, request._pb.ByteSize())

    def _write_sha256(self, write: Any) -> str:
        material = cast(Any, write)._pb.SerializeToString()
        if type(material) is not bytes:
            raise _CandidateStoreMalformed() from None
        return hashlib.sha256(material).hexdigest()

    def encoded_document_size(self, record: CandidateStoreRecord) -> int:
        return self.encoded_record_sizes(record)[0]

    def encoded_create_size(
        self, kind: CandidateRecordKind, records: tuple[CandidateStoreRecord, ...]
    ) -> int:
        try:
            return self._commit_size(self._writes(kind, records))
        except (_CandidateStoreMalformed, CandidateStoreFailure):
            raise
        except Exception:
            raise CandidateStoreFailure("permanent") from None

    def encoded_create_base_size(self, kind: CandidateRecordKind) -> int:
        if kind not in _COLLECTIONS:
            raise CandidateStoreFailure("permanent") from None
        try:
            return self._commit_size(())
        except Exception:
            raise CandidateStoreFailure("permanent") from None

    def encoded_record_sizes(self, record: CandidateStoreRecord) -> tuple[int, int, str]:
        try:
            write = self._writes(record.kind, (record,))[0]
            document_size = cast(int, write.update._pb.ByteSize())
            contribution = self._commit_size((write,)) - self._commit_size(())
            if document_size < 0 or contribution < 0:
                raise CandidateStoreFailure("permanent") from None
            return document_size, contribution, self._write_sha256(write)
        except (_CandidateStoreMalformed, CandidateStoreFailure):
            raise
        except Exception:
            raise CandidateStoreFailure("permanent") from None

    def _read_value(self, kind: CandidateRecordKind, raw: object) -> Mapping[str, object]:
        if type(raw) is not dict:
            raise _CandidateStoreMalformed() from None
        value = dict(cast(dict[str, object], raw))
        if kind == "chunk":
            embedding = value.get("embedding")
            vector_type = cast(Any, self._sdk).vector.Vector
            if type(embedding) is not vector_type:
                raise _CandidateStoreMalformed() from None
            vector = getattr(embedding, "_value", None)
            if not isinstance(vector, tuple) or any(
                type(item) is not float or not math.isfinite(item) for item in vector
            ):
                raise _CandidateStoreMalformed() from None
            value["embedding"] = tuple(0.0 if item == 0.0 else item for item in vector)
        return value

    def _snapshot_record(
        self, kind: CandidateRecordKind, snapshot: object
    ) -> CandidateStoreRecord | None:
        try:
            if cast(Any, snapshot).exists is not True:
                return None
            key = cast(Any, snapshot).id
            raw = cast(Any, snapshot).to_dict()
            return CandidateStoreRecord(
                kind=kind,
                key=key,
                value=cast(Mapping[str, StrictStoreValue], self._read_value(kind, raw)),
            )
        except (_CandidateStoreMalformed, CandidateStoreFailure):
            raise
        except Exception:
            raise _CandidateStoreMalformed() from None

    async def get(
        self, kind: CandidateRecordKind, key: str, *, timeout_seconds: int
    ) -> CandidateStoreRecord | None:
        try:
            snapshot = await self._reference(kind, key).get(retry=None, timeout=timeout_seconds)
            return self._snapshot_record(kind, snapshot)
        except asyncio.CancelledError:
            raise
        except (_CandidateStoreMalformed, CandidateStoreFailure):
            raise
        except Exception as error:
            raise _normalized_sdk_error(error) from None

    async def get_many(
        self,
        kind: CandidateRecordKind,
        keys: tuple[str, ...],
        *,
        timeout_seconds: int,
    ) -> tuple[CandidateStoreRecord | None, ...]:
        try:
            references = [self._reference(kind, key) for key in keys]
            by_key: dict[str, CandidateStoreRecord | None] = {}
            snapshots = cast(Any, self._client).get_all(
                references, retry=None, timeout=timeout_seconds
            )
            async for snapshot in snapshots:
                key = cast(Any, snapshot).id
                if type(key) is not str or key not in keys or key in by_key:
                    raise _CandidateStoreMalformed() from None
                by_key[key] = self._snapshot_record(kind, snapshot)
            if set(by_key) != set(keys):
                raise _CandidateStoreMalformed() from None
            return tuple(by_key[key] for key in keys)
        except asyncio.CancelledError:
            raise
        except (_CandidateStoreMalformed, CandidateStoreFailure):
            raise
        except Exception as error:
            raise _normalized_sdk_error(error) from None

    async def create_many_checked(
        self,
        kind: CandidateRecordKind,
        records: tuple[CandidateStoreRecord, ...],
        *,
        expected_encoded_size: int,
        expected_write_sha256s: tuple[str, ...],
        timeout_seconds: int,
    ) -> None:
        try:
            if (
                type(expected_encoded_size) is not int
                or expected_encoded_size < 0
                or type(expected_write_sha256s) is not tuple
                or len(expected_write_sha256s) != len(records)
                or any(
                    type(value) is not str
                    or len(value) != 64
                    or any(character not in "0123456789abcdef" for character in value)
                    for value in expected_write_sha256s
                )
            ):
                raise CandidateStoreFailure("permanent") from None
            batch = cast(Any, self._client).batch()
            for record in records:
                batch.create(self._reference(kind, record.key), self._write_value(record))
            writes = tuple(batch._write_pbs)
            if (
                len(writes) != len(records)
                or any(
                    cast(int, write.update._pb.ByteSize()) > FIRESTORE_DOCUMENT_MAX_BYTES
                    for write in writes
                )
                or self._commit_size(writes) != expected_encoded_size
                or tuple(self._write_sha256(write) for write in writes) != expected_write_sha256s
            ):
                raise _CandidateStoreMalformed() from None
            await batch.commit(retry=None, timeout=timeout_seconds)
        except asyncio.CancelledError:
            raise
        except (_CandidateStoreMalformed, CandidateStoreFailure):
            raise
        except Exception as error:
            raise _normalized_sdk_error(error) from None

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

    async def list_page(
        self,
        kind: CandidateRecordKind,
        corpus: ExactCorpusReference,
        after_key: str | None,
        limit: int,
        *,
        timeout_seconds: int,
    ) -> CandidateStorePage:
        if kind not in {"document", "chunk"} or limit != CANDIDATE_READ_PAGE_SIZE:
            raise CandidateStoreFailure("permanent") from None
        try:
            field_filter = cast(Any, self._sdk).base_query.FieldFilter
            and_filter = cast(Any, self._sdk).base_query.And
            filters = and_filter(
                [
                    field_filter("corpus_id", "==", corpus.corpus_id),
                    field_filter("corpus_version", "==", corpus.corpus_version),
                ]
            )
            query = (
                cast(Any, self._client)
                .collection(_COLLECTIONS[kind])
                .where(filter=filters)
                .order_by("__name__")
            )
            if after_key is not None:
                query = query.start_after({"__name__": self._reference(kind, after_key)})
            snapshots = await query.limit(CANDIDATE_READ_LOOKAHEAD_SIZE).get(
                retry=None, timeout=timeout_seconds
            )
            if len(snapshots) > CANDIDATE_READ_LOOKAHEAD_SIZE:
                raise _CandidateStoreMalformed() from None
            converted = tuple(
                record
                for snapshot in snapshots
                if (record := self._snapshot_record(kind, snapshot)) is not None
            )
            if len(converted) != len(snapshots):
                raise _CandidateStoreMalformed() from None
            records = converted[:CANDIDATE_READ_PAGE_SIZE]
            next_after = (
                records[-1].key if len(converted) == CANDIDATE_READ_LOOKAHEAD_SIZE else None
            )
            return CandidateStorePage(records=records, next_after_key=next_after)
        except asyncio.CancelledError:
            raise
        except (_CandidateStoreMalformed, CandidateStoreFailure):
            raise
        except Exception as error:
            raise _normalized_sdk_error(error) from None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        result = cast(Any, self._client).close()
        if inspect.isawaitable(result):
            await result


def _normalized_sdk_error(error: Exception) -> CandidateStoreFailure:
    try:
        exceptions = importlib.import_module("google.api_core.exceptions")
        if isinstance(error, exceptions.AlreadyExists):
            return CandidateStoreFailure("conflict")
        if isinstance(
            error,
            (
                exceptions.ServiceUnavailable,
                exceptions.DeadlineExceeded,
                exceptions.Aborted,
            ),
        ):
            return CandidateStoreFailure("transient")
    except (ImportError, AttributeError):
        pass
    return CandidateStoreFailure("permanent")


def create_candidate_store(project_id: str) -> FirestoreSdkCandidateStore:
    """Construct an SDK-backed store without issuing a request."""
    try:
        sdk = importlib.import_module("google.cloud.firestore_v1")
        helpers = importlib.import_module("google.cloud.firestore_v1._helpers")
    except ModuleNotFoundError:
        raise RuntimeError(
            "Candidate persistence requires the optional 'firestore' dependency profile"
        ) from None
    client = sdk.AsyncClient(project=project_id, database=FIRESTORE_DATABASE_ID)
    return FirestoreSdkCandidateStore(client, sdk=sdk, helpers=helpers)
