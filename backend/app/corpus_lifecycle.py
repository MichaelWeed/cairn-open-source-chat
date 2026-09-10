"""Attested immutable-corpus lifecycle registry.

This internal module is deliberately not wired into application startup or HTTP.
It owns exact lifecycle state, active-pointer CAS, and immutable audit records while
borrowing M8's signer-free candidate verification callable.
"""

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from threading import RLock
from typing import Annotated, Any, Literal, Protocol, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetCoreSchemaHandler,
    StrictInt,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import CoreSchema, core_schema

from app.ingest.candidate_persistence import (
    MAX_SIGNER_ID_CHARS,
    AttestationIdentity,
    AttestationVerifier,
    CandidatePersistenceError,
    VerifiedCandidateEvidence,
    canonical_json_bytes,
)
from app.ingest.planner import (
    MAX_DOCUMENTS,
    MAX_EMBEDDING_IDENTITY_CHARS,
    MAX_TOTAL_CHUNKS,
)
from app.retrieval_contracts import MAX_SAFE_INTEGER, ExactCorpusReference

CORPUS_LIFECYCLE_CONTRACT_VERSION: Literal["1.0"] = "1.0"
CORPUS_LIFECYCLE_RECORD_SCHEMA_VERSION: Literal["1.0"] = "1.0"
ACTIVE_CORPUS_POINTER_SCHEMA_VERSION: Literal["1.0"] = "1.0"
CORPUS_LIFECYCLE_AUDIT_SCHEMA_VERSION: Literal["1.0"] = "1.0"
LIFECYCLE_READY_REVISION: Literal[0] = 0
LIFECYCLE_REMOVED_REVISION: Literal[1] = 1

LifecycleAction = Literal["ready", "promote", "rollback", "remove"]
LifecycleDisposition = Literal["applied", "confirmed"]
StoreMutationResult = Literal["applied", "conflict"]
CorpusLifecycleErrorCode = Literal[
    "invalid_request",
    "candidate_unavailable",
    "attestation_untrusted",
    "lifecycle_conflict",
    "store_unavailable",
    "malformed_store",
    "no_active_version",
    "active_version_forbidden",
]

_ERROR_MESSAGES: dict[CorpusLifecycleErrorCode, str] = {
    "invalid_request": "The corpus lifecycle request is invalid.",
    "candidate_unavailable": "The attested candidate is unavailable.",
    "attestation_untrusted": "The candidate attestation identity is not trusted.",
    "lifecycle_conflict": "The corpus lifecycle state conflicts with the request.",
    "store_unavailable": "The corpus lifecycle store is unavailable.",
    "malformed_store": "The corpus lifecycle store returned invalid state.",
    "no_active_version": "The corpus has no active version.",
    "active_version_forbidden": "The active corpus version cannot be removed.",
}
_MISSING = object()
_MODEL_REBUILD_LOCK = RLock()
_DIGEST_CHARS = frozenset("0123456789abcdef")


class CorpusLifecycleError(Exception):
    """Fixed content-free lifecycle failure."""

    def __init__(self, code: CorpusLifecycleErrorCode) -> None:
        if type(code) is not str or code not in _ERROR_MESSAGES:
            raise ValueError("Unsupported corpus lifecycle error code.") from None
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])

    def __repr__(self) -> str:
        return f"CorpusLifecycleError(code={self.code!r})"

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
                "type": "corpus_lifecycle_error",
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


def _sanitize_error(error: CorpusLifecycleError) -> CorpusLifecycleError:
    error.__cause__ = None
    error.__context__ = None
    error.__traceback__ = None
    return error


def _content_free_call[Result](
    code: CorpusLifecycleErrorCode,
    operation: Callable[[], Result],
    *,
    preserve_lifecycle_error: bool = False,
) -> Result:
    result: object = _MISSING
    lifecycle_error: CorpusLifecycleError | None = None
    try:
        result = operation()
    except CorpusLifecycleError as error:
        lifecycle_error = error
    except Exception:
        pass
    if lifecycle_error is not None and preserve_lifecycle_error:
        raise _sanitize_error(lifecycle_error) from None
    if result is _MISSING:
        raise CorpusLifecycleError(code) from None
    return cast(Result, result)


def _content_free_validation(
    model: type["CorpusLifecycleModel"],
    value: Any,
    handler: Callable[[Any], Any],
) -> Any:
    def validate() -> Any:
        if isinstance(value, Mapping) and any(key not in model.model_fields for key in value):
            raise ValueError
        return handler(value)

    return _content_free_call(
        "invalid_request",
        validate,
        preserve_lifecycle_error=True,
    )


class _ContentFreeSchemaValidator:
    __slots__ = ("_validator",)

    def __init__(self, validator: Any) -> None:
        self._validator = validator

    def validate_python(self, *args: Any, **kwargs: Any) -> Any:
        return _content_free_call(
            "invalid_request",
            lambda: self._validator.validate_python(*args, **kwargs),
        )

    def validate_json(self, *args: Any, **kwargs: Any) -> Any:
        return _content_free_call(
            "invalid_request",
            lambda: self._validator.validate_json(*args, **kwargs),
        )

    def validate_strings(self, *args: Any, **kwargs: Any) -> Any:
        return _content_free_call(
            "invalid_request",
            lambda: self._validator.validate_strings(*args, **kwargs),
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._validator, name)


class CorpusLifecycleModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        hide_input_in_errors=True,
        arbitrary_types_allowed=True,
        revalidate_instances="always",
    )

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        validator = cls.__pydantic_validator__
        if not isinstance(validator, _ContentFreeSchemaValidator):
            cast(Any, cls).__pydantic_validator__ = _ContentFreeSchemaValidator(validator)

    @classmethod
    def model_rebuild(
        cls,
        *,
        force: bool = False,
        raise_errors: bool = True,
        _parent_namespace_depth: int = 2,
        _types_namespace: Any = None,
    ) -> bool | None:
        with _MODEL_REBUILD_LOCK:
            result = super().model_rebuild(
                force=force,
                raise_errors=raise_errors,
                _parent_namespace_depth=_parent_namespace_depth,
                _types_namespace=_types_namespace,
            )
            validator = cls.__pydantic_validator__
            if not isinstance(validator, _ContentFreeSchemaValidator):
                cast(Any, cls).__pydantic_validator__ = _ContentFreeSchemaValidator(validator)
            return result

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_wrap_validator_function(
            lambda value, validator: _content_free_validation(cls, value, validator),
            handler(source_type),
        )

    @classmethod
    def _validated(cls, values: Any) -> Self:
        return _content_free_call(
            "invalid_request",
            lambda: cls.model_validate(values),
            preserve_lifecycle_error=True,
        )

    @classmethod
    def model_construct(cls, _fields_set: set[str] | None = None, **values: Any) -> Self:
        if _fields_set is not None:
            fields_set = _content_free_call("invalid_request", lambda: set(_fields_set))
            if not fields_set.issubset(cls.model_fields):
                raise CorpusLifecycleError("invalid_request") from None
        result = cls._validated(values)
        if _fields_set is not None:
            object.__setattr__(result, "__pydantic_fields_set__", set(_fields_set))
        return result

    @classmethod
    def construct(cls, _fields_set: set[str] | None = None, **values: Any) -> Self:
        return cls.model_construct(_fields_set=_fields_set, **values)

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        base_copy = super().model_copy

        def prepare() -> tuple[dict[str, Any], set[str]]:
            source = base_copy(deep=deep)
            values = {name: getattr(source, name) for name in type(self).model_fields}
            fields_set = set(source.model_fields_set)
            if update is not None:
                values.update(update)
                fields_set.update(update)
            return values, fields_set

        values, fields_set = _content_free_call("invalid_request", prepare)
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

        def prepare() -> dict[str, Any]:
            values = self.model_dump(include=include, exclude=exclude, round_trip=True)
            if update is not None:
                values.update(update)
            return values

        return type(self)._validated(_content_free_call("invalid_request", prepare))

    def __replace__(self, **changes: Any) -> Self:
        return self.model_copy(update=changes)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise CorpusLifecycleError("invalid_request") from None

    def __delattr__(self, name: str) -> None:
        del name
        raise CorpusLifecycleError("invalid_request") from None


def _revalidate_model[T: BaseModel](value: object, model: type[T]) -> T:
    def validate() -> T:
        if type(value) is model:
            if object.__getattribute__(value, "__pydantic_extra__"):
                raise ValueError
            copied: object = {name: getattr(value, name) for name in model.model_fields}
        elif isinstance(value, Mapping):
            copied = dict(value)
        else:
            raise TypeError
        return model.model_validate(copied)

    return _content_free_call(
        "invalid_request",
        validate,
        preserve_lifecycle_error=True,
    )


def _revalidate_corpus(value: object) -> ExactCorpusReference:
    return _revalidate_model(value, ExactCorpusReference)


def _revalidate_identity(value: object) -> AttestationIdentity:
    return _revalidate_model(value, AttestationIdentity)


def _digest(value: str) -> str:
    if len(value) != 64 or any(character not in _DIGEST_CHARS for character in value):
        raise ValueError
    return value


def _printable(value: str) -> str:
    if (
        value != value.strip()
        or not value.isascii()
        or any(ord(character) < 0x20 or ord(character) > 0x7E for character in value)
    ):
        raise ValueError
    return value


class VerifiedLifecycleEvidence(CorpusLifecycleModel):
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
        return _digest(value)

    @field_validator("embedding_identity", "signature_algorithm_id", "signing_key_id")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        return _printable(value)


