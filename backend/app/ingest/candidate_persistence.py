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
from dataclasses import dataclass
from datetime import date
from functools import partial
from types import MappingProxyType
from typing import Annotated, Any, Literal, Protocol, Self, cast
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetCoreSchemaHandler,
    StrictInt,
    ValidationInfo,
    field_serializer,
    field_validator,
)
from pydantic_core import CoreSchema, core_schema

from app.ingest.planner import (
    MAX_CHUNKS_PER_DOCUMENT,
    MAX_DOCUMENTS,
    MAX_EMBEDDING_IDENTITY_CHARS,
    MAX_PROVENANCE_OWNER_CHARS,
    MAX_TOTAL_CHUNKS,
    CandidateIngestionPlan,
    EmbeddingSpecification,
    PlannedChunk,
    PlannedDocument,
    PlannedProvenance,
    _embedding_sha256,
    _plan_sha256,
    _text_sha256,
)
from app.ingest.provenance import MAX_CITATION_TITLE_CHARS, MAX_MANIFEST_BYTES
from app.retrieval_contracts import (
    MAX_RETRIEVAL_DOCUMENT_CHARS,
    MAX_RETRIEVED_CHUNK_CHARS,
    ExactCorpusReference,
)
from app.retrieval_firestore import (
    FIRESTORE_RECORD_SCHEMA_VERSION,
    firestore_chunk_document_id,
)

CANDIDATE_PERSISTENCE_CONTRACT_VERSION: Literal["1.0"] = "1.0"
CANDIDATE_RECORD_SCHEMA_VERSION: Literal["1.0"] = "1.0"
DOCUMENT_RECORD_SCHEMA_VERSION: Literal["1.0"] = "1.0"
ATTESTATION_CONTRACT_VERSION: Literal["1.0"] = "1.0"
ATTESTATION_RECORD_SCHEMA_VERSION: Literal["1.0"] = "1.0"
INVENTORY_ALGORITHM: Literal["cairn-candidate-inventory-v1"] = "cairn-candidate-inventory-v1"
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


class _CandidateStoreMalformed(Exception):
    """Private content-free malformed-store response classification."""

    def __init__(self) -> None:
        super().__init__("Candidate store returned malformed data.")

    def __repr__(self) -> str:
        return "_CandidateStoreMalformed()"


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
        revalidate_instances="always",
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
    def model_construct(cls, _fields_set: set[str] | None = None, **values: Any) -> Self:
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

    @classmethod
    def construct(cls, _fields_set: set[str] | None = None, **values: Any) -> Self:
        return cls.model_construct(_fields_set=_fields_set, **values)

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        try:
            source = super().model_copy(deep=deep)
            values = {name: getattr(source, name) for name in type(self).model_fields}
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

    def __replace__(self, **changes: Any) -> Self:
        return self.model_copy(update=changes)


def _revalidate_nested_model(
    value: object, model: type[CandidatePersistenceModel]
) -> CandidatePersistenceModel:
    try:
        if type(value) is model:
            if object.__getattribute__(value, "__pydantic_extra__"):
                raise ValueError
            value = {name: getattr(value, name) for name in model.model_fields}
        elif isinstance(value, Mapping):
            value = dict(value)
        else:
            raise TypeError
        return model.model_validate(value)
    except CandidatePersistenceError:
        raise
    except Exception:
        raise CandidatePersistenceError("invalid_plan") from None


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


def _json_arrays_to_tuples(value: object) -> object:
    if isinstance(value, list):
        return tuple(_json_arrays_to_tuples(item) for item in value)
    if isinstance(value, Mapping):
        return {key: _json_arrays_to_tuples(item) for key, item in value.items()}
    return value


def _revalidate_plan(
    value: object, *, validation_mode: Literal["python", "json"] = "python"
) -> CandidateIngestionPlan:
    try:
        if type(value) is CandidateIngestionPlan:
            if object.__getattribute__(value, "__pydantic_extra__"):
                raise ValueError
            value = {name: getattr(value, name) for name in CandidateIngestionPlan.model_fields}
        elif isinstance(value, Mapping):
            value = dict(value)
        else:
            raise TypeError
        if validation_mode == "json":
            return CandidateIngestionPlan.model_validate_json(
                json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            )
        return CandidateIngestionPlan.model_validate(value)
    except Exception:
        raise CandidatePersistenceError("invalid_plan") from None


def _revalidate_corpus(value: object) -> ExactCorpusReference:
    try:
        if type(value) is ExactCorpusReference:
            if object.__getattribute__(value, "__pydantic_extra__"):
                raise ValueError
            value = {name: getattr(value, name) for name in ExactCorpusReference.model_fields}
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
    def validate_plan(cls, value: object, info: ValidationInfo) -> CandidateIngestionPlan:
        return _revalidate_plan(value, validation_mode=info.mode)


class CandidatePersistenceReceipt(CandidatePersistenceModel):
    contract_version: Literal["1.0"]
    corpus: ExactCorpusReference
    plan_sha256: Annotated[str, Field(min_length=64, max_length=64)]
    disposition: CandidateDisposition
    document_count: Annotated[StrictInt, Field(ge=1, le=MAX_DOCUMENTS)]
    chunk_count: Annotated[StrictInt, Field(ge=1, le=MAX_TOTAL_CHUNKS)]
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
    def validate_value_before(
        cls, value: object, info: ValidationInfo
    ) -> Mapping[str, StrictStoreValue]:
        if info.mode == "json":
            value = _json_arrays_to_tuples(value)
        return _freeze_store_mapping(value)

    @field_validator("value")
    @classmethod
    def validate_value_after(
        cls, value: Mapping[str, StrictStoreValue]
    ) -> Mapping[str, StrictStoreValue]:
        return _freeze_store_mapping(value)

    @field_serializer("value")
    def serialize_value(self, value: Mapping[str, StrictStoreValue]) -> object:
        return _plain_store_value(value)


