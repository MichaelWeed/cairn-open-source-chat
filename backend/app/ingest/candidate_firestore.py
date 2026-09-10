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
    CandidateRecordKind,
    CandidateStoreFailure,
    CandidateStorePage,
    CandidateStoreRecord,
    StrictStoreValue,
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
        self._encoded_sizes: dict[tuple[str, tuple[str, ...], str], int] = {}

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

    def _fingerprint(
        self,
        kind: CandidateRecordKind,
        records: tuple[CandidateStoreRecord, ...],
        writes: tuple[Any, ...],
    ) -> tuple[str, tuple[str, ...], str]:
        digest = hashlib.sha256()
        for write in writes:
            material = cast(bytes, write._pb.SerializeToString())
            digest.update(len(material).to_bytes(8, "big"))
            digest.update(material)
        return kind, tuple(record.key for record in records), digest.hexdigest()

    def encoded_document_size(self, record: CandidateStoreRecord) -> int:
        try:
            write = self._writes(record.kind, (record,))[0]
            return cast(int, write.update._pb.ByteSize())
        except CandidateStoreFailure:
            raise
        except Exception:
            raise CandidateStoreFailure("permanent") from None

    def encoded_create_size(
        self, kind: CandidateRecordKind, records: tuple[CandidateStoreRecord, ...]
    ) -> int:
        try:
            writes = self._writes(kind, records)
            request = cast(Any, self._sdk).types.CommitRequest(
                database=cast(Any, self._client)._database_string,
                writes=writes,
            )
            size = cast(int, request._pb.ByteSize())
            self._encoded_sizes[self._fingerprint(kind, records, writes)] = size
            return size
        except CandidateStoreFailure:
            raise
        except Exception:
            raise CandidateStoreFailure("permanent") from None

    def _read_value(self, kind: CandidateRecordKind, raw: object) -> Mapping[str, object]:
        if type(raw) is not dict:
            raise CandidateStoreFailure("permanent") from None
        value = dict(cast(dict[str, object], raw))
        if kind == "chunk":
            embedding = value.get("embedding")
            vector_type = cast(Any, self._sdk).vector.Vector
            if type(embedding) is not vector_type:
                raise CandidateStoreFailure("permanent") from None
            vector = getattr(embedding, "_value", None)
            if not isinstance(vector, tuple) or any(
                type(item) is not float or not math.isfinite(item) for item in vector
            ):
                raise CandidateStoreFailure("permanent") from None
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
        except CandidateStoreFailure:
            raise
        except Exception:
            raise CandidateStoreFailure("permanent") from None

    async def get(
        self, kind: CandidateRecordKind, key: str, *, timeout_seconds: int
    ) -> CandidateStoreRecord | None:
        try:
            snapshot = await self._reference(kind, key).get(
                retry=None, timeout=timeout_seconds
            )
            return self._snapshot_record(kind, snapshot)
        except asyncio.CancelledError:
            raise
        except CandidateStoreFailure:
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
                    raise CandidateStoreFailure("permanent") from None
                by_key[key] = self._snapshot_record(kind, snapshot)
            if set(by_key) != set(keys):
                raise CandidateStoreFailure("permanent") from None
            return tuple(by_key[key] for key in keys)
        except asyncio.CancelledError:
            raise
        except CandidateStoreFailure:
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
        try:
            writes = self._writes(kind, records)
            fingerprint = self._fingerprint(kind, records, writes)
            expected_size = self._encoded_sizes.get(fingerprint)
            request = cast(Any, self._sdk).types.CommitRequest(
                database=cast(Any, self._client)._database_string,
                writes=writes,
            )
            if expected_size is None or request._pb.ByteSize() != expected_size:
                raise CandidateStoreFailure("permanent") from None
            batch = cast(Any, self._client).batch()
            for record in records:
                batch.create(self._reference(kind, record.key), self._write_value(record))
            if tuple(batch._write_pbs) != writes:
                raise CandidateStoreFailure("permanent") from None
            await batch.commit(retry=None, timeout=timeout_seconds)
        except asyncio.CancelledError:
            raise
        except CandidateStoreFailure:
            raise
        except Exception as error:
            raise _normalized_sdk_error(error) from None

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
                query = query.start_after(
                    {"__name__": self._reference(kind, after_key)}
                )
            snapshots = await query.limit(CANDIDATE_READ_LOOKAHEAD_SIZE).get(
                retry=None, timeout=timeout_seconds
            )
            if len(snapshots) > CANDIDATE_READ_LOOKAHEAD_SIZE:
                raise CandidateStoreFailure("permanent") from None
            converted = tuple(
                record
                for snapshot in snapshots
                if (record := self._snapshot_record(kind, snapshot)) is not None
            )
            if len(converted) != len(snapshots):
                raise CandidateStoreFailure("permanent") from None
            records = converted[:CANDIDATE_READ_PAGE_SIZE]
            next_after = (
                records[-1].key
                if len(converted) == CANDIDATE_READ_LOOKAHEAD_SIZE
                else None
            )
            return CandidateStorePage(records=records, next_after_key=next_after)
        except asyncio.CancelledError:
            raise
        except CandidateStoreFailure:
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
