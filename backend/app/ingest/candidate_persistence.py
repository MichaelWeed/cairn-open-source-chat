"""Immutable candidate persistence and content attestation.

This module is deliberately not wired into application startup.  It owns a
create-only, exact-scope persistence protocol that can be exercised with an
injected store and signer without credentials or external I/O.
"""

import asyncio
import base64
import hashlib
import json
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from functools import partial
from types import MappingProxyType
from typing import Annotated, Any, Literal, Protocol, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetCoreSchemaHandler,
    StrictInt,
    field_validator,
)
from pydantic_core import CoreSchema, core_schema

from app.ingest.planner import (
    CandidateIngestionPlan,
    PlannedDocument,
    _embedding_sha256,
    _plan_sha256,
    _text_sha256,
)
from app.retrieval_contracts import ExactCorpusReference
from app.retrieval_firestore import (
    FIRESTORE_RECORD_SCHEMA_VERSION,
    firestore_chunk_document_id,
)

CANDIDATE_PERSISTENCE_CONTRACT_VERSION: Literal["1.0"] = "1.0"
CANDIDATE_RECORD_SCHEMA_VERSION: Literal["1.0"] = "1.0"
DOCUMENT_RECORD_SCHEMA_VERSION: Literal["1.0"] = "1.0"
ATTESTATION_CONTRACT_VERSION: Literal["1.0"] = "1.0"
ATTESTATION_RECORD_SCHEMA_VERSION: Literal["1.0"] = "1.0"
INVENTORY_ALGORITHM: Literal["cairn-candidate-inventory-v1"] = (
    "cairn-candidate-inventory-v1"
)
CANDIDATE_WRITE_BATCH_MAX_RECORDS = 400
CANDIDATE_WRITE_BATCH_MAX_BYTES = 8_388_608
FIRESTORE_DOCUMENT_MAX_BYTES = 1_048_576
CANDIDATE_READ_PAGE_SIZE = 200
CANDIDATE_READ_LOOKAHEAD_SIZE = 201
MAX_SIGNER_ID_CHARS = 256
MIN_SIGNATURE_BYTES = 16
MAX_SIGNATURE_BYTES = 4096

CANDIDATE_COLLECTION = "cairn_corpus_candidates_v1"
DOCUMENT_COLLECTION = "cairn_corpus_documents_v1"
ATTESTATION_COLLECTION = "cairn_corpus_attestations_v1"

CandidateRecordKind = Literal["candidate", "document", "chunk", "attestation"]
CandidatePersistenceErrorCode = Literal[
    "invalid_plan",
    "unsupported_embedding",
    "candidate_conflict",
    "store_bounds_exceeded",
    "store_unavailable",
    "malformed_store",
    "attestation_failed",
]
CandidateDisposition = Literal["created", "resumed", "confirmed"]
type StrictStoreValue = (
    None
    | bool
    | int
    | float
    | str
    | tuple["StrictStoreValue", ...]
    | Mapping[str, "StrictStoreValue"]
)

_ERROR_MESSAGES: dict[CandidatePersistenceErrorCode, str] = {
    "invalid_plan": "The candidate persistence request is invalid.",
    "unsupported_embedding": "The candidate embedding is unsupported.",
    "candidate_conflict": "The immutable candidate conflicts with stored content.",
    "store_bounds_exceeded": "The candidate exceeds a persistence bound.",
    "store_unavailable": "The candidate store is unavailable.",
    "malformed_store": "The candidate store returned an invalid result.",
    "attestation_failed": "Candidate attestation verification failed.",
}
_MISSING = object()


class CandidatePersistenceError(Exception):
    """Fixed, content-free persistence failure."""

    def __init__(self, code: CandidatePersistenceErrorCode) -> None:
        if type(code) is not str or code not in _ERROR_MESSAGES:
            raise ValueError("Unsupported candidate persistence error code.") from None
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])

    def __repr__(self) -> str:
        return f"CandidatePersistenceError(code={self.code!r})"

    def errors(
        self,
        *,
        include_url: bool = True,
        include_context: bool = True,
        include_input: bool = True,
    ) -> list[dict[str, object]]:
        del include_url, include_context, include_input
        return [
            {
                "type": "candidate_persistence_error",
                "loc": (),
                "msg": _ERROR_MESSAGES[self.code],
                "code": self.code,
            }
        ]

    def json(
        self,
        *,
        indent: int | None = None,
        include_url: bool = True,
        include_context: bool = True,
        include_input: bool = True,
    ) -> str:
        return json.dumps(
            self.errors(
                include_url=include_url,
                include_context=include_context,
                include_input=include_input,
            ),
            indent=indent,
            separators=None if indent is not None else (",", ":"),
        )


def canonical_json_bytes(value: object) -> bytes:
    """Encode attestation material with the contract's canonical JSON rules."""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError):
        raise CandidatePersistenceError("invalid_plan") from None


class CandidateStoreFailure(Exception):
    """Content-free store transport classification."""

    def __init__(self, code: Literal["conflict", "transient", "permanent"]) -> None:
        if code not in {"conflict", "transient", "permanent"}:
            raise ValueError("Unsupported candidate store failure code.") from None
        self.code = code
        super().__init__("Candidate store request failed.")

    def __repr__(self) -> str:
        return f"CandidateStoreFailure(code={self.code!r})"


def _content_free_validation(
    model: type["CandidatePersistenceModel"],
    value: Any,
    handler: Callable[[Any], Any],
) -> Any:
    result: Any = _MISSING
    try:
        if isinstance(value, Mapping) and any(key not in model.model_fields for key in value):
            raise ValueError
        result = handler(value)
    except CandidatePersistenceError:
        raise
    except Exception:
        pass
    if result is _MISSING:
        raise CandidatePersistenceError("invalid_plan") from None
    return result


class CandidatePersistenceModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        hide_input_in_errors=True,
        arbitrary_types_allowed=True,
    )

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        schema = handler(source_type)
        return core_schema.no_info_wrap_validator_function(
            lambda value, validator: _content_free_validation(cls, value, validator),
            schema,
        )

    @classmethod
    def _validated(cls, values: Any) -> Self:
        try:
            return cls.model_validate(values)
        except CandidatePersistenceError:
            raise
        except Exception:
            raise CandidatePersistenceError("invalid_plan") from None

    @classmethod
    def model_construct(
        cls, _fields_set: set[str] | None = None, **values: Any
    ) -> Self:
        if _fields_set is not None:
            try:
                if not set(_fields_set).issubset(cls.model_fields):
                    raise ValueError
            except Exception:
                raise CandidatePersistenceError("invalid_plan") from None
        result = cls._validated(values)
        if _fields_set is not None:
            object.__setattr__(result, "__pydantic_fields_set__", set(_fields_set))
        return result

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        try:
            source = super().model_copy(deep=deep)
            values = {
                name: getattr(source, name) for name in type(self).model_fields
            }
            fields_set = set(source.model_fields_set)
            if update is not None:
                values.update(update)
                fields_set.update(update)
        except Exception:
            raise CandidatePersistenceError("invalid_plan") from None
        result = type(self)._validated(values)
        object.__setattr__(result, "__pydantic_fields_set__", fields_set)
        return result

    def copy(
        self,
        *,
        include: Any = None,
        exclude: Any = None,
        update: dict[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if include is None and exclude is None and update is None:
            return self.model_copy(deep=deep)
        try:
            values = self.model_dump(include=include, exclude=exclude, round_trip=True)
            if update is not None:
                values.update(update)
        except Exception:
            raise CandidatePersistenceError("invalid_plan") from None
        return type(self)._validated(values)


def _freeze_store_value(value: object) -> StrictStoreValue:
    if value is None or type(value) in {bool, int, str}:
        return cast(None | bool | int | str, value)
    if type(value) is float:
        number = value
        if not math.isfinite(number):
            raise ValueError
        return 0.0 if number == 0.0 else number
    if isinstance(value, tuple):
        return tuple(_freeze_store_value(item) for item in value)
    if isinstance(value, Mapping):
        copied: dict[str, StrictStoreValue] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError
            copied[key] = _freeze_store_value(item)
        return MappingProxyType(copied)
    raise ValueError


def _freeze_store_mapping(value: object) -> Mapping[str, StrictStoreValue]:
    if not isinstance(value, Mapping):
        raise ValueError
    frozen = _freeze_store_value(value)
    if not isinstance(frozen, Mapping):
        raise ValueError
    return frozen


def _revalidate_plan(value: object) -> CandidateIngestionPlan:
    try:
        if type(value) is not CandidateIngestionPlan:
            raise TypeError
        if object.__getattribute__(value, "__pydantic_extra__"):
            raise ValueError
        fields = {
            name: getattr(value, name) for name in CandidateIngestionPlan.model_fields
        }
        return CandidateIngestionPlan.model_validate(fields)
    except Exception:
        raise CandidatePersistenceError("invalid_plan") from None


def _revalidate_corpus(value: object) -> ExactCorpusReference:
    try:
        if type(value) is ExactCorpusReference:
            if object.__getattribute__(value, "__pydantic_extra__"):
                raise ValueError
            value = {
                name: getattr(value, name) for name in ExactCorpusReference.model_fields
            }
        elif isinstance(value, Mapping):
            value = dict(value)
        else:
            raise TypeError
        return ExactCorpusReference.model_validate(value)
    except Exception:
        raise CandidatePersistenceError("invalid_plan") from None


class CandidatePersistenceRequest(CandidatePersistenceModel):
    contract_version: Literal["1.0"]
    plan: CandidateIngestionPlan

    @field_validator("plan", mode="before")
    @classmethod
    def validate_plan(cls, value: object) -> CandidateIngestionPlan:
        return _revalidate_plan(value)


class CandidatePersistenceReceipt(CandidatePersistenceModel):
    contract_version: Literal["1.0"]
    corpus: ExactCorpusReference
    plan_sha256: Annotated[str, Field(min_length=64, max_length=64)]
    disposition: CandidateDisposition
    document_count: Annotated[StrictInt, Field(ge=1)]
    chunk_count: Annotated[StrictInt, Field(ge=1)]
    inventory_sha256: Annotated[str, Field(min_length=64, max_length=64)]
    attestation_payload_sha256: Annotated[str, Field(min_length=64, max_length=64)]

    @field_validator("corpus", mode="before")
    @classmethod
    def validate_corpus(cls, value: object) -> ExactCorpusReference:
        return _revalidate_corpus(value)

    @field_validator("plan_sha256", "inventory_sha256", "attestation_payload_sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if any(character not in "0123456789abcdef" for character in value):
            raise ValueError
        return value


class CandidateStoreRecord(CandidatePersistenceModel):
    kind: CandidateRecordKind
    key: Annotated[str, Field(min_length=1, max_length=256)]
    value: Mapping[str, StrictStoreValue]

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if not value.isascii() or any(ord(char) < 0x21 or ord(char) > 0x7E for char in value):
            raise ValueError
        return value

    @field_validator("value", mode="before")
    @classmethod
    def validate_value_before(cls, value: object) -> Mapping[str, StrictStoreValue]:
        return _freeze_store_mapping(value)

    @field_validator("value")
    @classmethod
    def validate_value_after(
        cls, value: Mapping[str, StrictStoreValue]
    ) -> Mapping[str, StrictStoreValue]:
        return _freeze_store_mapping(value)


class CandidateStorePage(CandidatePersistenceModel):
    records: Annotated[tuple[CandidateStoreRecord, ...], Field(max_length=200)]
    next_after_key: Annotated[str, Field(min_length=1, max_length=256)] | None

    @field_validator("next_after_key")
    @classmethod
    def validate_cursor(cls, value: str | None) -> str | None:
        if value is not None and (
            not value.isascii()
            or any(ord(char) < 0x21 or ord(char) > 0x7E for char in value)
        ):
            raise ValueError
        return value


class AttestationCorpus(CandidatePersistenceModel):
    kind: Literal["exact"]
    corpus_id: str
    corpus_version: str


class AttestationEmbedding(CandidatePersistenceModel):
    identity: str
    dimensions: Annotated[StrictInt, Field(ge=1, le=2048)]


class AttestationRecordSchemas(CandidatePersistenceModel):
    candidate: Literal["1.0"]
    document: Literal["1.0"]
    chunk: Literal["1.0"]


class AttestationPayload(CandidatePersistenceModel):
    attestation_version: Literal["1.0"]
    corpus: AttestationCorpus
    plan_contract_version: Literal["1.0"]
    plan_sha256: Annotated[str, Field(min_length=64, max_length=64)]
    semantic_manifest_sha256: Annotated[str, Field(min_length=64, max_length=64)]
    embedding: AttestationEmbedding
    document_count: Annotated[StrictInt, Field(ge=1)]
    chunk_count: Annotated[StrictInt, Field(ge=1)]
    record_schemas: AttestationRecordSchemas
    inventory_algorithm: Literal["cairn-candidate-inventory-v1"]
    inventory_sha256: Annotated[str, Field(min_length=64, max_length=64)]
    signature_algorithm_id: Annotated[str, Field(min_length=1, max_length=MAX_SIGNER_ID_CHARS)]
    signing_key_id: Annotated[str, Field(min_length=1, max_length=MAX_SIGNER_ID_CHARS)]

    @field_validator(
        "plan_sha256", "semantic_manifest_sha256", "inventory_sha256"
    )
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if any(character not in "0123456789abcdef" for character in value):
            raise ValueError
        return value

    @field_validator("signature_algorithm_id", "signing_key_id")
    @classmethod
    def validate_signer_id(cls, value: str) -> str:
        if (
            value != value.strip()
            or not value.isascii()
            or any(ord(char) < 0x20 or ord(char) > 0x7E for char in value)
        ):
            raise ValueError
        return value


class CandidateAttestation(CandidatePersistenceModel):
    schema_version: Literal["1.0"]
    payload: AttestationPayload
    payload_sha256: Annotated[str, Field(min_length=64, max_length=64)]
    signature_b64url: Annotated[str, Field(min_length=22, max_length=5462)]

    @field_validator("payload_sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if any(character not in "0123456789abcdef" for character in value):
            raise ValueError
        return value


class CandidateStore(Protocol):
    def encoded_document_size(self, record: CandidateStoreRecord) -> int: ...

    def encoded_create_size(
        self, kind: CandidateRecordKind, records: tuple[CandidateStoreRecord, ...]
    ) -> int: ...

    async def get(
        self, kind: CandidateRecordKind, key: str, *, timeout_seconds: int
    ) -> CandidateStoreRecord | None: ...

    async def get_many(
        self,
        kind: CandidateRecordKind,
        keys: tuple[str, ...],
        *,
        timeout_seconds: int,
    ) -> tuple[CandidateStoreRecord | None, ...]: ...

    async def create_many(
        self,
        kind: CandidateRecordKind,
        records: tuple[CandidateStoreRecord, ...],
        *,
        timeout_seconds: int,
    ) -> None: ...

    async def list_page(
        self,
        kind: CandidateRecordKind,
        corpus: ExactCorpusReference,
        after_key: str | None,
        limit: int,
        *,
        timeout_seconds: int,
    ) -> CandidateStorePage: ...

    async def aclose(self) -> None: ...


class AttestationVerifier(Protocol):
    @property
    def algorithm_id(self) -> str: ...

    @property
    def key_id(self) -> str: ...

    async def verify(self, payload: bytes, signature: bytes) -> bool: ...


class AttestationSigner(AttestationVerifier, Protocol):
    async def sign(self, payload: bytes) -> bytes: ...


def candidate_store_key(corpus: ExactCorpusReference) -> str:
    corpus = _revalidate_corpus(corpus)
    material = canonical_json_bytes(
        {
            "corpus_id": corpus.corpus_id,
            "corpus_version": corpus.corpus_version,
            "kind": "exact",
        }
    )
    digest = hashlib.sha256(b"cairn-candidate-v1\0" + material).hexdigest()
    return "cand1-" + digest


def _record(
    kind: CandidateRecordKind, key: str, value: Mapping[str, object]
) -> CandidateStoreRecord:
    return CandidateStoreRecord(
        kind=kind,
        key=key,
        value=cast(Mapping[str, StrictStoreValue], value),
    )


def _candidate_header(plan: CandidateIngestionPlan) -> CandidateStoreRecord:
    return _record(
        "candidate",
        candidate_store_key(plan.corpus),
        {
            "schema_version": CANDIDATE_RECORD_SCHEMA_VERSION,
            "plan_contract_version": plan.contract_version,
            "corpus_id": plan.corpus.corpus_id,
            "corpus_version": plan.corpus.corpus_version,
            "plan_sha256": plan.plan_sha256,
            "semantic_manifest_sha256": plan.semantic_manifest_sha256,
            "embedding_identity": plan.embedding.identity,
            "embedding_dimensions": plan.embedding.dimensions,
            "document_count": plan.document_count,
            "chunk_count": plan.chunk_count,
        },
    )


def _document_record(
    plan: CandidateIngestionPlan, document: PlannedDocument
) -> CandidateStoreRecord:
    return _record(
        "document",
        document.document_id,
        {
            "schema_version": DOCUMENT_RECORD_SCHEMA_VERSION,
            "plan_contract_version": plan.contract_version,
            "corpus_id": plan.corpus.corpus_id,
            "corpus_version": plan.corpus.corpus_version,
            "plan_sha256": plan.plan_sha256,
            "document_id": document.document_id,
            "relative_path": document.relative_path,
            "parser_id": document.parser_id,
            "normalization_id": document.normalization_id,
            "chunking_id": document.chunking_id,
            "normalized_text_sha256": document.normalized_text_sha256,
            "document_plan_sha256": document.document_plan_sha256,
            "chunk_count": len(document.chunks),
            "provenance": {
                "title": document.provenance.title,
                "url": document.provenance.url,
                "owner": document.provenance.owner,
                "reviewed_at": document.provenance.reviewed_at.isoformat(),
                "source_sha256": document.provenance.source_sha256,
            },
        },
    )


def _chunk_record(
    plan: CandidateIngestionPlan, document: PlannedDocument, chunk_index: int
) -> CandidateStoreRecord:
    chunk = document.chunks[chunk_index]
    if (
        chunk.document_id != document.document_id
        or chunk.chunk_index != chunk_index
        or chunk.citation_title != document.provenance.title
        or chunk.citation_url != document.provenance.url
    ):
        raise CandidatePersistenceError("invalid_plan") from None
    return _record(
        "chunk",
        firestore_chunk_document_id(chunk.chunk_id),
        {
            "schema_version": FIRESTORE_RECORD_SCHEMA_VERSION,
            "corpus_id": plan.corpus.corpus_id,
            "corpus_version": plan.corpus.corpus_version,
            "embedding_identity": plan.embedding.identity,
            "chunk_id": chunk.chunk_id,
            "document_id": chunk.document_id,
            "source": document.relative_path,
            "chunk_index": chunk.chunk_index,
            "text": chunk.text,
            "citation_title": chunk.citation_title,
            "citation_url": chunk.citation_url,
            "embedding": chunk.embedding,
        },
    )


def expected_candidate_records(
    plan: CandidateIngestionPlan,
) -> tuple[
    CandidateStoreRecord,
    tuple[CandidateStoreRecord, ...],
    tuple[CandidateStoreRecord, ...],
]:
    plan = _revalidate_plan(plan)
    documents = tuple(
        sorted(
            (_document_record(plan, document) for document in plan.documents),
            key=lambda record: record.key.encode("utf-8"),
        )
    )
    chunks = tuple(
        sorted(
            (
                _chunk_record(plan, document, index)
                for document in plan.documents
                for index in range(len(document.chunks))
            ),
            key=lambda record: record.key.encode("utf-8"),
        )
    )
    if len({record.key for record in documents}) != len(documents) or len(
        {record.key for record in chunks}
    ) != len(chunks):
        raise CandidatePersistenceError("invalid_plan") from None
    return _candidate_header(plan), documents, chunks


def _plain_store_value(value: StrictStoreValue) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_store_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_store_value(item) for item in value]
    return value


def _exact_value_equal(left: StrictStoreValue, right: StrictStoreValue) -> bool:
    if type(left) is float or type(right) is float:
        return (
            type(left) is float
            and type(right) is float
            and left.hex() == right.hex()
        )
    if isinstance(left, Mapping) or isinstance(right, Mapping):
        if not isinstance(left, Mapping) or not isinstance(right, Mapping):
            return False
        if set(left.keys()) != set(right.keys()):
            return False
        return all(_exact_value_equal(left[key], right[key]) for key in left)
    if isinstance(left, tuple) or isinstance(right, tuple):
        return (
            isinstance(left, tuple)
            and isinstance(right, tuple)
            and len(left) == len(right)
            and all(_exact_value_equal(a, b) for a, b in zip(left, right, strict=True))
        )
    return type(left) is type(right) and left == right


def _records_equal(left: CandidateStoreRecord, right: CandidateStoreRecord) -> bool:
    return (
        left.kind == right.kind
        and left.key == right.key
        and _exact_value_equal(left.value, right.value)
    )


def _inventory_value(record: CandidateStoreRecord) -> object:
    def convert(value: StrictStoreValue, *, embedding: bool = False) -> object:
        if isinstance(value, Mapping):
            return {
                key: convert(item, embedding=record.kind == "chunk" and key == "embedding")
                for key, item in value.items()
            }
        if isinstance(value, tuple):
            return [convert(item, embedding=embedding) for item in value]
        if type(value) is float:
            if not embedding:
                raise CandidatePersistenceError("malformed_store") from None
            return (0.0 if value == 0.0 else value).hex()
        return value

    return convert(record.value)


def candidate_inventory_sha256(records: Sequence[CandidateStoreRecord]) -> str:
    hasher = hashlib.sha256()
    hasher.update(b"cairn-candidate-inventory-v1\0")
    for record in records:
        material = canonical_json_bytes(
            {
                "key": record.key,
                "kind": record.kind,
                "value": _inventory_value(record),
            }
        )
        if len(material) > (2**64 - 1):
            raise CandidatePersistenceError("store_bounds_exceeded") from None
        hasher.update(len(material).to_bytes(8, "big", signed=False))
        hasher.update(material)
    return hasher.hexdigest()


def _signer_identity(verifier: AttestationVerifier) -> tuple[str, str]:
    try:
        algorithm_id = verifier.algorithm_id
        key_id = verifier.key_id
    except asyncio.CancelledError:
        raise
    except Exception:
        raise CandidatePersistenceError("attestation_failed") from None
    for value in (algorithm_id, key_id):
        if (
            type(value) is not str
            or not 1 <= len(value) <= MAX_SIGNER_ID_CHARS
            or value != value.strip()
            or not value.isascii()
            or any(ord(char) < 0x20 or ord(char) > 0x7E for char in value)
        ):
            raise CandidatePersistenceError("attestation_failed") from None
    return algorithm_id, key_id


def _attestation_payload(
    plan: CandidateIngestionPlan,
    inventory_sha256: str,
    verifier: AttestationVerifier,
) -> AttestationPayload:
    algorithm_id, key_id = _signer_identity(verifier)
    return AttestationPayload(
        attestation_version=ATTESTATION_CONTRACT_VERSION,
        corpus=AttestationCorpus(
            kind="exact",
            corpus_id=plan.corpus.corpus_id,
            corpus_version=plan.corpus.corpus_version,
        ),
        plan_contract_version=plan.contract_version,
        plan_sha256=plan.plan_sha256,
        semantic_manifest_sha256=plan.semantic_manifest_sha256,
        embedding=AttestationEmbedding(
            identity=plan.embedding.identity,
            dimensions=plan.embedding.dimensions,
        ),
        document_count=plan.document_count,
        chunk_count=plan.chunk_count,
        record_schemas=AttestationRecordSchemas(
            candidate=CANDIDATE_RECORD_SCHEMA_VERSION,
            document=DOCUMENT_RECORD_SCHEMA_VERSION,
            chunk=cast(Literal["1.0"], FIRESTORE_RECORD_SCHEMA_VERSION),
        ),
        inventory_algorithm=INVENTORY_ALGORITHM,
        inventory_sha256=inventory_sha256,
        signature_algorithm_id=algorithm_id,
        signing_key_id=key_id,
    )


def _payload_mapping(payload: AttestationPayload) -> dict[str, object]:
    return payload.model_dump(mode="json")


def _attestation_record(
    plan: CandidateIngestionPlan,
    payload: AttestationPayload,
    signature: bytes,
) -> CandidateStoreRecord:
    payload_bytes = canonical_json_bytes(_payload_mapping(payload))
    encoded = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return _record(
        "attestation",
        candidate_store_key(plan.corpus),
        {
            "schema_version": ATTESTATION_RECORD_SCHEMA_VERSION,
            "payload": _payload_mapping(payload),
            "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
            "signature_b64url": encoded,
        },
    )


def _validated_record(value: object) -> CandidateStoreRecord:
    try:
        if type(value) is not CandidateStoreRecord:
            raise TypeError
        if object.__getattribute__(value, "__pydantic_extra__"):
            raise ValueError
        return CandidateStoreRecord.model_validate(
            {name: getattr(value, name) for name in CandidateStoreRecord.model_fields}
        )
    except Exception:
        raise CandidatePersistenceError("malformed_store") from None


class CandidatePersistenceService:
    def __init__(
        self,
        *,
        store: CandidateStore,
        signer: AttestationSigner,
        expected_embedding_identity: str,
        expected_embedding_dimensions: int,
        timeout_seconds: int,
        max_retries: int,
        sleep: Callable[[float], Awaitable[None]],
        owns_store: bool = False,
    ) -> None:
        if (
            type(expected_embedding_identity) is not str
            or not 1 <= len(expected_embedding_identity) <= 256
            or expected_embedding_identity != expected_embedding_identity.strip()
            or not expected_embedding_identity.isascii()
            or type(expected_embedding_dimensions) is not int
            or not 1 <= expected_embedding_dimensions <= 2048
            or type(timeout_seconds) is not int
            or not 1 <= timeout_seconds <= 30
            or type(max_retries) is not int
            or max_retries not in {0, 1}
            or not callable(sleep)
            or type(owns_store) is not bool
        ):
            raise CandidatePersistenceError("invalid_plan") from None
        self._store = store
        self._signer = signer
        self._expected_embedding_identity = expected_embedding_identity
        self._expected_embedding_dimensions = expected_embedding_dimensions
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._sleep = sleep
        self._owns_store = owns_store
        self._closed = False
        self._close_lock = asyncio.Lock()
        self._document_size_cache: dict[tuple[str, str, str], int] = {}
        self._batch_size_cache: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}

    def _validate_request(self, request: object) -> CandidateIngestionPlan:
        if self._closed:
            raise CandidatePersistenceError("store_unavailable") from None
        try:
            if type(request) is not CandidatePersistenceRequest:
                raise TypeError
            request = CandidatePersistenceRequest.model_validate(
                {
                    name: getattr(request, name)
                    for name in CandidatePersistenceRequest.model_fields
                }
            )
        except CandidatePersistenceError:
            raise
        except Exception:
            raise CandidatePersistenceError("invalid_plan") from None
        plan = _revalidate_plan(request.plan)
        if (
            plan.embedding.dimensions > 2048
            or plan.embedding.identity != self._expected_embedding_identity
            or plan.embedding.dimensions != self._expected_embedding_dimensions
        ):
            raise CandidatePersistenceError("unsupported_embedding") from None
        if _plan_sha256(plan) != plan.plan_sha256:
            raise CandidatePersistenceError("invalid_plan") from None
        return plan

    async def _bounded(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        async with asyncio.timeout(self._timeout_seconds):
            return await operation()

    async def _read(
        self, operation: Callable[[], Awaitable[Any]]
    ) -> Any:
        for attempt in range(self._max_retries + 1):
            try:
                return await self._bounded(operation)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                if attempt == self._max_retries:
                    raise CandidatePersistenceError("store_unavailable") from None
            except CandidateStoreFailure as error:
                if error.code != "transient" or attempt == self._max_retries:
                    raise CandidatePersistenceError("store_unavailable") from None
            except Exception:
                raise CandidatePersistenceError("store_unavailable") from None
            await self._delay()
        raise AssertionError("unreachable")

    def _encoded_document_size(self, record: CandidateStoreRecord) -> int:
        try:
            size = self._store.encoded_document_size(record)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise CandidatePersistenceError("malformed_store") from None
        if type(size) is not int or size < 0:
            raise CandidatePersistenceError("malformed_store") from None
        fingerprint = (record.kind, record.key, repr(record.value))
        previous = self._document_size_cache.setdefault(fingerprint, size)
        if previous != size:
            raise CandidatePersistenceError("malformed_store") from None
        if size > FIRESTORE_DOCUMENT_MAX_BYTES:
            raise CandidatePersistenceError("store_bounds_exceeded") from None
        return size

    def _encoded_create_size(
        self, kind: CandidateRecordKind, records: tuple[CandidateStoreRecord, ...]
    ) -> int:
        try:
            size = self._store.encoded_create_size(kind, records)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise CandidatePersistenceError("malformed_store") from None
        if type(size) is not int or size < 0:
            raise CandidatePersistenceError("malformed_store") from None
        fingerprint = (
            kind,
            tuple((record.key, repr(record.value)) for record in records),
        )
        previous = self._batch_size_cache.setdefault(fingerprint, size)
        if previous != size:
            raise CandidatePersistenceError("malformed_store") from None
        return size

    async def _delay(self) -> None:
        try:
            await self._sleep(0.1)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise CandidatePersistenceError("store_unavailable") from None

    def _plan_batches(
        self, kind: CandidateRecordKind, records: tuple[CandidateStoreRecord, ...]
    ) -> tuple[tuple[CandidateStoreRecord, ...], ...]:
        batches: list[tuple[CandidateStoreRecord, ...]] = []
        current: tuple[CandidateStoreRecord, ...] = ()
        for record in records:
            self._encoded_document_size(record)
            proposed = current + (record,)
            proposed_size = self._encoded_create_size(kind, proposed)
            if (
                len(proposed) > CANDIDATE_WRITE_BATCH_MAX_RECORDS
                or proposed_size > CANDIDATE_WRITE_BATCH_MAX_BYTES
            ):
                if not current:
                    raise CandidatePersistenceError("store_bounds_exceeded") from None
                batches.append(current)
                proposed = (record,)
                if self._encoded_create_size(kind, proposed) > CANDIDATE_WRITE_BATCH_MAX_BYTES:
                    raise CandidatePersistenceError("store_bounds_exceeded") from None
            current = proposed
        if current:
            batches.append(current)
        return tuple(batches)

    def _compare_existing(
        self, raw: object, expected: CandidateStoreRecord
    ) -> CandidateStoreRecord:
        record = _validated_record(raw)
        if not _records_equal(record, expected):
            raise CandidatePersistenceError("candidate_conflict") from None
        return record

    async def _get(self, expected: CandidateStoreRecord) -> CandidateStoreRecord | None:
        raw = await self._read(
            lambda: self._store.get(
                expected.kind, expected.key, timeout_seconds=self._timeout_seconds
            )
        )
        if raw is None:
            return None
        return self._compare_existing(raw, expected)

    async def _get_many(
        self, kind: CandidateRecordKind, records: tuple[CandidateStoreRecord, ...]
    ) -> tuple[CandidateStoreRecord | None, ...]:
        raw = await self._read(
            lambda: self._store.get_many(
                kind,
                tuple(record.key for record in records),
                timeout_seconds=self._timeout_seconds,
            )
        )
        if type(raw) is not tuple or len(raw) != len(records):
            raise CandidatePersistenceError("malformed_store") from None
        validated: list[CandidateStoreRecord | None] = []
        for item, expected in zip(raw, records, strict=True):
            validated.append(None if item is None else self._compare_existing(item, expected))
        return tuple(validated)

    async def _create_once(
        self, kind: CandidateRecordKind, records: tuple[CandidateStoreRecord, ...]
    ) -> bool:
        if self._encoded_create_size(kind, records) > CANDIDATE_WRITE_BATCH_MAX_BYTES:
            raise CandidatePersistenceError("store_bounds_exceeded") from None
        try:
            await self._bounded(
                lambda: self._store.create_many(
                    kind, records, timeout_seconds=self._timeout_seconds
                )
            )
            return True
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return False
        except CandidateStoreFailure as error:
            if error.code == "permanent":
                raise CandidatePersistenceError("store_unavailable") from None
            return False
        except Exception:
            raise CandidatePersistenceError("store_unavailable") from None

    async def _create_with_resolution(
        self, kind: CandidateRecordKind, records: tuple[CandidateStoreRecord, ...]
    ) -> bool:
        if not records:
            return False
        if await self._create_once(kind, records):
            return True
        reread = await self._get_many(kind, records)
        missing = tuple(
            record for record, existing in zip(records, reread, strict=True) if existing is None
        )
        if not missing:
            return False
        if self._max_retries == 0:
            raise CandidatePersistenceError("store_unavailable") from None
        await self._delay()
        if await self._create_once(kind, missing):
            return True
        final = await self._get_many(kind, missing)
        if any(item is None for item in final):
            raise CandidatePersistenceError("store_unavailable") from None
        return False

    async def _create_header(self, header: CandidateStoreRecord) -> bool:
        """Return true only when the first header create has a known success."""
        if await self._create_once("candidate", (header,)):
            return True
        reread = await self._get_many("candidate", (header,))
        if reread[0] is not None:
            return False
        if self._max_retries == 0:
            raise CandidatePersistenceError("store_unavailable") from None
        await self._delay()
        if await self._create_once("candidate", (header,)):
            return False
        final = await self._get_many("candidate", (header,))
        if final[0] is None:
            raise CandidatePersistenceError("store_unavailable") from None
        return False

    async def _create_or_confirm_batch(
        self, kind: CandidateRecordKind, records: tuple[CandidateStoreRecord, ...]
    ) -> bool:
        existing = await self._get_many(kind, records)
        missing = tuple(
            record for record, item in zip(records, existing, strict=True) if item is None
        )
        return await self._create_with_resolution(kind, missing)

    async def _list_exact(
        self,
        kind: Literal["document", "chunk"],
        corpus: ExactCorpusReference,
        expected: tuple[CandidateStoreRecord, ...],
    ) -> tuple[CandidateStoreRecord, ...]:
        after: str | None = None
        gathered: list[CandidateStoreRecord] = []
        maximum_calls = max(1, math.ceil((len(expected) + 1) / CANDIDATE_READ_PAGE_SIZE))
        for _ in range(maximum_calls):
            raw = await self._read(
                partial(
                    self._store.list_page,
                    kind,
                    corpus,
                    after,
                    CANDIDATE_READ_PAGE_SIZE,
                    timeout_seconds=self._timeout_seconds,
                )
            )
            try:
                if type(raw) is not CandidateStorePage:
                    raise TypeError
                page = CandidateStorePage.model_validate(
                    {name: getattr(raw, name) for name in CandidateStorePage.model_fields}
                )
            except Exception:
                raise CandidatePersistenceError("malformed_store") from None
            records = tuple(_validated_record(item) for item in page.records)
            if len(records) > CANDIDATE_READ_PAGE_SIZE:
                raise CandidatePersistenceError("malformed_store") from None
            keys = tuple(record.key for record in records)
            if (
                any(record.kind != kind for record in records)
                or keys != tuple(sorted(keys, key=lambda key: key.encode("utf-8")))
                or len(keys) != len(set(keys))
                or (after is not None and any(key.encode() <= after.encode() for key in keys))
            ):
                raise CandidatePersistenceError("malformed_store") from None
            if page.next_after_key is not None:
                if (
                    not records
                    or len(records) != CANDIDATE_READ_PAGE_SIZE
                    or page.next_after_key != records[-1].key
                    or page.next_after_key == after
                ):
                    raise CandidatePersistenceError("malformed_store") from None
            gathered.extend(records)
            if page.next_after_key is None:
                break
            after = page.next_after_key
        else:
            raise CandidatePersistenceError("malformed_store") from None
        if len(gathered) != len(expected):
            raise CandidatePersistenceError("candidate_conflict") from None
        for actual, wanted in zip(gathered, expected, strict=True):
            self._compare_existing(actual, wanted)
        return tuple(gathered)

    async def _readback(
        self,
        plan: CandidateIngestionPlan,
        header: CandidateStoreRecord,
        documents: tuple[CandidateStoreRecord, ...],
        chunks: tuple[CandidateStoreRecord, ...],
    ) -> tuple[CandidateStoreRecord, ...]:
        if await self._get(header) is None:
            raise CandidatePersistenceError("candidate_conflict") from None
        read_documents = await self._list_exact("document", plan.corpus, documents)
        read_chunks = await self._list_exact("chunk", plan.corpus, chunks)
        planned_chunks = {
            chunk.chunk_id: chunk
            for document in plan.documents
            for chunk in document.chunks
        }
        for record in read_chunks:
            text = record.value["text"]
            embedding = record.value["embedding"]
            chunk_id = record.value["chunk_id"]
            if (
                type(text) is not str
                or type(chunk_id) is not str
                or not isinstance(embedding, tuple)
                or chunk_id not in planned_chunks
                or _text_sha256(text) != planned_chunks[chunk_id].text_sha256
                or _embedding_sha256(cast(tuple[float, ...], embedding))
                != planned_chunks[chunk_id].embedding_sha256
            ):
                raise CandidatePersistenceError("candidate_conflict") from None
        return (header, *read_documents, *read_chunks)

    async def _verify_attestation(
        self,
        raw_record: object,
        plan: CandidateIngestionPlan,
        payload: AttestationPayload,
        verifier: AttestationVerifier,
    ) -> tuple[CandidateStoreRecord, str]:
        try:
            record = _validated_record(raw_record)
            if record.kind != "attestation" or record.key != candidate_store_key(plan.corpus):
                raise ValueError
            envelope = CandidateAttestation.model_validate(_plain_store_value(record.value))
            expected_payload = _payload_mapping(payload)
            if envelope.payload.model_dump(mode="json") != expected_payload:
                raise ValueError
            payload_bytes = canonical_json_bytes(expected_payload)
            payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
            if envelope.payload_sha256 != payload_sha256:
                raise ValueError
            encoded = envelope.signature_b64url
            if "=" in encoded or not encoded.isascii():
                raise ValueError
            padding = "=" * ((4 - len(encoded) % 4) % 4)
            signature = base64.b64decode(
                encoded + padding, altchars=b"-_", validate=True
            )
            if (
                not MIN_SIGNATURE_BYTES <= len(signature) <= MAX_SIGNATURE_BYTES
                or base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
                != encoded
            ):
                raise ValueError
            verified = await verifier.verify(payload_bytes, signature)
            if verified is not True:
                raise ValueError
            return record, payload_sha256
        except asyncio.CancelledError:
            raise
        except Exception:
            raise CandidatePersistenceError("attestation_failed") from None

    def _receipt(
        self,
        plan: CandidateIngestionPlan,
        disposition: CandidateDisposition,
        inventory_sha256: str,
        payload_sha256: str,
    ) -> CandidatePersistenceReceipt:
        return CandidatePersistenceReceipt(
            contract_version=CANDIDATE_PERSISTENCE_CONTRACT_VERSION,
            corpus=plan.corpus,
            plan_sha256=plan.plan_sha256,
            disposition=disposition,
            document_count=plan.document_count,
            chunk_count=plan.chunk_count,
            inventory_sha256=inventory_sha256,
            attestation_payload_sha256=payload_sha256,
        )

    async def verify_attested_candidate(
        self,
        request: CandidatePersistenceRequest,
        *,
        verifier: AttestationVerifier,
    ) -> CandidatePersistenceReceipt:
        """Read and verify one complete candidate without signing or writing."""
        plan = self._validate_request(request)
        _signer_identity(verifier)
        header, documents, chunks = expected_candidate_records(plan)
        records = await self._readback(plan, header, documents, chunks)
        inventory_sha256 = candidate_inventory_sha256(records)
        payload = _attestation_payload(plan, inventory_sha256, verifier)
        raw = await self._read(
            lambda: self._store.get(
                "attestation", header.key, timeout_seconds=self._timeout_seconds
            )
        )
        if raw is None:
            raise CandidatePersistenceError("attestation_failed") from None
        _, payload_sha256 = await self._verify_attestation(
            raw, plan, payload, verifier
        )
        return self._receipt(
            plan, "confirmed", inventory_sha256, payload_sha256
        )

    async def persist_and_attest_candidate(
        self, request: CandidatePersistenceRequest
    ) -> CandidatePersistenceReceipt:
        plan = self._validate_request(request)
        _signer_identity(self._signer)
        header, documents, chunks = expected_candidate_records(plan)
        header_batch = self._plan_batches("candidate", (header,))
        document_batches = self._plan_batches("document", documents)
        chunk_batches = self._plan_batches("chunk", chunks)

        initial_header = await self._get(header)
        initial_header_present = initial_header is not None
        header_created = False
        known_later_create = False
        if initial_header is None:
            header_created = await self._create_header(header_batch[0][0])
            if await self._get(header) is None:
                raise CandidatePersistenceError("store_unavailable") from None
        for batch in document_batches:
            known_later_create |= await self._create_or_confirm_batch("document", batch)
        for batch in chunk_batches:
            known_later_create |= await self._create_or_confirm_batch("chunk", batch)

        records = await self._readback(plan, header, documents, chunks)
        inventory_sha256 = candidate_inventory_sha256(records)
        payload = _attestation_payload(plan, inventory_sha256, self._signer)
        payload_bytes = canonical_json_bytes(_payload_mapping(payload))
        raw_attestation = await self._read(
            lambda: self._store.get(
                "attestation", header.key, timeout_seconds=self._timeout_seconds
            )
        )
        if raw_attestation is None:
            try:
                signature = await self._signer.sign(payload_bytes)
                if type(signature) is not bytes or not MIN_SIGNATURE_BYTES <= len(
                    signature
                ) <= MAX_SIGNATURE_BYTES:
                    raise ValueError
                verified = await self._signer.verify(payload_bytes, signature)
                if verified is not True:
                    raise ValueError
            except asyncio.CancelledError:
                raise
            except Exception:
                raise CandidatePersistenceError("attestation_failed") from None
            proposed = _attestation_record(plan, payload, signature)
            self._plan_batches("attestation", (proposed,))
            created = await self._create_once("attestation", (proposed,))
            known_later_create |= created
            raw_attestation = await self._read(
                lambda: self._store.get(
                    "attestation", header.key, timeout_seconds=self._timeout_seconds
                )
            )
            if raw_attestation is None and not created and self._max_retries == 1:
                await self._delay()
                created = await self._create_once("attestation", (proposed,))
                known_later_create |= created
                raw_attestation = await self._read(
                    lambda: self._store.get(
                        "attestation", header.key, timeout_seconds=self._timeout_seconds
                    )
                )
            if raw_attestation is None:
                raise CandidatePersistenceError("store_unavailable") from None
        _, payload_sha256 = await self._verify_attestation(
            raw_attestation, plan, payload, self._signer
        )
        disposition: CandidateDisposition
        if header_created:
            disposition = "created"
        elif initial_header_present and known_later_create:
            disposition = "resumed"
        else:
            disposition = "confirmed"
        return self._receipt(
            plan, disposition, inventory_sha256, payload_sha256
        )

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            if self._owns_store:
                try:
                    await self._store.aclose()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