class CandidateStorePage(CandidatePersistenceModel):
    records: Annotated[tuple[CandidateStoreRecord, ...], Field(max_length=200)]
    next_after_key: Annotated[str, Field(min_length=1, max_length=256)] | None

    @field_validator("records", mode="before")
    @classmethod
    def validate_records(
        cls, value: object, info: ValidationInfo
    ) -> tuple[CandidateStoreRecord, ...]:
        try:
            if type(value) is list and info.mode == "json":
                value = tuple(value)
            if type(value) is not tuple:
                raise TypeError
            return tuple(
                cast(
                    CandidateStoreRecord,
                    _revalidate_nested_model(item, CandidateStoreRecord),
                )
                for item in value
            )
        except CandidatePersistenceError:
            raise
        except Exception:
            raise CandidatePersistenceError("invalid_plan") from None

    @field_validator("next_after_key")
    @classmethod
    def validate_cursor(cls, value: str | None) -> str | None:
        if value is not None and (
            not value.isascii() or any(ord(char) < 0x21 or ord(char) > 0x7E for char in value)
        ):
            raise ValueError
        return value


class AttestationCorpus(CandidatePersistenceModel):
    kind: Literal["exact"]
    corpus_id: str
    corpus_version: str

    @field_validator("corpus_id", "corpus_version")
    @classmethod
    def validate_exact_corpus_fields(cls, value: str, info: Any) -> str:
        values = {
            "kind": "exact",
            "corpus_id": value if info.field_name == "corpus_id" else "placeholder",
            "corpus_version": value if info.field_name == "corpus_version" else "1",
        }
        try:
            exact = ExactCorpusReference.model_validate(values)
        except Exception:
            raise CandidatePersistenceError("invalid_plan") from None
        return cast(str, getattr(exact, info.field_name))