class CorpusLifecycleRecord(VerifiedLifecycleEvidence):
    schema_version: Literal["1.0"]
    contract_version: Literal["1.0"]
    state: Literal["ready", "logically_removed"]
    revision: Literal[0, 1]

    @field_validator("revision", mode="before")
    @classmethod
    def validate_revision_type(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError
        return value

    @model_validator(mode="after")
    def validate_state_revision(self) -> "CorpusLifecycleRecord":
        expected = 0 if self.state == "ready" else 1
        if self.revision != expected:
            raise ValueError
        return self

    @property
    def evidence(self) -> VerifiedLifecycleEvidence:
        return VerifiedLifecycleEvidence.model_validate(
            self.model_dump(
                include=set(VerifiedLifecycleEvidence.model_fields),
                round_trip=True,
            )
        )


class ActiveCorpusPointer(CorpusLifecycleModel):
    schema_version: Literal["1.0"]
    contract_version: Literal["1.0"]
    corpus_id: Annotated[str, Field(min_length=1, max_length=96)]
    target: ExactCorpusReference
    revision: Annotated[StrictInt, Field(ge=0, le=MAX_SAFE_INTEGER)]
    target_lifecycle_revision: Literal[0]
    attestation_payload_sha256: Annotated[str, Field(min_length=64, max_length=64)]

    @field_validator("target", mode="before")
    @classmethod
    def validate_target(cls, value: object) -> ExactCorpusReference:
        return _revalidate_corpus(value)

    @field_validator("target_lifecycle_revision", mode="before")
    @classmethod
    def validate_target_lifecycle_revision(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError
        return value

    @field_validator("attestation_payload_sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _digest(value)

    @model_validator(mode="after")
    def validate_scope(self) -> "ActiveCorpusPointer":
        if self.target.corpus_id != self.corpus_id:
            raise ValueError
        return self


class LifecycleAuditRecord(CorpusLifecycleModel):
    schema_version: Literal["1.0"]
    contract_version: Literal["1.0"]
    action: LifecycleAction
    subject: ExactCorpusReference | None
    before: ExactCorpusReference | None
    after: ExactCorpusReference | None
    resulting_revision: Annotated[StrictInt, Field(ge=0, le=MAX_SAFE_INTEGER)]
    authorizing_attestation_payload_sha256: Annotated[
        str, Field(min_length=64, max_length=64)
    ]

    @field_validator("subject", "before", "after", mode="before")
    @classmethod
    def validate_references(
        cls, value: object, info: ValidationInfo
    ) -> ExactCorpusReference | None:
        del info
        return None if value is None else _revalidate_corpus(value)

    @field_validator("authorizing_attestation_payload_sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _digest(value)

    @model_validator(mode="after")
    def validate_shape(self) -> "LifecycleAuditRecord":
        if self.action == "ready":
            valid = (
                self.subject is not None
                and self.before is None
                and self.after is None
                and self.resulting_revision == 0
            )
        elif self.action == "remove":
            valid = (
                self.subject is not None
                and self.before is None
                and self.after is None
                and self.resulting_revision == 1
            )
        elif self.action == "promote":
            valid = (
                self.subject is None
                and self.after is not None
                and (
                    (self.resulting_revision == 0 and self.before is None)
                    or (
                        self.resulting_revision > 0
                        and self.before is not None
                        and self.before != self.after
                    )
                )
            )
        else:
            valid = (
                self.subject is None
                and self.before is not None
                and self.after is not None
                and self.before != self.after
                and self.resulting_revision > 0
            )
        if not valid:
            raise ValueError
        if self.before is not None and self.after is not None:
            if self.before.corpus_id != self.after.corpus_id:
                raise ValueError
        return self


class MarkReadyRequest(CorpusLifecycleModel):
    contract_version: Literal["1.0"]
    corpus: ExactCorpusReference
    trusted_identity: AttestationIdentity

    @field_validator("corpus", mode="before")
    @classmethod
    def validate_corpus(cls, value: object) -> ExactCorpusReference:
        return _revalidate_corpus(value)

    @field_validator("trusted_identity", mode="before")
    @classmethod
    def validate_identity(cls, value: object) -> AttestationIdentity:
        return _revalidate_identity(value)


class ExpectedActivePointer(CorpusLifecycleModel):
    target: ExactCorpusReference
    revision: Annotated[StrictInt, Field(ge=0, le=MAX_SAFE_INTEGER)]

    @field_validator("target", mode="before")
    @classmethod
    def validate_target(cls, value: object) -> ExactCorpusReference:
        return _revalidate_corpus(value)


class SwitchActiveRequest(CorpusLifecycleModel):
    contract_version: Literal["1.0"]
    action: Literal["promote", "rollback"]
    target: ExactCorpusReference
    expected: ExpectedActivePointer | None

    @field_validator("target", mode="before")
    @classmethod
    def validate_target(cls, value: object) -> ExactCorpusReference:
        return _revalidate_corpus(value)

    @field_validator("expected", mode="before")
    @classmethod
    def validate_expected(cls, value: object) -> ExpectedActivePointer | None:
        return None if value is None else _revalidate_model(value, ExpectedActivePointer)

    @model_validator(mode="after")
    def validate_switch(self) -> "SwitchActiveRequest":
        if self.action == "rollback" and self.expected is None:
            raise ValueError
        if self.expected is not None:
            if self.expected.target.corpus_id != self.target.corpus_id:
                raise ValueError
            if self.expected.target == self.target:
                raise ValueError
            if self.expected.revision == MAX_SAFE_INTEGER:
                raise ValueError
        return self


class RemoveCorpusVersionRequest(CorpusLifecycleModel):
    contract_version: Literal["1.0"]
    corpus: ExactCorpusReference
    expected_lifecycle_revision: Literal[0]

    @field_validator("expected_lifecycle_revision", mode="before")
    @classmethod
    def validate_expected_lifecycle_revision(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError
        return value

    @field_validator("corpus", mode="before")
    @classmethod
    def validate_corpus(cls, value: object) -> ExactCorpusReference:
        return _revalidate_corpus(value)


class LifecycleMutationReceipt(CorpusLifecycleModel):
    contract_version: Literal["1.0"]
    action: LifecycleAction
    disposition: LifecycleDisposition
    subject: ExactCorpusReference | None
    target: ExactCorpusReference | None
    resulting_revision: Annotated[StrictInt, Field(ge=0, le=MAX_SAFE_INTEGER)]

    @field_validator("subject", "target", mode="before")
    @classmethod
    def validate_references(cls, value: object) -> ExactCorpusReference | None:
        return None if value is None else _revalidate_corpus(value)

    @model_validator(mode="after")
    def validate_shape(self) -> "LifecycleMutationReceipt":
        if self.action in {"ready", "remove"}:
            valid = self.subject is not None and self.target is None
        else:
            valid = self.subject is None and self.target is not None
        if not valid:
            raise ValueError
        if self.action == "ready" and self.resulting_revision != 0:
            raise ValueError
        if self.action == "remove" and self.resulting_revision != 1:
            raise ValueError
        if self.action == "rollback" and self.resulting_revision == 0:
            raise ValueError
        return self


class LifecycleSnapshot(CorpusLifecycleModel):
    record: CorpusLifecycleRecord | None
    audit: LifecycleAuditRecord | None

    @field_validator("record", mode="before")
    @classmethod
    def validate_record(cls, value: object) -> CorpusLifecycleRecord | None:
        return None if value is None else _revalidate_model(value, CorpusLifecycleRecord)

    @field_validator("audit", mode="before")
    @classmethod
    def validate_audit(cls, value: object) -> LifecycleAuditRecord | None:
        return None if value is None else _revalidate_model(value, LifecycleAuditRecord)

    @model_validator(mode="after")
    def validate_pair(self) -> "LifecycleSnapshot":
        if (self.record is None) != (self.audit is None):
            raise ValueError
        if self.record is not None and self.audit is not None:
            expected_action = "ready" if self.record.state == "ready" else "remove"
            if (
                self.audit.action != expected_action
                or self.audit.subject != self.record.corpus
                or self.audit.resulting_revision != self.record.revision
                or self.audit.authorizing_attestation_payload_sha256
                != self.record.attestation_payload_sha256
            ):
                raise ValueError
        return self


class ActivePointerSnapshot(CorpusLifecycleModel):
    pointer: ActiveCorpusPointer | None
    audit: LifecycleAuditRecord | None
    target_lifecycle: LifecycleSnapshot | None

    @field_validator("pointer", mode="before")
    @classmethod
    def validate_pointer(cls, value: object) -> ActiveCorpusPointer | None:
        return None if value is None else _revalidate_model(value, ActiveCorpusPointer)

    @field_validator("audit", mode="before")
    @classmethod
    def validate_audit(cls, value: object) -> LifecycleAuditRecord | None:
        return None if value is None else _revalidate_model(value, LifecycleAuditRecord)

    @field_validator("target_lifecycle", mode="before")
    @classmethod
    def validate_target(cls, value: object) -> LifecycleSnapshot | None:
        return None if value is None else _revalidate_model(value, LifecycleSnapshot)

    @model_validator(mode="after")
    def validate_relationships(self) -> "ActivePointerSnapshot":
        values = (self.pointer, self.audit, self.target_lifecycle)
        if all(value is None for value in values):
            return self
        if any(value is None for value in values):
            raise ValueError
        pointer = cast(ActiveCorpusPointer, self.pointer)
        audit = cast(LifecycleAuditRecord, self.audit)
        lifecycle = cast(LifecycleSnapshot, self.target_lifecycle)
        record = lifecycle.record
        if (
            record is None
            or record.state != "ready"
            or record.revision != 0
            or record.corpus != pointer.target
            or pointer.attestation_payload_sha256 != record.attestation_payload_sha256
            or audit.action not in {"promote", "rollback"}
            or audit.after != pointer.target
            or audit.resulting_revision != pointer.revision
            or audit.authorizing_attestation_payload_sha256
            != pointer.attestation_payload_sha256
            or (pointer.revision == 0 and (audit.action != "promote" or audit.before is not None))
            or (pointer.revision > 0 and audit.before is None)
        ):
            raise ValueError
        return self


class ResolvedActiveState(CorpusLifecycleModel):
    target: ExactCorpusReference
    pointer_revision: Annotated[StrictInt, Field(ge=0, le=MAX_SAFE_INTEGER)]
    lifecycle_revision: Literal[0]
    evidence: VerifiedLifecycleEvidence

    @field_validator("target", mode="before")
    @classmethod
    def validate_target(cls, value: object) -> ExactCorpusReference:
        return _revalidate_corpus(value)

    @field_validator("lifecycle_revision", mode="before")
    @classmethod
    def validate_lifecycle_revision(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError
        return value

    @field_validator("evidence", mode="before")
    @classmethod
    def validate_evidence(cls, value: object) -> VerifiedLifecycleEvidence:
        return _revalidate_model(value, VerifiedLifecycleEvidence)

    @model_validator(mode="after")
    def validate_scope(self) -> "ResolvedActiveState":
        if self.target != self.evidence.corpus:
            raise ValueError
        return self


class AttestationTrustPolicy(Protocol):
    @property
    def policy_version(self) -> str: ...

    @property
    def policy_generation(self) -> int: ...

    def verifier_for(
        self, identity: AttestationIdentity
    ) -> AttestationVerifier | None: ...


class AttestedCandidateVerifier(Protocol):
    async def __call__(
        self,
        corpus: ExactCorpusReference,
        identity: AttestationIdentity,
        verifier: AttestationVerifier,
    ) -> VerifiedCandidateEvidence: ...


class CorpusLifecycleStoreFailure(Exception):
    """Content-free lifecycle-store failure classification."""

    def __init__(
        self,
        code: Literal["retryable", "commit_outcome_unknown", "permanent", "malformed"],
    ) -> None:
        if code not in {"retryable", "commit_outcome_unknown", "permanent", "malformed"}:
            raise ValueError("Unsupported lifecycle store failure code.") from None
        self.code = code
        super().__init__("Corpus lifecycle store request failed.")

    def __repr__(self) -> str:
        return f"CorpusLifecycleStoreFailure(code={self.code!r})"


class CorpusLifecycleStore(Protocol):
    async def read_lifecycle_snapshot(
        self, corpus: ExactCorpusReference, *, timeout_seconds: int
    ) -> LifecycleSnapshot: ...

    async def read_active_snapshot(
        self, corpus_id: str, *, timeout_seconds: int
    ) -> ActivePointerSnapshot: ...

    async def commit_ready(
        self,
        expected_absent: bool,
        replacement: CorpusLifecycleRecord,
        audit: LifecycleAuditRecord,
        *,
        timeout_seconds: int,
    ) -> StoreMutationResult: ...

    async def compare_and_swap_active(
        self,
        expected: ActivePointerSnapshot,
        replacement: ActiveCorpusPointer,
        target_ready: CorpusLifecycleRecord,
        audit: LifecycleAuditRecord,
        *,
        timeout_seconds: int,
    ) -> StoreMutationResult: ...

    async def compare_and_remove(
        self,
        expected_ready: LifecycleSnapshot,
        replacement: CorpusLifecycleRecord,
        expected_active: ActivePointerSnapshot,
        audit: LifecycleAuditRecord,
        *,
        timeout_seconds: int,
    ) -> StoreMutationResult: ...

    async def aclose(self) -> None: ...


def _derived_key(prefix: str, domain: str, value: object) -> str:
    material = domain.encode("ascii") + b"\0" + canonical_json_bytes(value)
    return prefix + hashlib.sha256(material).hexdigest()


def lifecycle_record_key(corpus: ExactCorpusReference) -> str:
    corpus = _revalidate_corpus(corpus)
    return _derived_key(
        "life1-",
        "cairn-corpus-lifecycle-v1",
        {
            "kind": "exact",
            "corpus_id": corpus.corpus_id,
            "corpus_version": corpus.corpus_version,
        },
    )


def active_pointer_key(corpus_id: str) -> str:
    validated = _validate_corpus_id(corpus_id)
    return _derived_key("active1-", "cairn-active-corpus-v1", {"corpus_id": validated})


def lifecycle_audit_key(corpus: ExactCorpusReference, resulting_revision: int) -> str:
    corpus = _revalidate_corpus(corpus)
    if type(resulting_revision) is not int or resulting_revision not in {0, 1}:
        raise CorpusLifecycleError("invalid_request") from None
    return _derived_key(
        "audit1-",
        "cairn-corpus-lifecycle-audit-v1",
        {
            "stream": "lifecycle",
            "kind": "exact",
            "corpus_id": corpus.corpus_id,
            "corpus_version": corpus.corpus_version,
            "resulting_revision": resulting_revision,
        },
    )


def active_audit_key(corpus_id: str, resulting_revision: int) -> str:
    validated = _validate_corpus_id(corpus_id)
    if (
        type(resulting_revision) is not int
        or resulting_revision < 0
        or resulting_revision > MAX_SAFE_INTEGER
    ):
        raise CorpusLifecycleError("invalid_request") from None
    return _derived_key(
        "audit1-",
        "cairn-active-corpus-audit-v1",
        {
            "stream": "active",
            "corpus_id": validated,
            "resulting_revision": resulting_revision,
        },
    )


def _validate_corpus_id(value: object) -> str:
    exact = _content_free_call(
        "invalid_request",
        lambda: ExactCorpusReference(
            kind="exact",
            corpus_id=cast(Any, value),
            corpus_version="validation-only",
        ),
    )
    return exact.corpus_id


def _evidence_values(evidence: VerifiedLifecycleEvidence) -> dict[str, object]:
    return evidence.model_dump(mode="python", round_trip=True)


def _record_from_evidence(
    evidence: VerifiedLifecycleEvidence,
    *,
    state: Literal["ready", "logically_removed"] = "ready",
) -> CorpusLifecycleRecord:
    return CorpusLifecycleRecord.model_validate(
        {
            **_evidence_values(evidence),
            "schema_version": CORPUS_LIFECYCLE_RECORD_SCHEMA_VERSION,
            "contract_version": CORPUS_LIFECYCLE_CONTRACT_VERSION,
            "state": state,
            "revision": 0 if state == "ready" else 1,
        }
    )


def _ready_audit(record: CorpusLifecycleRecord) -> LifecycleAuditRecord:
    return LifecycleAuditRecord(
        schema_version=CORPUS_LIFECYCLE_AUDIT_SCHEMA_VERSION,
        contract_version=CORPUS_LIFECYCLE_CONTRACT_VERSION,
        action="ready",
        subject=record.corpus,
        before=None,
        after=None,
        resulting_revision=0,
        authorizing_attestation_payload_sha256=record.attestation_payload_sha256,
    )


def _remove_audit(record: CorpusLifecycleRecord) -> LifecycleAuditRecord:
    return LifecycleAuditRecord(
        schema_version=CORPUS_LIFECYCLE_AUDIT_SCHEMA_VERSION,
        contract_version=CORPUS_LIFECYCLE_CONTRACT_VERSION,
        action="remove",
        subject=record.corpus,
        before=None,
        after=None,
        resulting_revision=1,
        authorizing_attestation_payload_sha256=record.attestation_payload_sha256,
    )


class _PolicySnapshot:
    __slots__ = ("policy", "version", "generation")

    def __init__(self, policy: AttestationTrustPolicy, version: str, generation: int) -> None:
        self.policy = policy
        self.version = version
        self.generation = generation


def _capture_policy(policy: AttestationTrustPolicy) -> _PolicySnapshot:
    captured: tuple[str, int] | None = None
    try:
        version = policy.policy_version
        generation = policy.policy_generation
        if (
            type(version) is not str
            or not 1 <= len(version) <= MAX_SIGNER_ID_CHARS
            or _printable(version) != version
            or type(generation) is not int
            or not 0 <= generation <= MAX_SAFE_INTEGER
            or not callable(policy.verifier_for)
        ):
            raise ValueError
        captured = (version, generation)
    except Exception:
        pass
    if captured is None:
        raise CorpusLifecycleError("attestation_untrusted") from None
    return _PolicySnapshot(policy, *captured)


def _select_verifier(
    policy: _PolicySnapshot, identity: AttestationIdentity
) -> AttestationVerifier:
    selected: AttestationVerifier | None = None
    try:
        candidate = policy.policy.verifier_for(identity)
        if candidate is None:
            raise ValueError
        if (
            candidate.algorithm_id != identity.algorithm_id
            or candidate.key_id != identity.key_id
            or _printable(candidate.algorithm_id) != candidate.algorithm_id
            or _printable(candidate.key_id) != candidate.key_id
            or not callable(candidate.verify)
        ):
            raise ValueError
        selected = candidate
    except Exception:
        pass
    if selected is None:
        raise CorpusLifecycleError("attestation_untrusted") from None
    return selected


def _identity_from_record(record: CorpusLifecycleRecord) -> AttestationIdentity:
    return _content_free_call(
        "malformed_store",
        lambda: AttestationIdentity(
            algorithm_id=record.signature_algorithm_id,
            key_id=record.signing_key_id,
        ),
    )


def _validate_lifecycle_snapshot(value: object) -> LifecycleSnapshot:
    try:
        return _revalidate_model(value, LifecycleSnapshot)
    except CorpusLifecycleError:
        pass
    raise CorpusLifecycleError("malformed_store") from None


def _validate_active_snapshot(value: object) -> ActivePointerSnapshot:
    try:
        return _revalidate_model(value, ActivePointerSnapshot)
    except CorpusLifecycleError:
        pass
    raise CorpusLifecycleError("malformed_store") from None


class CorpusLifecycleService:
    def __init__(
        self,
        *,
        store: CorpusLifecycleStore,
        verify_attested_candidate: AttestedCandidateVerifier,
        expected_embedding_identity: str,
        expected_embedding_dimensions: int,
        timeout_seconds: int,
        max_retries: Literal[0, 1],
        sleep: Callable[[float], Awaitable[None]],
        owns_store: bool = False,
    ) -> None:
        valid = False
        try:
            valid = (
                callable(verify_attested_candidate)
                and type(expected_embedding_identity) is str
                and 1 <= len(expected_embedding_identity) <= MAX_EMBEDDING_IDENTITY_CHARS
                and _printable(expected_embedding_identity) == expected_embedding_identity
                and type(expected_embedding_dimensions) is int
                and 1 <= expected_embedding_dimensions <= 2048
                and type(timeout_seconds) is int
                and 1 <= timeout_seconds <= 30
                and type(max_retries) is int
                and max_retries in {0, 1}
                and callable(sleep)
                and type(owns_store) is bool
            )
        except Exception:
            pass
        if not valid:
            raise CorpusLifecycleError("invalid_request") from None
        self._store = store
        self._verify_attested_candidate = verify_attested_candidate
        self._expected_embedding_identity = expected_embedding_identity
        self._expected_embedding_dimensions = expected_embedding_dimensions
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._sleep = sleep
        self._owns_store = owns_store
        self._closed = False
        self._close_lock = asyncio.Lock()

    def _ensure_open(self) -> None:
        if self._closed:
            raise CorpusLifecycleError("store_unavailable") from None

    async def _sleep_retry(self) -> None:
        failure = False
        try:
            await self._sleep(0.1)
        except asyncio.CancelledError:
            raise
        except Exception:
            failure = True
        if failure:
            raise CorpusLifecycleError("store_unavailable") from None

    async def _read_lifecycle(self, corpus: ExactCorpusReference) -> LifecycleSnapshot:
        self._ensure_open()
        for attempt in range(self._max_retries + 1):
            result: object = _MISSING
            failure: CorpusLifecycleStoreFailure | None = None
            try:
                result = await self._store.read_lifecycle_snapshot(
                    corpus, timeout_seconds=self._timeout_seconds
                )
            except asyncio.CancelledError:
                raise
            except CorpusLifecycleStoreFailure as error:
                failure = error
            except Exception:
                failure = CorpusLifecycleStoreFailure("permanent")
            if failure is None:
                snapshot = _validate_lifecycle_snapshot(result)
                if snapshot.record is not None and snapshot.record.corpus != corpus:
                    raise CorpusLifecycleError("malformed_store") from None
                return snapshot
            if failure.code == "malformed":
                raise CorpusLifecycleError("malformed_store") from None
            if failure.code != "retryable" or attempt == self._max_retries:
                raise CorpusLifecycleError("store_unavailable") from None
            await self._sleep_retry()
        raise AssertionError("unreachable")

    async def _read_active(self, corpus_id: str) -> ActivePointerSnapshot:
        self._ensure_open()
        for attempt in range(self._max_retries + 1):
            result: object = _MISSING
            failure: CorpusLifecycleStoreFailure | None = None
            try:
                result = await self._store.read_active_snapshot(
                    corpus_id, timeout_seconds=self._timeout_seconds
                )
            except asyncio.CancelledError:
                raise
            except CorpusLifecycleStoreFailure as error:
                failure = error
            except Exception:
                failure = CorpusLifecycleStoreFailure("permanent")
            if failure is None:
                snapshot = _validate_active_snapshot(result)
                if (
                    snapshot.pointer is not None
                    and snapshot.pointer.corpus_id != corpus_id
                ):
                    raise CorpusLifecycleError("malformed_store") from None
                return snapshot
            if failure.code == "malformed":
                raise CorpusLifecycleError("malformed_store") from None
            if failure.code != "retryable" or attempt == self._max_retries:
                raise CorpusLifecycleError("store_unavailable") from None
            await self._sleep_retry()
        raise AssertionError("unreachable")

    async def _verified_evidence(
        self,
        corpus: ExactCorpusReference,
        identity: AttestationIdentity,
        policy: _PolicySnapshot,
    ) -> VerifiedLifecycleEvidence:
        verifier = _select_verifier(policy, identity)
        raw: object = _MISSING
        persistence_error: CandidatePersistenceError | None = None
        try:
            raw = await self._verify_attested_candidate(corpus, identity, verifier)
        except asyncio.CancelledError:
            raise
        except CandidatePersistenceError as error:
            persistence_error = error
        except Exception:
            persistence_error = CandidatePersistenceError("malformed_store")
        if persistence_error is not None:
            mapping: dict[str, CorpusLifecycleErrorCode] = {
                "attestation_failed": "candidate_unavailable",
                "store_unavailable": "store_unavailable",
                "candidate_conflict": "malformed_store",
                "store_bounds_exceeded": "malformed_store",
                "malformed_store": "malformed_store",
                "invalid_plan": "malformed_store",
                "unsupported_embedding": "malformed_store",
            }
            raise CorpusLifecycleError(mapping[persistence_error.code]) from None
        evidence: VerifiedLifecycleEvidence | None = None
        try:
            if (
                type(raw) is not VerifiedCandidateEvidence
                or object.__getattribute__(raw, "__pydantic_extra__")
            ):
                raise TypeError
            copied = {
                name: getattr(raw, name) for name in VerifiedCandidateEvidence.model_fields
            }
            accepted = VerifiedCandidateEvidence.model_validate(copied)
            if (
                accepted.signature_algorithm_id != identity.algorithm_id
                or accepted.signing_key_id != identity.key_id
            ):
                raise ValueError
            evidence = VerifiedLifecycleEvidence.model_validate(
                {name: getattr(accepted, name) for name in VerifiedLifecycleEvidence.model_fields}
            )
        except Exception:
            pass
        if evidence is None:
            raise CorpusLifecycleError("malformed_store") from None
        if evidence.corpus != corpus:
            raise CorpusLifecycleError("malformed_store") from None
        if (
            evidence.embedding_identity != self._expected_embedding_identity
            or evidence.embedding_dimensions != self._expected_embedding_dimensions
        ):
            raise CorpusLifecycleError("candidate_unavailable") from None
        return evidence

    def _require_allowlisted_record(
        self, record: CorpusLifecycleRecord, policy: _PolicySnapshot
    ) -> None:
        _select_verifier(policy, _identity_from_record(record))

    async def mark_ready(
        self, request: MarkReadyRequest, trust_policy: AttestationTrustPolicy
    ) -> LifecycleMutationReceipt:
        validated = _revalidate_model(request, MarkReadyRequest)
        policy = _capture_policy(trust_policy)
        evidence = await self._verified_evidence(
            validated.corpus, validated.trusted_identity, policy
        )
        replacement = _record_from_evidence(evidence)
        audit = _ready_audit(replacement)
        initial = await self._read_lifecycle(validated.corpus)
        if initial.record is not None:
            if initial == LifecycleSnapshot(record=replacement, audit=audit):
                return LifecycleMutationReceipt(
                    contract_version="1.0",
                    action="ready",
                    disposition="confirmed",
                    subject=validated.corpus,
                    target=None,
                    resulting_revision=0,
                )
            raise CorpusLifecycleError("lifecycle_conflict") from None
        return await self._commit_ready(replacement, audit)

    async def _commit_ready(
        self, replacement: CorpusLifecycleRecord, audit: LifecycleAuditRecord
    ) -> LifecycleMutationReceipt:
        intended = LifecycleSnapshot(record=replacement, audit=audit)
        for attempt in range(self._max_retries + 1):
            outcome: StoreMutationResult | None = None
            failure: CorpusLifecycleStoreFailure | None = None
            try:
                outcome = await self._store.commit_ready(
                    True, replacement, audit, timeout_seconds=self._timeout_seconds
                )
                if outcome not in {"applied", "conflict"}:
                    raise CorpusLifecycleStoreFailure("malformed")
            except asyncio.CancelledError:
                raise
            except CorpusLifecycleStoreFailure as error:
                failure = error
            except Exception:
                failure = CorpusLifecycleStoreFailure("permanent")
            if outcome == "applied":
                disposition: LifecycleDisposition = "applied"
                break
            if outcome == "conflict":
                confirmed = await self._read_lifecycle(replacement.corpus)
                if confirmed == intended:
                    disposition = "confirmed"
                    break
                raise CorpusLifecycleError("lifecycle_conflict") from None
            if failure is not None and failure.code == "commit_outcome_unknown":
                confirmed = await self._read_lifecycle(replacement.corpus)
                if confirmed == intended:
                    disposition = "confirmed"
                    break
                if confirmed.record is not None:
                    raise CorpusLifecycleError("lifecycle_conflict") from None
                if attempt == self._max_retries:
                    raise CorpusLifecycleError("store_unavailable") from None
                await self._sleep_retry()
                continue
            if failure is not None and failure.code == "malformed":
                raise CorpusLifecycleError("malformed_store") from None
            if failure is not None and failure.code == "retryable" and attempt < self._max_retries:
                await self._sleep_retry()
                continue
            raise CorpusLifecycleError("store_unavailable") from None
        else:
            raise AssertionError("unreachable")
        return LifecycleMutationReceipt(
            contract_version="1.0",
            action="ready",
            disposition=disposition,
            subject=replacement.corpus,
            target=None,
            resulting_revision=0,
        )

    async def switch_active(
        self, request: SwitchActiveRequest, trust_policy: AttestationTrustPolicy
    ) -> LifecycleMutationReceipt:
        validated = _revalidate_model(request, SwitchActiveRequest)
        policy = _capture_policy(trust_policy)
        current = await self._read_active(validated.target.corpus_id)
        expected = validated.expected
        if current.pointer is not None:
            current_record = cast(LifecycleSnapshot, current.target_lifecycle).record
            assert current_record is not None
            self._require_allowlisted_record(current_record, policy)
        intended_revision = 0 if expected is None else expected.revision + 1
        replay_candidate = current.pointer is not None and (
            current.pointer.target == validated.target
            and current.pointer.revision == intended_revision
            and current.audit is not None
            and current.audit.action == validated.action
        )
        if expected is None:
            if current.pointer is not None and not replay_candidate:
                raise CorpusLifecycleError("lifecycle_conflict") from None
        elif (
            not replay_candidate
            and (
                current.pointer is None
                or current.pointer.target != expected.target
                or current.pointer.revision != expected.revision
            )
        ):
            raise CorpusLifecycleError("lifecycle_conflict") from None
        target_snapshot = await self._read_lifecycle(validated.target)
        target_record = target_snapshot.record
        if target_record is None or target_record.state != "ready":
            raise CorpusLifecycleError("lifecycle_conflict") from None
        target_identity = _identity_from_record(target_record)
        verified = await self._verified_evidence(validated.target, target_identity, policy)
        if verified != target_record.evidence:
            raise CorpusLifecycleError("lifecycle_conflict") from None
        replacement = ActiveCorpusPointer(
            schema_version="1.0",
            contract_version="1.0",
            corpus_id=validated.target.corpus_id,
            target=validated.target,
            revision=intended_revision,
            target_lifecycle_revision=0,
            attestation_payload_sha256=verified.attestation_payload_sha256,
        )
        audit = LifecycleAuditRecord(
            schema_version="1.0",
            contract_version="1.0",
            action=validated.action,
            subject=None,
            before=None if expected is None else expected.target,
            after=validated.target,
            resulting_revision=intended_revision,
            authorizing_attestation_payload_sha256=verified.attestation_payload_sha256,
        )
        intended = ActivePointerSnapshot(
            pointer=replacement,
            audit=audit,
            target_lifecycle=target_snapshot,
        )
        if replay_candidate:
            if current == intended:
                return LifecycleMutationReceipt(
                    contract_version="1.0",
                    action=validated.action,
                    disposition="confirmed",
                    subject=None,
                    target=validated.target,
                    resulting_revision=intended_revision,
                )
            raise CorpusLifecycleError("lifecycle_conflict") from None
        return await self._commit_switch(
            current, replacement, target_record, audit, validated.action
        )

    async def _commit_switch(
        self,
        expected: ActivePointerSnapshot,
        replacement: ActiveCorpusPointer,
        target_ready: CorpusLifecycleRecord,
        audit: LifecycleAuditRecord,
        action: Literal["promote", "rollback"],
    ) -> LifecycleMutationReceipt:
        intended = ActivePointerSnapshot(
            pointer=replacement,
            audit=audit,
            target_lifecycle=LifecycleSnapshot(
                record=target_ready,
                audit=_ready_audit(target_ready),
            ),
        )
        for attempt in range(self._max_retries + 1):
            outcome: StoreMutationResult | None = None
            failure: CorpusLifecycleStoreFailure | None = None
            try:
                outcome = await self._store.compare_and_swap_active(
                    expected,
                    replacement,
                    target_ready,
                    audit,
                    timeout_seconds=self._timeout_seconds,
                )
                if outcome not in {"applied", "conflict"}:
                    raise CorpusLifecycleStoreFailure("malformed")
            except asyncio.CancelledError:
                raise
            except CorpusLifecycleStoreFailure as error:
                failure = error
            except Exception:
                failure = CorpusLifecycleStoreFailure("permanent")
            if outcome == "applied":
                disposition: LifecycleDisposition = "applied"
                break
            if outcome == "conflict":
                confirmed = await self._read_active(replacement.corpus_id)
                if confirmed == intended:
                    disposition = "confirmed"
                    break
                raise CorpusLifecycleError("lifecycle_conflict") from None
            if failure is not None and failure.code == "commit_outcome_unknown":
                confirmed = await self._read_active(replacement.corpus_id)
                if confirmed == intended:
                    disposition = "confirmed"
                    break
                if confirmed != expected:
                    raise CorpusLifecycleError("lifecycle_conflict") from None
                if attempt == self._max_retries:
                    raise CorpusLifecycleError("store_unavailable") from None
                await self._sleep_retry()
                continue
            if failure is not None and failure.code == "malformed":
                raise CorpusLifecycleError("malformed_store") from None
            if failure is not None and failure.code == "retryable" and attempt < self._max_retries:
                await self._sleep_retry()
                continue
            raise CorpusLifecycleError("store_unavailable") from None
        else:
            raise AssertionError("unreachable")
        return LifecycleMutationReceipt(
            contract_version="1.0",
            action=action,
            disposition=disposition,
            subject=None,
            target=replacement.target,
            resulting_revision=replacement.revision,
        )

    async def remove_version(
        self, request: RemoveCorpusVersionRequest
    ) -> LifecycleMutationReceipt:
        validated = _revalidate_model(request, RemoveCorpusVersionRequest)
        current = await self._read_lifecycle(validated.corpus)
        if current.record is None:
            raise CorpusLifecycleError("lifecycle_conflict") from None
        if current.record.state == "logically_removed":
            expected_removed = current.record
            active = await self._read_active(validated.corpus.corpus_id)
            if active.pointer is not None and active.pointer.target == validated.corpus:
                raise CorpusLifecycleError("active_version_forbidden") from None
            if current.audit == _remove_audit(expected_removed):
                return LifecycleMutationReceipt(
                    contract_version="1.0",
                    action="remove",
                    disposition="confirmed",
                    subject=validated.corpus,
                    target=None,
                    resulting_revision=1,
                )
            raise CorpusLifecycleError("lifecycle_conflict") from None
        active = await self._read_active(validated.corpus.corpus_id)
        if active.pointer is not None and active.pointer.target == validated.corpus:
            raise CorpusLifecycleError("active_version_forbidden") from None
        replacement = _record_from_evidence(
            current.record.evidence,
            state="logically_removed",
        )
        audit = _remove_audit(replacement)
        return await self._commit_remove(current, replacement, active, audit)

    async def _commit_remove(
        self,
        expected: LifecycleSnapshot,
        replacement: CorpusLifecycleRecord,
        expected_active: ActivePointerSnapshot,
        audit: LifecycleAuditRecord,
    ) -> LifecycleMutationReceipt:
        intended = LifecycleSnapshot(record=replacement, audit=audit)
        for attempt in range(self._max_retries + 1):
            outcome: StoreMutationResult | None = None
            failure: CorpusLifecycleStoreFailure | None = None
            try:
                outcome = await self._store.compare_and_remove(
                    expected,
                    replacement,
                    expected_active,
                    audit,
                    timeout_seconds=self._timeout_seconds,
                )
                if outcome not in {"applied", "conflict"}:
                    raise CorpusLifecycleStoreFailure("malformed")
            except asyncio.CancelledError:
                raise
            except CorpusLifecycleStoreFailure as error:
                failure = error
            except Exception:
                failure = CorpusLifecycleStoreFailure("permanent")
            if outcome == "applied":
                disposition: LifecycleDisposition = "applied"
                break
            if outcome == "conflict":
                confirmed = await self._read_lifecycle(replacement.corpus)
                if confirmed == intended:
                    disposition = "confirmed"
                    break
                raise CorpusLifecycleError("lifecycle_conflict") from None
            if failure is not None and failure.code == "commit_outcome_unknown":
                confirmed = await self._read_lifecycle(replacement.corpus)
                if confirmed == intended:
                    disposition = "confirmed"
                    break
                if confirmed != expected:
                    raise CorpusLifecycleError("lifecycle_conflict") from None
                if attempt == self._max_retries:
                    raise CorpusLifecycleError("store_unavailable") from None
                await self._sleep_retry()
                continue
            if failure is not None and failure.code == "malformed":
                raise CorpusLifecycleError("malformed_store") from None
            if failure is not None and failure.code == "retryable" and attempt < self._max_retries:
                await self._sleep_retry()
                continue
            raise CorpusLifecycleError("store_unavailable") from None
        else:
            raise AssertionError("unreachable")
        return LifecycleMutationReceipt(
            contract_version="1.0",
            action="remove",
            disposition=disposition,
            subject=replacement.corpus,
            target=None,
            resulting_revision=1,
        )

    async def resolve_active_state(
        self, corpus_id: str, trust_policy: AttestationTrustPolicy
    ) -> ResolvedActiveState:
        validated_id = _validate_corpus_id(corpus_id)
        policy = _capture_policy(trust_policy)
        snapshot = await self._read_active(validated_id)
        if snapshot.pointer is None:
            raise CorpusLifecycleError("no_active_version") from None
        lifecycle = cast(LifecycleSnapshot, snapshot.target_lifecycle)
        record = lifecycle.record
        if record is None or record.state != "ready":
            raise CorpusLifecycleError("malformed_store") from None
        self._require_allowlisted_record(record, policy)
        return ResolvedActiveState(
            target=snapshot.pointer.target,
            pointer_revision=snapshot.pointer.revision,
            lifecycle_revision=0,
            evidence=record.evidence,
        )

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            if self._owns_store:
                failed = False
                try:
                    await self._store.aclose()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    failed = True
                if failed:
                    return