class AttestationEmbedding(CandidatePersistenceModel):
    identity: Annotated[str, Field(min_length=1, max_length=MAX_EMBEDDING_IDENTITY_CHARS)]
    dimensions: Annotated[StrictInt, Field(ge=1, le=2048)]

    @field_validator("identity")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        if (
            value != value.strip()
            or not value.isascii()
            or any(ord(char) < 0x20 or ord(char) > 0x7E for char in value)
        ):
            raise ValueError
        return value


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
    document_count: Annotated[StrictInt, Field(ge=1, le=MAX_DOCUMENTS)]
    chunk_count: Annotated[StrictInt, Field(ge=1, le=MAX_TOTAL_CHUNKS)]
    record_schemas: AttestationRecordSchemas
    inventory_algorithm: Literal["cairn-candidate-inventory-v1"]
    inventory_sha256: Annotated[str, Field(min_length=64, max_length=64)]
    signature_algorithm_id: Annotated[str, Field(min_length=1, max_length=MAX_SIGNER_ID_CHARS)]
    signing_key_id: Annotated[str, Field(min_length=1, max_length=MAX_SIGNER_ID_CHARS)]

    @field_validator("corpus", mode="before")
    @classmethod
    def validate_attestation_corpus(cls, value: object) -> AttestationCorpus:
        return cast(
            AttestationCorpus,
            _revalidate_nested_model(value, AttestationCorpus),
        )

    @field_validator("embedding", mode="before")
    @classmethod
    def validate_attestation_embedding(cls, value: object) -> AttestationEmbedding:
        return cast(
            AttestationEmbedding,
            _revalidate_nested_model(value, AttestationEmbedding),
        )

    @field_validator("record_schemas", mode="before")
    @classmethod
    def validate_attestation_record_schemas(cls, value: object) -> AttestationRecordSchemas:
        return cast(
            AttestationRecordSchemas,
            _revalidate_nested_model(value, AttestationRecordSchemas),
        )

    @field_validator("plan_sha256", "semantic_manifest_sha256", "inventory_sha256")
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

    @field_validator("payload", mode="before")
    @classmethod
    def validate_payload(cls, value: object) -> AttestationPayload:
        return cast(
            AttestationPayload,
            _revalidate_nested_model(value, AttestationPayload),
        )

    @field_validator("payload_sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if any(character not in "0123456789abcdef" for character in value):
            raise ValueError
        return value


class AttestationIdentity(CandidatePersistenceModel):
    algorithm_id: Annotated[str, Field(min_length=1, max_length=MAX_SIGNER_ID_CHARS)]
    key_id: Annotated[str, Field(min_length=1, max_length=MAX_SIGNER_ID_CHARS)]

    @field_validator("algorithm_id", "key_id")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        if (
            value != value.strip()
            or not value.isascii()
            or any(ord(char) < 0x20 or ord(char) > 0x7E for char in value)
        ):
            raise ValueError
        return value


class VerifiedCandidateEvidence(CandidatePersistenceModel):
    corpus: ExactCorpusReference
    plan_sha256: Annotated[str, Field(min_length=64, max_length=64)]
    semantic_manifest_sha256: Annotated[str, Field(min_length=64, max_length=64)]
    embedding_identity: Annotated[str, Field(min_length=1, max_length=MAX_EMBEDDING_IDENTITY_CHARS)]
    embedding_dimensions: Annotated[StrictInt, Field(ge=1, le=2048)]
    document_count: Annotated[StrictInt, Field(ge=1, le=MAX_DOCUMENTS)]
    chunk_count: Annotated[StrictInt, Field(ge=1, le=MAX_TOTAL_CHUNKS)]
    inventory_sha256: Annotated[str, Field(min_length=64, max_length=64)]
    attestation_payload_sha256: Annotated[str, Field(min_length=64, max_length=64)]
    signature_algorithm_id: Annotated[str, Field(min_length=1, max_length=MAX_SIGNER_ID_CHARS)]
    signing_key_id: Annotated[str, Field(min_length=1, max_length=MAX_SIGNER_ID_CHARS)]

    @field_validator("corpus", mode="before")
    @classmethod
    def validate_corpus(cls, value: object) -> ExactCorpusReference:
        return _revalidate_corpus(value)

    @field_validator(
        "plan_sha256",
        "semantic_manifest_sha256",
        "inventory_sha256",
        "attestation_payload_sha256",
    )
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if any(character not in "0123456789abcdef" for character in value):
            raise ValueError
        return value

    @field_validator("embedding_identity", "signature_algorithm_id", "signing_key_id")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        if (
            value != value.strip()
            or not value.isascii()
            or any(ord(char) < 0x20 or ord(char) > 0x7E for char in value)
        ):
            raise ValueError
        return value


class _DurableCandidateHeader(CandidatePersistenceModel):
    schema_version: Literal["1.0"]
    plan_contract_version: Literal["1.0"]
    corpus_id: str
    corpus_version: str
    plan_sha256: Annotated[str, Field(min_length=64, max_length=64)]
    semantic_manifest_sha256: Annotated[str, Field(min_length=64, max_length=64)]
    embedding_identity: Annotated[str, Field(min_length=1, max_length=MAX_EMBEDDING_IDENTITY_CHARS)]
    embedding_dimensions: Annotated[StrictInt, Field(ge=1, le=2048)]
    document_count: Annotated[StrictInt, Field(ge=1, le=MAX_DOCUMENTS)]
    chunk_count: Annotated[StrictInt, Field(ge=1, le=MAX_TOTAL_CHUNKS)]

    @field_validator("plan_sha256", "semantic_manifest_sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if any(character not in "0123456789abcdef" for character in value):
            raise ValueError
        return value

    @field_validator("embedding_identity")
    @classmethod
    def validate_embedding_identity(cls, value: str) -> str:
        if (
            value != value.strip()
            or not value.isascii()
            or any(ord(char) < 0x20 or ord(char) > 0x7E for char in value)
        ):
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


class _LinearCandidateStore(CandidateStore, Protocol):
    def encoded_create_base_size(self, kind: CandidateRecordKind) -> int: ...

    def encoded_record_sizes(self, record: CandidateStoreRecord) -> tuple[int, int, str]: ...

    async def create_many_checked(
        self,
        kind: CandidateRecordKind,
        records: tuple[CandidateStoreRecord, ...],
        *,
        expected_encoded_size: int,
        expected_write_sha256s: tuple[str, ...],
        timeout_seconds: int,
    ) -> None: ...


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
        return type(left) is float and type(right) is float and left.hex() == right.hex()
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
    identity: object = _MISSING
    try:
        algorithm_id = verifier.algorithm_id
        key_id = verifier.key_id
        identity = (algorithm_id, key_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    if identity is _MISSING:
        raise CandidatePersistenceError("attestation_failed") from None
    raw_algorithm_id, raw_key_id = cast(tuple[object, object], identity)
    for value in (raw_algorithm_id, raw_key_id):
        if (
            type(value) is not str
            or not 1 <= len(value) <= MAX_SIGNER_ID_CHARS
            or value != value.strip()
            or not value.isascii()
            or any(ord(char) < 0x20 or ord(char) > 0x7E for char in value)
        ):
            raise CandidatePersistenceError("attestation_failed") from None
    return cast(str, raw_algorithm_id), cast(str, raw_key_id)


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


async def _read_with_policy(
    operation: Callable[[], Awaitable[Any]],
    *,
    timeout_seconds: int,
    max_retries: int,
    sleep: Callable[[float], Awaitable[None]],
) -> Any:
    for attempt in range(max_retries + 1):
        failure_code: CandidatePersistenceErrorCode | None = None
        should_retry = False
        try:
            async with asyncio.timeout(timeout_seconds):
                return await operation()
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            if attempt == max_retries:
                failure_code = "store_unavailable"
            else:
                should_retry = True
        except _CandidateStoreMalformed:
            failure_code = "malformed_store"
        except CandidateStoreFailure as error:
            if error.code != "transient" or attempt == max_retries:
                failure_code = "store_unavailable"
            else:
                should_retry = True
        except Exception:
            failure_code = "store_unavailable"
        if failure_code is not None:
            raise CandidatePersistenceError(failure_code) from None
        if not should_retry:
            raise AssertionError("unreachable")
        sleep_failed = False
        try:
            await sleep(0.1)
        except asyncio.CancelledError:
            raise
        except Exception:
            sleep_failed = True
        if sleep_failed:
            raise CandidatePersistenceError("store_unavailable") from None
    raise AssertionError("unreachable")


async def _list_exact_records(
    *,
    store: CandidateStore,
    read: Callable[[Callable[[], Awaitable[Any]]], Awaitable[Any]],
    kind: Literal["document", "chunk"],
    corpus: ExactCorpusReference,
    expected_count: int,
    timeout_seconds: int,
) -> tuple[CandidateStoreRecord, ...]:
    after: str | None = None
    gathered: list[CandidateStoreRecord] = []
    maximum_calls = max(1, math.ceil(expected_count / CANDIDATE_READ_PAGE_SIZE))
    for _ in range(maximum_calls):
        raw = await read(
            partial(
                store.list_page,
                kind,
                corpus,
                after,
                CANDIDATE_READ_PAGE_SIZE,
                timeout_seconds=timeout_seconds,
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
        keys = tuple(record.key for record in records)
        if (
            any(record.kind != kind for record in records)
            or keys != tuple(sorted(keys, key=lambda key: key.encode("utf-8")))
            or len(keys) != len(set(keys))
            or (after is not None and any(key.encode() <= after.encode() for key in keys))
        ):
            raise CandidatePersistenceError("malformed_store") from None
        if expected_count == 0:
            if records or page.next_after_key is not None:
                raise CandidatePersistenceError("candidate_conflict") from None
            return ()
        remaining = expected_count - len(gathered)
        if remaining <= 0 or len(records) > remaining:
            raise CandidatePersistenceError("candidate_conflict") from None
        expect_cursor = remaining > CANDIDATE_READ_PAGE_SIZE
        if expect_cursor != (page.next_after_key is not None):
            raise CandidatePersistenceError("malformed_store") from None
        if page.next_after_key is not None:
            if (
                len(records) != CANDIDATE_READ_PAGE_SIZE
                or page.next_after_key != records[-1].key
                or page.next_after_key == after
            ):
                raise CandidatePersistenceError("malformed_store") from None
        elif len(records) != remaining:
            raise CandidatePersistenceError("candidate_conflict") from None
        gathered.extend(records)
        if page.next_after_key is None:
            break
        after = page.next_after_key
    else:
        raise CandidatePersistenceError("malformed_store") from None
    if len(gathered) != expected_count:
        raise CandidatePersistenceError("candidate_conflict") from None
    return tuple(gathered)


async def _verify_attestation_record(
    raw_record: object,
    *,
    corpus: ExactCorpusReference,
    payload: AttestationPayload,
    identity: AttestationIdentity,
    verifier: AttestationVerifier,
    bounded: Callable[[Callable[[], Awaitable[Any]]], Awaitable[Any]],
) -> tuple[CandidateStoreRecord, str]:
    failure = False
    try:
        record = _validated_record(raw_record)
        if record.kind != "attestation" or record.key != candidate_store_key(corpus):
            raise ValueError
        envelope = CandidateAttestation.model_validate(_plain_store_value(record.value))
        expected_payload = _payload_mapping(payload)
        if (
            envelope.payload.model_dump(mode="json") != expected_payload
            or envelope.payload.signature_algorithm_id != identity.algorithm_id
            or envelope.payload.signing_key_id != identity.key_id
        ):
            raise ValueError
        payload_bytes = canonical_json_bytes(expected_payload)
        payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
        if envelope.payload_sha256 != payload_sha256:
            raise ValueError
        encoded = envelope.signature_b64url
        if "=" in encoded or not encoded.isascii():
            raise ValueError
        padding = "=" * ((4 - len(encoded) % 4) % 4)
        signature = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
        if (
            not MIN_SIGNATURE_BYTES <= len(signature) <= MAX_SIGNATURE_BYTES
            or base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii") != encoded
        ):
            raise ValueError
        if _signer_identity(verifier) != (identity.algorithm_id, identity.key_id):
            raise ValueError
        verified = await bounded(partial(verifier.verify, payload_bytes, signature))
        if verified is not True:
            raise ValueError
        if _signer_identity(verifier) != (identity.algorithm_id, identity.key_id):
            raise ValueError
        return record, payload_sha256
    except asyncio.CancelledError:
        raise
    except Exception:
        failure = True
    if failure:
        raise CandidatePersistenceError("attestation_failed") from None
    raise AssertionError("unreachable")


def _parse_durable_header(
    raw: object, corpus: ExactCorpusReference
) -> tuple[CandidateStoreRecord, _DurableCandidateHeader]:
    try:
        record = _validated_record(raw)
        if record.kind != "candidate" or record.key != candidate_store_key(corpus):
            raise ValueError
        header = _DurableCandidateHeader.model_validate(_plain_store_value(record.value))
        if header.corpus_id != corpus.corpus_id or header.corpus_version != corpus.corpus_version:
            raise ValueError
        return record, header
    except Exception:
        raise CandidatePersistenceError("malformed_store") from None


def _valid_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _bounded_text(value: object, maximum: int) -> bool:
    return (
        type(value) is str
        and 1 <= len(value) <= maximum
        and bool(value.strip())
        and all(ord(character) >= 0x20 or character in "\n\t" for character in value)
    )


def _validate_durable_records(
    header: _DurableCandidateHeader,
    corpus: ExactCorpusReference,
    documents: tuple[CandidateStoreRecord, ...],
    chunks: tuple[CandidateStoreRecord, ...],
) -> None:
    document_fields = {
        "schema_version",
        "plan_contract_version",
        "corpus_id",
        "corpus_version",
        "plan_sha256",
        "document_id",
        "relative_path",
        "parser_id",
        "normalization_id",
        "chunking_id",
        "normalized_text_sha256",
        "document_plan_sha256",
        "chunk_count",
        "provenance",
    }
    provenance_fields = {"title", "url", "owner", "reviewed_at", "source_sha256"}
    chunk_fields = {
        "schema_version",
        "corpus_id",
        "corpus_version",
        "embedding_identity",
        "chunk_id",
        "document_id",
        "source",
        "chunk_index",
        "text",
        "citation_title",
        "citation_url",
        "embedding",
    }
    document_values: dict[str, Mapping[str, StrictStoreValue]] = {}
    expected_chunk_total = 0
    try:
        for record in documents:
            value = record.value
            provenance = value.get("provenance")
            document_id = value.get("document_id")
            chunk_count = value.get("chunk_count")
            if (
                set(value) != document_fields
                or record.kind != "document"
                or type(document_id) is not str
                or record.key != document_id
                or len(document_id) != 68
                or not document_id.startswith("doc_")
                or not _valid_sha256(document_id[4:])
                or value.get("schema_version") != DOCUMENT_RECORD_SCHEMA_VERSION
                or value.get("plan_contract_version") != "1.0"
                or value.get("corpus_id") != corpus.corpus_id
                or value.get("corpus_version") != corpus.corpus_version
                or value.get("plan_sha256") != header.plan_sha256
                or not _bounded_text(value.get("relative_path"), MAX_RETRIEVAL_DOCUMENT_CHARS)
                or not _bounded_text(value.get("parser_id"), 256)
                or not _bounded_text(value.get("normalization_id"), 256)
                or not _bounded_text(value.get("chunking_id"), 256)
                or not _valid_sha256(value.get("normalized_text_sha256"))
                or not _valid_sha256(value.get("document_plan_sha256"))
                or type(chunk_count) is not int
                or not 1 <= chunk_count <= MAX_CHUNKS_PER_DOCUMENT
                or not isinstance(provenance, Mapping)
                or set(provenance) != provenance_fields
                or not _bounded_text(provenance.get("title"), MAX_CITATION_TITLE_CHARS)
                or not _bounded_text(provenance.get("owner"), MAX_PROVENANCE_OWNER_CHARS)
                or not _bounded_text(provenance.get("url"), MAX_MANIFEST_BYTES)
                or not _valid_sha256(provenance.get("source_sha256"))
                or type(provenance.get("reviewed_at")) is not str
            ):
                raise ValueError
            parsed_url = urlsplit(cast(str, provenance["url"]))
            _ = parsed_url.port
            if (
                parsed_url.scheme not in {"http", "https"}
                or parsed_url.hostname is None
                or parsed_url.username is not None
                or parsed_url.password is not None
                or any(character.isspace() for character in cast(str, provenance["url"]))
            ):
                raise ValueError
            reviewed_at = cast(str, provenance["reviewed_at"])
            parsed_reviewed_at = date.fromisoformat(reviewed_at)
            if parsed_reviewed_at.isoformat() != reviewed_at:
                raise ValueError
            document_values[document_id] = value
            expected_chunk_total += chunk_count
        if expected_chunk_total != header.chunk_count:
            raise ValueError

        indices: dict[str, set[int]] = {document_id: set() for document_id in document_values}
        for record in chunks:
            value = record.value
            chunk_id = value.get("chunk_id")
            document_id = value.get("document_id")
            chunk_index = value.get("chunk_index")
            embedding = value.get("embedding")
            document = document_values.get(cast(str, document_id))
            provenance = None if document is None else document.get("provenance")
            if (
                set(value) != chunk_fields
                or record.kind != "chunk"
                or type(chunk_id) is not str
                or len(chunk_id) != 68
                or not chunk_id.startswith("chk_")
                or not _valid_sha256(chunk_id[4:])
                or record.key != firestore_chunk_document_id(chunk_id)
                or value.get("schema_version") != FIRESTORE_RECORD_SCHEMA_VERSION
                or value.get("corpus_id") != corpus.corpus_id
                or value.get("corpus_version") != corpus.corpus_version
                or value.get("embedding_identity") != header.embedding_identity
                or type(document_id) is not str
                or document is None
                or type(chunk_index) is not int
                or not 0 <= chunk_index < cast(int, document["chunk_count"])
                or not _bounded_text(value.get("source"), MAX_RETRIEVAL_DOCUMENT_CHARS)
                or value.get("source") != document.get("relative_path")
                or not _bounded_text(value.get("text"), MAX_RETRIEVED_CHUNK_CHARS)
                or not isinstance(provenance, Mapping)
                or value.get("citation_title") != provenance.get("title")
                or value.get("citation_url") != provenance.get("url")
                or not isinstance(embedding, tuple)
                or len(embedding) != header.embedding_dimensions
                or any(type(item) is not float or not math.isfinite(item) for item in embedding)
                or chunk_index in indices[document_id]
            ):
                raise ValueError
            indices[document_id].add(chunk_index)
        if any(
            values != set(range(cast(int, document_values[key]["chunk_count"])))
            for key, values in indices.items()
        ):
            raise ValueError
    except Exception:
        raise CandidatePersistenceError("candidate_conflict") from None


def _reconstruct_durable_plan(
    header: _DurableCandidateHeader,
    corpus: ExactCorpusReference,
    documents: tuple[CandidateStoreRecord, ...],
    chunks: tuple[CandidateStoreRecord, ...],
) -> CandidateIngestionPlan:
    try:
        chunks_by_document: dict[str, list[CandidateStoreRecord]] = {
            cast(str, record.value["document_id"]): [] for record in documents
        }
        for record in chunks:
            chunks_by_document[cast(str, record.value["document_id"])].append(record)

        planned_documents: list[PlannedDocument] = []
        for record in documents:
            value = record.value
            document_id = cast(str, value["document_id"])
            provenance = cast(Mapping[str, StrictStoreValue], value["provenance"])
            planned_chunks = tuple(
                PlannedChunk(
                    chunk_id=cast(str, chunk.value["chunk_id"]),
                    document_id=cast(str, chunk.value["document_id"]),
                    chunk_index=cast(int, chunk.value["chunk_index"]),
                    text=cast(str, chunk.value["text"]),
                    text_sha256=_text_sha256(cast(str, chunk.value["text"])),
                    embedding=cast(tuple[float, ...], chunk.value["embedding"]),
                    embedding_sha256=_embedding_sha256(
                        cast(tuple[float, ...], chunk.value["embedding"])
                    ),
                    citation_title=cast(str, chunk.value["citation_title"]),
                    citation_url=cast(str, chunk.value["citation_url"]),
                )
                for chunk in sorted(
                    chunks_by_document[document_id],
                    key=lambda item: cast(int, item.value["chunk_index"]),
                )
            )
            planned_documents.append(
                PlannedDocument(
                    document_id=document_id,
                    relative_path=cast(str, value["relative_path"]),
                    parser_id=cast(str, value["parser_id"]),
                    normalization_id=cast(Any, value["normalization_id"]),
                    chunking_id=cast(Any, value["chunking_id"]),
                    normalized_text_sha256=cast(str, value["normalized_text_sha256"]),
                    provenance=PlannedProvenance(
                        title=cast(str, provenance["title"]),
                        url=cast(str, provenance["url"]),
                        owner=cast(str, provenance["owner"]),
                        reviewed_at=date.fromisoformat(cast(str, provenance["reviewed_at"])),
                        source_sha256=cast(str, provenance["source_sha256"]),
                    ),
                    chunks=planned_chunks,
                    document_plan_sha256=cast(str, value["document_plan_sha256"]),
                )
            )
        ordered_documents = tuple(
            sorted(
                planned_documents,
                key=lambda document: document.relative_path.encode("utf-8"),
            )
        )
        return CandidateIngestionPlan(
            contract_version="1.0",
            corpus=corpus,
            embedding=EmbeddingSpecification(
                identity=header.embedding_identity,
                dimensions=header.embedding_dimensions,
            ),
            semantic_manifest_sha256=header.semantic_manifest_sha256,
            documents=ordered_documents,
            document_count=header.document_count,
            chunk_count=header.chunk_count,
            plan_sha256=header.plan_sha256,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        raise CandidatePersistenceError("attestation_failed") from None


def _payload_from_durable_header(
    header: _DurableCandidateHeader,
    corpus: ExactCorpusReference,
    identity: AttestationIdentity,
    inventory_sha256: str,
) -> AttestationPayload:
    return AttestationPayload(
        attestation_version=ATTESTATION_CONTRACT_VERSION,
        corpus=AttestationCorpus(
            kind="exact",
            corpus_id=corpus.corpus_id,
            corpus_version=corpus.corpus_version,
        ),
        plan_contract_version="1.0",
        plan_sha256=header.plan_sha256,
        semantic_manifest_sha256=header.semantic_manifest_sha256,
        embedding=AttestationEmbedding(
            identity=header.embedding_identity,
            dimensions=header.embedding_dimensions,
        ),
        document_count=header.document_count,
        chunk_count=header.chunk_count,
        record_schemas=AttestationRecordSchemas(
            candidate=CANDIDATE_RECORD_SCHEMA_VERSION,
            document=DOCUMENT_RECORD_SCHEMA_VERSION,
            chunk=cast(Literal["1.0"], FIRESTORE_RECORD_SCHEMA_VERSION),
        ),
        inventory_algorithm=INVENTORY_ALGORITHM,
        inventory_sha256=inventory_sha256,
        signature_algorithm_id=identity.algorithm_id,
        signing_key_id=identity.key_id,
    )


class CandidateAttestationVerificationService:
    """Signer-free read-only verification for one exact durable candidate."""

    def __init__(
        self,
        *,
        store: CandidateStore,
        timeout_seconds: int,
        max_retries: int,
        sleep: Callable[[float], Awaitable[None]],
        owns_store: bool = False,
    ) -> None:
        if (
            type(timeout_seconds) is not int
            or not 1 <= timeout_seconds <= 30
            or type(max_retries) is not int
            or max_retries not in {0, 1}
            or not callable(sleep)
            or type(owns_store) is not bool
        ):
            raise CandidatePersistenceError("invalid_plan") from None
        self._store = store
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._sleep = sleep
        self._owns_store = owns_store
        self._closed = False
        self._close_lock = asyncio.Lock()

    async def _read(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        if self._closed:
            raise CandidatePersistenceError("store_unavailable") from None
        return await _read_with_policy(
            operation,
            timeout_seconds=self._timeout_seconds,
            max_retries=self._max_retries,
            sleep=self._sleep,
        )

    async def _bounded(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        async with asyncio.timeout(self._timeout_seconds):
            return await operation()

    async def verify_attested_candidate(
        self,
        corpus: ExactCorpusReference,
        identity: AttestationIdentity,
        verifier: AttestationVerifier,
    ) -> VerifiedCandidateEvidence:
        corpus = _revalidate_corpus(corpus)
        identity = cast(
            AttestationIdentity,
            _revalidate_nested_model(identity, AttestationIdentity),
        )
        if _signer_identity(verifier) != (identity.algorithm_id, identity.key_id):
            raise CandidatePersistenceError("attestation_failed") from None
        key = candidate_store_key(corpus)
        raw_header = await self._read(
            lambda: self._store.get("candidate", key, timeout_seconds=self._timeout_seconds)
        )
        if raw_header is None:
            raise CandidatePersistenceError("attestation_failed") from None
        header_record, header = _parse_durable_header(raw_header, corpus)
        documents = await _list_exact_records(
            store=self._store,
            read=self._read,
            kind="document",
            corpus=corpus,
            expected_count=header.document_count,
            timeout_seconds=self._timeout_seconds,
        )
        chunks = await _list_exact_records(
            store=self._store,
            read=self._read,
            kind="chunk",
            corpus=corpus,
            expected_count=header.chunk_count,
            timeout_seconds=self._timeout_seconds,
        )
        _validate_durable_records(header, corpus, documents, chunks)
        _reconstruct_durable_plan(header, corpus, documents, chunks)
        inventory_sha256 = candidate_inventory_sha256((header_record, *documents, *chunks))
        payload = _payload_from_durable_header(header, corpus, identity, inventory_sha256)
        raw_attestation = await self._read(
            lambda: self._store.get("attestation", key, timeout_seconds=self._timeout_seconds)
        )
        if raw_attestation is None:
            raise CandidatePersistenceError("attestation_failed") from None
        _, payload_sha256 = await _verify_attestation_record(
            raw_attestation,
            corpus=corpus,
            payload=payload,
            identity=identity,
            verifier=verifier,
            bounded=self._bounded,
        )
        return VerifiedCandidateEvidence(
            corpus=corpus,
            plan_sha256=header.plan_sha256,
            semantic_manifest_sha256=header.semantic_manifest_sha256,
            embedding_identity=header.embedding_identity,
            embedding_dimensions=header.embedding_dimensions,
            document_count=header.document_count,
            chunk_count=header.chunk_count,
            inventory_sha256=inventory_sha256,
            attestation_payload_sha256=payload_sha256,
            signature_algorithm_id=identity.algorithm_id,
            signing_key_id=identity.key_id,
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


@dataclass(frozen=True, slots=True, repr=False)
class _CandidateWriteBatch:
    records: tuple[CandidateStoreRecord, ...]
    encoded_size: int
    base_size: int
    contributions: tuple[int, ...]
    write_sha256s: tuple[str, ...]

    def __repr__(self) -> str:
        return f"_CandidateWriteBatch(count={len(self.records)}, encoded_size={self.encoded_size})"

    def __len__(self) -> int:
        return len(self.records)

    def select(self, indices: tuple[int, ...]) -> "_CandidateWriteBatch":
        return _CandidateWriteBatch(
            records=tuple(self.records[index] for index in indices),
            encoded_size=self.base_size + sum(self.contributions[index] for index in indices),
            base_size=self.base_size,
            contributions=tuple(self.contributions[index] for index in indices),
            write_sha256s=tuple(self.write_sha256s[index] for index in indices),
        )


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

    def _validate_request(self, request: object) -> CandidateIngestionPlan:
        if self._closed:
            raise CandidatePersistenceError("store_unavailable") from None
        try:
            if type(request) is not CandidatePersistenceRequest:
                raise TypeError
            request = CandidatePersistenceRequest.model_validate(
                {name: getattr(request, name) for name in CandidatePersistenceRequest.model_fields}
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

    async def _read(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        return await _read_with_policy(
            operation,
            timeout_seconds=self._timeout_seconds,
            max_retries=self._max_retries,
            sleep=self._sleep,
        )

    def _linear_store(self) -> _LinearCandidateStore | None:
        try:
            if all(
                callable(getattr(self._store, name, None))
                for name in (
                    "encoded_create_base_size",
                    "encoded_record_sizes",
                    "create_many_checked",
                )
            ):
                return cast(_LinearCandidateStore, self._store)
        except Exception:
            raise CandidatePersistenceError("malformed_store") from None
        return None

    def _encoded_create_base_size(self, kind: CandidateRecordKind) -> int:
        try:
            linear_store = self._linear_store()
            size = (
                linear_store.encoded_create_base_size(kind)
                if linear_store is not None
                else self._store.encoded_create_size(kind, ())
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise CandidatePersistenceError("malformed_store") from None
        if type(size) is not int or size < 0:
            raise CandidatePersistenceError("malformed_store") from None
        return size

    def _encoded_record_sizes(
        self, record: CandidateStoreRecord, *, base_size: int
    ) -> tuple[int, int, str]:
        try:
            linear_store = self._linear_store()
            if linear_store is not None:
                sizes = linear_store.encoded_record_sizes(record)
            else:
                document_size = self._store.encoded_document_size(record)
                singleton_size = self._store.encoded_create_size(record.kind, (record,))
                sizes = (
                    document_size,
                    singleton_size - base_size,
                    candidate_inventory_sha256((record,)),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise CandidatePersistenceError("malformed_store") from None
        if (
            type(sizes) is not tuple
            or len(sizes) != 3
            or any(type(size) is not int or size < 0 for size in sizes[:2])
            or not _valid_sha256(sizes[2])
        ):
            raise CandidatePersistenceError("malformed_store") from None
        document_size, contribution, write_sha256 = sizes
        if document_size > FIRESTORE_DOCUMENT_MAX_BYTES:
            raise CandidatePersistenceError("store_bounds_exceeded") from None
        return document_size, contribution, write_sha256

    async def _delay(self) -> None:
        try:
            await self._sleep(0.1)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise CandidatePersistenceError("store_unavailable") from None

    def _plan_batches(
        self, kind: CandidateRecordKind, records: tuple[CandidateStoreRecord, ...]
    ) -> tuple[_CandidateWriteBatch, ...]:
        batches: list[_CandidateWriteBatch] = []
        base_size = self._encoded_create_base_size(kind)
        current: list[CandidateStoreRecord] = []
        current_contributions: list[int] = []
        current_sha256s: list[str] = []
        current_size = base_size
        for record in records:
            _, contribution, write_sha256 = self._encoded_record_sizes(record, base_size=base_size)
            if (
                len(current) == CANDIDATE_WRITE_BATCH_MAX_RECORDS
                or current_size + contribution > CANDIDATE_WRITE_BATCH_MAX_BYTES
            ):
                if not current:
                    raise CandidatePersistenceError("store_bounds_exceeded") from None
                batches.append(
                    _CandidateWriteBatch(
                        records=tuple(current),
                        encoded_size=current_size,
                        base_size=base_size,
                        contributions=tuple(current_contributions),
                        write_sha256s=tuple(current_sha256s),
                    )
                )
                current = []
                current_contributions = []
                current_sha256s = []
                current_size = base_size
                if current_size + contribution > CANDIDATE_WRITE_BATCH_MAX_BYTES:
                    raise CandidatePersistenceError("store_bounds_exceeded") from None
            current.append(record)
            current_contributions.append(contribution)
            current_sha256s.append(write_sha256)
            current_size += contribution
        if current:
            batches.append(
                _CandidateWriteBatch(
                    records=tuple(current),
                    encoded_size=current_size,
                    base_size=base_size,
                    contributions=tuple(current_contributions),
                    write_sha256s=tuple(current_sha256s),
                )
            )
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

    async def _create_once(self, kind: CandidateRecordKind, batch: _CandidateWriteBatch) -> bool:
        if batch.encoded_size > CANDIDATE_WRITE_BATCH_MAX_BYTES:
            raise CandidatePersistenceError("store_bounds_exceeded") from None
        try:
            linear_store = self._linear_store()
            if linear_store is not None:
                await self._bounded(
                    lambda: linear_store.create_many_checked(
                        kind,
                        batch.records,
                        expected_encoded_size=batch.encoded_size,
                        expected_write_sha256s=batch.write_sha256s,
                        timeout_seconds=self._timeout_seconds,
                    )
                )
            else:
                if self._store.encoded_create_size(kind, batch.records) != batch.encoded_size:
                    raise _CandidateStoreMalformed()
                await self._bounded(
                    lambda: self._store.create_many(
                        kind,
                        batch.records,
                        timeout_seconds=self._timeout_seconds,
                    )
                )
            return True
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return False
        except _CandidateStoreMalformed:
            raise CandidatePersistenceError("malformed_store") from None
        except CandidateStoreFailure as error:
            if error.code == "permanent":
                raise CandidatePersistenceError("store_unavailable") from None
            return False
        except Exception:
            raise CandidatePersistenceError("store_unavailable") from None

    async def _create_with_resolution(
        self, kind: CandidateRecordKind, batch: _CandidateWriteBatch
    ) -> bool:
        if not batch.records:
            return False
        if await self._create_once(kind, batch):
            return True
        reread = await self._get_many(kind, batch.records)
        missing_indices = tuple(index for index, existing in enumerate(reread) if existing is None)
        if not missing_indices:
            return False
        if self._max_retries == 0:
            raise CandidatePersistenceError("store_unavailable") from None
        await self._delay()
        retry_batch = batch.select(missing_indices)
        if await self._create_once(kind, retry_batch):
            return True
        final = await self._get_many(kind, retry_batch.records)
        if any(item is None for item in final):
            raise CandidatePersistenceError("store_unavailable") from None
        return False

    async def _create_header(self, batch: _CandidateWriteBatch) -> bool:
        """Return true only when the first header create has a known success."""
        header = batch.records[0]
        if await self._create_once("candidate", batch):
            return True
        reread = await self._get_many("candidate", (header,))
        if reread[0] is not None:
            return False
        if self._max_retries == 0:
            raise CandidatePersistenceError("store_unavailable") from None
        await self._delay()
        if await self._create_once("candidate", batch):
            return False
        final = await self._get_many("candidate", (header,))
        if final[0] is None:
            raise CandidatePersistenceError("store_unavailable") from None
        return False

    async def _create_or_confirm_batch(
        self, kind: CandidateRecordKind, batch: _CandidateWriteBatch
    ) -> bool:
        existing = await self._get_many(kind, batch.records)
        missing = tuple(index for index, item in enumerate(existing) if item is None)
        return await self._create_with_resolution(kind, batch.select(missing))

    async def _list_exact(
        self,
        kind: Literal["document", "chunk"],
        corpus: ExactCorpusReference,
        expected: tuple[CandidateStoreRecord, ...],
    ) -> tuple[CandidateStoreRecord, ...]:
        records = await _list_exact_records(
            store=self._store,
            read=self._read,
            kind=kind,
            corpus=corpus,
            expected_count=len(expected),
            timeout_seconds=self._timeout_seconds,
        )
        for actual, wanted in zip(records, expected, strict=True):
            self._compare_existing(actual, wanted)
        return records

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
            chunk.chunk_id: chunk for document in plan.documents for chunk in document.chunks
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
        algorithm_id, key_id = _signer_identity(verifier)
        return await _verify_attestation_record(
            raw_record,
            corpus=plan.corpus,
            payload=payload,
            identity=AttestationIdentity(
                algorithm_id=algorithm_id,
                key_id=key_id,
            ),
            verifier=verifier,
            bounded=self._bounded,
        )

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
            header_created = await self._create_header(header_batch[0])
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
            signature_result: object = _MISSING
            signing_failed = False
            try:
                signature_result = await self._bounded(partial(self._signer.sign, payload_bytes))
                if (
                    type(signature_result) is not bytes
                    or not MIN_SIGNATURE_BYTES <= len(signature_result) <= MAX_SIGNATURE_BYTES
                ):
                    raise ValueError
                verified = await self._bounded(
                    partial(self._signer.verify, payload_bytes, signature_result)
                )
                if verified is not True:
                    raise ValueError
            except asyncio.CancelledError:
                raise
            except Exception:
                signing_failed = True
            if signing_failed or type(signature_result) is not bytes:
                raise CandidatePersistenceError("attestation_failed") from None
            signature = signature_result
            proposed = _attestation_record(plan, payload, signature)
            attestation_batch = self._plan_batches("attestation", (proposed,))[0]
            created = await self._create_once("attestation", attestation_batch)
            known_later_create |= created
            raw_attestation = await self._read(
                lambda: self._store.get(
                    "attestation", header.key, timeout_seconds=self._timeout_seconds
                )
            )
            if raw_attestation is None and not created and self._max_retries == 1:
                await self._delay()
                created = await self._create_once("attestation", attestation_batch)
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
        return self._receipt(plan, disposition, inventory_sha256, payload_sha256)

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
