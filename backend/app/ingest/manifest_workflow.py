"""Pure reviewed-manifest policy, draft, and dry-run workflow."""

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import date, timedelta
from typing import Annotated, Any, Literal, Self, get_args, get_origin
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetCoreSchemaHandler,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)
from pydantic_core import CoreSchema, core_schema

from app.ingest.planner import (
    MAX_DOCUMENTS,
    MAX_PROVENANCE_OWNER_CHARS,
    CandidateDocumentSnapshot,
    CandidateEmbeddingFunction,
    CandidateIngestionPlan,
    CandidateSourceSnapshot,
    EmbeddingSpecification,
    IngestionPlanError,
    plan_candidate,
)
from app.ingest.provenance import (
    MAX_CITATION_TITLE_CHARS,
    MAX_MANIFEST_BYTES,
    CorpusProvenanceError,
    SourceProvenance,
    parse_provenance_manifest,
)
from app.retrieval_contracts import ExactCorpusReference

MAX_POLICY_IDENTITIES = 1_024
MAX_AUTHORITY_CHARS = MAX_PROVENANCE_OWNER_CHARS
MAX_REVIEW_AGE_DAYS = 36_600
_SHA256_LENGTH = 64
_MISSING = object()


class _DuplicateJsonKeyError(ValueError):
    pass


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError
        result[key] = value
    return result

ManifestWorkflowErrorCode = Literal[
    "invalid_model",
    "invalid_policy",
    "invalid_manifest",
    "unapproved_source",
    "review_out_of_window",
    "disallowed_origin",
    "claim_mismatch",
    "conflicting_claim",
    "invalid_draft",
    "approval_incomplete",
]

_ERROR_MESSAGES: dict[ManifestWorkflowErrorCode, str] = {
    "invalid_model": "The manifest workflow model is invalid.",
    "invalid_policy": "The manifest review policy is invalid.",
    "invalid_manifest": "The provenance manifest is invalid.",
    "unapproved_source": "A manifest source lacks approved authority.",
    "review_out_of_window": "A manifest review is outside the allowed window.",
    "disallowed_origin": "A manifest source origin is not allowed.",
    "claim_mismatch": "A manifest source does not match its claimed authority.",
    "conflicting_claim": "The manifest policy contains conflicting authority claims.",
    "invalid_draft": "The manifest draft is invalid.",
    "approval_incomplete": "The manifest draft lacks complete human approval.",
}


class ManifestWorkflowError(Exception):
    """Fixed, content-free manifest workflow failure."""

    def __init__(self, code: ManifestWorkflowErrorCode) -> None:
        if not isinstance(code, str) or code not in _ERROR_MESSAGES:
            raise ValueError("Unsupported manifest workflow error code.") from None
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])

    def __repr__(self) -> str:
        return f"ManifestWorkflowError(code={self.code!r})"

    def errors(self, **_: object) -> list[dict[str, object]]:
        return [
            {
                "type": "manifest_workflow_error",
                "loc": (),
                "msg": _ERROR_MESSAGES[self.code],
                "code": self.code,
            }
        ]

    def json(self, *, indent: int | None = None, **_: object) -> str:
        return json.dumps(
            self.errors(),
            indent=indent,
            separators=None if indent is not None else (",", ":"),
        )


def _content_free_validation(
    model: type["ManifestWorkflowModel"],
    value: Any,
    handler: Callable[[Any], Any],
    mode: str,
) -> Any:
    result: Any = _MISSING
    try:
        if isinstance(value, Mapping) and any(key not in model.model_fields for key in value):
            raise ValueError
        if mode in {"json", "string"} and isinstance(value, Mapping):
            value = {
                key: _json_to_strict_python(model.model_fields[key].annotation, item)
                for key, item in value.items()
            }
        result = handler(value)
    except ManifestWorkflowError:
        raise
    except Exception:
        pass
    if result is _MISSING:
        raise ManifestWorkflowError("invalid_model") from None
    return result


class ManifestWorkflowModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, hide_input_in_errors=True)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        schema = handler(source_type)
        return core_schema.with_info_wrap_validator_function(
            lambda value, validator, info: _content_free_validation(
                cls, value, validator, info.mode
            ),
            schema,
        )

    @classmethod
    def _validate_workflow_model(cls, values: Any) -> Self:
        result: Self | None = None
        try:
            result = cls.model_validate(values)
        except Exception:
            pass
        if result is None:
            raise ManifestWorkflowError("invalid_model") from None
        return result

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        values: dict[str, Any] | None = None
        fields: set[str] | None = None
        try:
            source = super().model_copy(deep=deep)
            values = {field: getattr(source, field) for field in type(self).model_fields}
            fields = set(source.model_fields_set)
            if update is not None:
                values.update(update)
                fields.update(update)
        except Exception:
            pass
        if values is None or fields is None:
            raise ManifestWorkflowError("invalid_model") from None
        validated = type(self)._validate_workflow_model(values)
        object.__setattr__(validated, "__pydantic_fields_set__", fields)
        return validated

    @classmethod
    def model_construct(
        cls, _fields_set: set[str] | None = None, **values: Any
    ) -> Self:
        invalid_fields = False
        try:
            if _fields_set is not None and not set(_fields_set).issubset(cls.model_fields):
                invalid_fields = True
        except Exception:
            invalid_fields = True
        if invalid_fields:
            raise ManifestWorkflowError("invalid_model") from None
        validated = cls._validate_workflow_model(values)
        if _fields_set is not None:
            object.__setattr__(validated, "__pydantic_fields_set__", set(_fields_set))
        return validated

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
        values: dict[str, Any] | None = None
        try:
            values = self.model_dump(include=include, exclude=exclude, round_trip=True)
            if update is not None:
                values.update(update)
        except Exception:
            pass
        if values is None:
            raise ManifestWorkflowError("invalid_model") from None
        return type(self)._validate_workflow_model(values)


def _json_to_strict_python(annotation: Any, value: Any) -> Any:
    if annotation is bytes and isinstance(value, str):
        return value.encode("utf-8", errors="strict")
    if annotation is date and isinstance(value, str):
        return date.fromisoformat(value)
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is tuple and isinstance(value, list):
        item_annotation = arguments[0] if arguments else Any
        return tuple(_json_to_strict_python(item_annotation, item) for item in value)
    if (
        isinstance(annotation, type)
        and issubclass(annotation, ManifestWorkflowModel)
        and isinstance(value, Mapping)
    ):
        return {
            key: (
                _json_to_strict_python(annotation.model_fields[key].annotation, item)
                if key in annotation.model_fields
                else item
            )
            for key, item in value.items()
        }
    return value


def _strict_text(value: str, *, maximum: int = MAX_AUTHORITY_CHARS) -> str:
    if not value or value != value.strip() or len(value) > maximum:
        raise ValueError
    value.encode("utf-8", errors="strict")
    return value


def _sha256_hex(value: str) -> str:
    if len(value) != _SHA256_LENGTH or any(c not in "0123456789abcdef" for c in value):
        raise ValueError
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8", errors="strict")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _canonical_origin(value: str, *, origin_only: bool) -> str:
    if any(character.isspace() for character in value):
        raise ValueError
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError from error
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or not value.startswith(f"{parsed.scheme}://")
    ):
        raise ValueError
    if origin_only and (parsed.path or parsed.query or parsed.fragment):
        raise ValueError
    host = parsed.hostname
    try:
        host.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError from error
    rendered_host = f"[{host}]" if ":" in host else host
    default_port = (parsed.scheme == "http" and port == 80) or (
        parsed.scheme == "https" and port == 443
    )
    canonical = f"{parsed.scheme}://{rendered_host}"
    if port is not None and not default_port:
        canonical += f":{port}"
    raw_origin = f"{parsed.scheme}://{parsed.netloc}"
    if raw_origin != canonical:
        raise ValueError
    if origin_only and value != canonical:
        raise ValueError
    return canonical


class OriginAuthorityClaim(ManifestWorkflowModel):
    origin: Annotated[str, Field(min_length=1, max_length=2_048)]
    authority: Annotated[str, Field(min_length=1, max_length=MAX_AUTHORITY_CHARS)]

    @field_validator("origin")
    @classmethod
    def validate_origin(cls, value: str) -> str:
        _canonical_origin(value, origin_only=True)
        return value

    @field_validator("authority")
    @classmethod
    def validate_authority(cls, value: str) -> str:
        return _strict_text(value)


class ManifestReviewPolicy(ManifestWorkflowModel):
    evaluation_date: date
    max_review_age_days: Annotated[StrictInt, Field(ge=0, le=MAX_REVIEW_AGE_DAYS)]
    approved_authorities: Annotated[
        tuple[str, ...], Field(min_length=1, max_length=MAX_POLICY_IDENTITIES)
    ]
    claims: Annotated[
        tuple[OriginAuthorityClaim, ...], Field(min_length=1, max_length=MAX_POLICY_IDENTITIES)
    ]

    @field_validator("approved_authorities")
    @classmethod
    def validate_authorities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if type(value) is not tuple:
            raise ValueError
        for authority in value:
            if type(authority) is not str:
                raise ValueError
            _strict_text(authority)
        if len(value) != len(set(value)):
            raise ValueError
        return value


class ReviewedManifestEntry(ManifestWorkflowModel):
    relative_path: Annotated[str, Field(min_length=1, max_length=2_048)]
    title: Annotated[str, Field(min_length=1, max_length=MAX_CITATION_TITLE_CHARS)]
    url: Annotated[str, Field(min_length=1, max_length=MAX_MANIFEST_BYTES)]
    source_sha256: str
    authority: Annotated[str, Field(min_length=1, max_length=MAX_AUTHORITY_CHARS)]
    reviewed_at: date
    entry_sha256: str

    @field_validator("relative_path", "title", "authority")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _strict_text(value, maximum=2_048)

    @field_validator("source_sha256", "entry_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        return _sha256_hex(value)

    @model_validator(mode="after")
    def validate_identity(self) -> "ReviewedManifestEntry":
        material = {
            "relative_path": self.relative_path,
            "title": self.title,
            "url": self.url,
            "source_sha256": self.source_sha256,
            "authority": self.authority,
            "reviewed_at": self.reviewed_at.isoformat(),
            "public": True,
        }
        if self.entry_sha256 != _digest(material):
            raise ValueError
        return self


class ReviewedManifestSnapshot(ManifestWorkflowModel):
    manifest_bytes: Annotated[bytes, Field(max_length=MAX_MANIFEST_BYTES)]
    entries: Annotated[
        tuple[ReviewedManifestEntry, ...], Field(min_length=1, max_length=MAX_DOCUMENTS)
    ]
    policy: ManifestReviewPolicy
    manifest_sha256: str
    policy_sha256: str
    snapshot_sha256: str

    @field_validator("manifest_sha256", "policy_sha256", "snapshot_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        return _sha256_hex(value)

    @model_validator(mode="after")
    def validate_identity(self) -> "ReviewedManifestSnapshot":
        expected_policy_sha256 = _digest(_policy_material(self.policy))
        if self.policy_sha256 != expected_policy_sha256:
            raise ValueError
        claims = _validated_claims(self.policy)
        oldest: date | None = None
        try:
            oldest = self.policy.evaluation_date - timedelta(
                days=self.policy.max_review_age_days
            )
        except (OverflowError, ValueError):
            pass
        if oldest is None:
            raise ValueError
        approved = set(self.policy.approved_authorities)
        for entry in self.entries:
            if (
                entry.reviewed_at > self.policy.evaluation_date
                or entry.reviewed_at < oldest
                or entry.authority not in approved
            ):
                raise ValueError
            origin: str | None = None
            try:
                origin = _canonical_origin(entry.url, origin_only=False)
            except ValueError:
                pass
            if origin is None or claims.get(origin) != entry.authority:
                raise ValueError
        paths = [entry.relative_path for entry in self.entries]
        if paths != sorted(paths, key=str.encode) or len(paths) != len(set(paths)):
            raise ValueError
        documents = {
            entry.relative_path: {
                "title": entry.title,
                "url": entry.url,
                "sha256": entry.source_sha256,
                "owner": entry.authority,
                "reviewed_at": entry.reviewed_at.isoformat(),
                "public": True,
            }
            for entry in self.entries
        }
        canonical_manifest = _canonical_json({"version": 1, "documents": documents})
        if self.manifest_bytes != canonical_manifest:
            raise ValueError
        if self.manifest_sha256 != hashlib.sha256(canonical_manifest).hexdigest():
            raise ValueError
        if self.snapshot_sha256 != _digest(
            {
                "namespace": "cairn-reviewed-manifest-v1",
                "manifest_sha256": self.manifest_sha256,
                "policy_sha256": self.policy_sha256,
            }
        ):
            raise ValueError
        return self


class ManifestOperation(ManifestWorkflowModel):
    kind: Literal["create", "update", "remove"]
    relative_path: Annotated[str, Field(min_length=1, max_length=2_048)]
    before_entry_sha256: str | None
    after_entry_sha256: str | None

    @field_validator("before_entry_sha256", "after_entry_sha256")
    @classmethod
    def validate_optional_sha256(cls, value: str | None) -> str | None:
        return None if value is None else _sha256_hex(value)

    @model_validator(mode="after")
    def validate_shape(self) -> "ManifestOperation":
        expected = {
            "create": (None, True),
            "update": (True, True),
            "remove": (True, None),
        }[self.kind]
        actual = (
            None if self.before_entry_sha256 is None else True,
            None if self.after_entry_sha256 is None else True,
        )
        if actual != expected:
            raise ValueError
        return self


class ManifestDryRunPlan(ManifestWorkflowModel):
    before_snapshot_sha256: str
    after_snapshot_sha256: str
    create: tuple[ManifestOperation, ...]
    update: tuple[ManifestOperation, ...]
    remove: tuple[ManifestOperation, ...]
    plan_sha256: str

    @field_validator("before_snapshot_sha256", "after_snapshot_sha256", "plan_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        return _sha256_hex(value)

    @model_validator(mode="after")
    def validate_plan(self) -> "ManifestDryRunPlan":
        groups = (("create", self.create), ("update", self.update), ("remove", self.remove))
        all_paths: list[str] = []
        for kind, operations in groups:
            paths = [operation.relative_path for operation in operations]
            if (
                any(operation.kind != kind for operation in operations)
                or paths != sorted(paths, key=str.encode)
                or len(paths) != len(set(paths))
            ):
                raise ValueError
            all_paths.extend(paths)
        if len(all_paths) != len(set(all_paths)):
            raise ValueError
        material = {
            "namespace": "cairn-manifest-dry-run-v1",
            "before_snapshot_sha256": self.before_snapshot_sha256,
            "after_snapshot_sha256": self.after_snapshot_sha256,
            "create": [operation.model_dump(mode="json") for operation in self.create],
            "update": [operation.model_dump(mode="json") for operation in self.update],
            "remove": [operation.model_dump(mode="json") for operation in self.remove],
        }
        if self.plan_sha256 != _digest(material):
            raise ValueError
        return self


class ManifestDocumentApproval(ManifestWorkflowModel):
    title: Annotated[str, Field(min_length=1, max_length=MAX_CITATION_TITLE_CHARS)]
    url: Annotated[str, Field(min_length=1, max_length=MAX_MANIFEST_BYTES)]
    authority: Annotated[str, Field(min_length=1, max_length=MAX_AUTHORITY_CHARS)]
    reviewed_at: date
    approved: StrictBool

    @field_validator("title", "authority")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _strict_text(value)


def _revalidate[T: BaseModel](value: object, model: type[T]) -> T:
    result: T | None = None
    try:
        if type(value) is not model:
            raise TypeError
        values = {field: getattr(value, field) for field in model.model_fields}
        result = model.model_validate(values)
    except ManifestWorkflowError:
        raise
    except Exception:
        pass
    if result is None:
        raise ManifestWorkflowError("invalid_model") from None
    return result


def _policy_material(policy: ManifestReviewPolicy) -> dict[str, object]:
    return {
        "evaluation_date": policy.evaluation_date.isoformat(),
        "max_review_age_days": policy.max_review_age_days,
        "approved_authorities": sorted(policy.approved_authorities),
        "claims": [
            {"origin": claim.origin, "authority": claim.authority}
            for claim in sorted(policy.claims, key=lambda item: (item.origin, item.authority))
        ],
    }


def _validated_claims(policy: ManifestReviewPolicy) -> dict[str, str]:
    claims: dict[str, str] = {}
    approved = set(policy.approved_authorities)
    for claim in policy.claims:
        if claim.authority not in approved:
            raise ManifestWorkflowError("invalid_policy") from None
        if claim.origin in claims:
            raise ManifestWorkflowError("conflicting_claim") from None
        claims[claim.origin] = claim.authority
    return claims


def _entry_material(path: str, source: SourceProvenance) -> dict[str, object]:
    return {
        "relative_path": path,
        "title": source.title,
        "url": source.url,
        "source_sha256": source.sha256,
        "authority": source.owner,
        "reviewed_at": source.reviewed_at.isoformat(),
        "public": True,
    }


def validate_reviewed_manifest(
    source: CandidateSourceSnapshot, policy: ManifestReviewPolicy
) -> ReviewedManifestSnapshot:
    """Validate exact source bytes under an explicit review policy."""
    policy = _revalidate(policy, ManifestReviewPolicy)
    validated_source: CandidateSourceSnapshot | None = None
    try:
        if type(source) is not CandidateSourceSnapshot:
            raise TypeError
        validated_source = CandidateSourceSnapshot.model_validate(
            source.model_dump(round_trip=True)
        )
    except Exception:
        pass
    if validated_source is None:
        raise ManifestWorkflowError("invalid_model") from None
    source = validated_source
    claims = _validated_claims(policy)
    documents = {item.relative_path: item.content for item in source.documents}
    parsed: dict[str, SourceProvenance] | None = None
    try:
        parsed = parse_provenance_manifest(source.manifest_bytes, documents)
    except CorpusProvenanceError:
        pass
    if parsed is None:
        raise ManifestWorkflowError("invalid_manifest") from None

    oldest: date | None = None
    try:
        oldest = policy.evaluation_date - timedelta(days=policy.max_review_age_days)
    except (OverflowError, ValueError):
        pass
    if oldest is None:
        raise ManifestWorkflowError("invalid_policy") from None
    approved = set(policy.approved_authorities)
    entries: list[ReviewedManifestEntry] = []
    manifest_documents: dict[str, object] = {}
    for path, provenance in sorted(parsed.items(), key=lambda item: item[0].encode("utf-8")):
        if provenance.reviewed_at > policy.evaluation_date or provenance.reviewed_at < oldest:
            raise ManifestWorkflowError("review_out_of_window") from None
        if provenance.owner not in approved:
            raise ManifestWorkflowError("unapproved_source") from None
        origin: str | None = None
        try:
            origin = _canonical_origin(provenance.url, origin_only=False)
        except ValueError:
            pass
        if origin is None:
            raise ManifestWorkflowError("disallowed_origin") from None
        authority = claims.get(origin)
        if authority is None:
            raise ManifestWorkflowError("disallowed_origin") from None
        if authority != provenance.owner:
            raise ManifestWorkflowError("claim_mismatch") from None
        material = _entry_material(path, provenance)
        entry_sha256 = _digest(material)
        entries.append(
            ReviewedManifestEntry(
                relative_path=path,
                title=provenance.title,
                url=provenance.url,
                source_sha256=provenance.sha256,
                authority=provenance.owner,
                reviewed_at=provenance.reviewed_at,
                entry_sha256=entry_sha256,
            )
        )
        manifest_documents[path] = {
            "title": provenance.title,
            "url": provenance.url,
            "sha256": provenance.sha256,
            "owner": provenance.owner,
            "reviewed_at": provenance.reviewed_at.isoformat(),
            "public": True,
        }
    canonical_manifest = _canonical_json({"version": 1, "documents": manifest_documents})
    manifest_sha256 = hashlib.sha256(canonical_manifest).hexdigest()
    policy_sha256 = _digest(_policy_material(policy))
    snapshot_sha256 = _digest(
        {
            "namespace": "cairn-reviewed-manifest-v1",
            "manifest_sha256": manifest_sha256,
            "policy_sha256": policy_sha256,
        }
    )
    return ReviewedManifestSnapshot(
        manifest_bytes=canonical_manifest,
        entries=tuple(entries),
        policy=policy,
        manifest_sha256=manifest_sha256,
        policy_sha256=policy_sha256,
        snapshot_sha256=snapshot_sha256,
    )


def plan_manifest_changes(
    before: ReviewedManifestSnapshot, after: ReviewedManifestSnapshot
) -> ManifestDryRunPlan:
    """Return a deterministic content-free create/update/remove dry-run."""
    before = _revalidate(before, ReviewedManifestSnapshot)
    after = _revalidate(after, ReviewedManifestSnapshot)
    old = {entry.relative_path: entry for entry in before.entries}
    new = {entry.relative_path: entry for entry in after.entries}
    creates = tuple(
        ManifestOperation(
            kind="create",
            relative_path=path,
            before_entry_sha256=None,
            after_entry_sha256=new[path].entry_sha256,
        )
        for path in sorted(new.keys() - old.keys(), key=str.encode)
    )
    updates = tuple(
        ManifestOperation(
            kind="update",
            relative_path=path,
            before_entry_sha256=old[path].entry_sha256,
            after_entry_sha256=new[path].entry_sha256,
        )
        for path in sorted(old.keys() & new.keys(), key=str.encode)
        if old[path].entry_sha256 != new[path].entry_sha256
    )
    removes = tuple(
        ManifestOperation(
            kind="remove",
            relative_path=path,
            before_entry_sha256=old[path].entry_sha256,
            after_entry_sha256=None,
        )
        for path in sorted(old.keys() - new.keys(), key=str.encode)
    )
    material = {
        "namespace": "cairn-manifest-dry-run-v1",
        "before_snapshot_sha256": before.snapshot_sha256,
        "after_snapshot_sha256": after.snapshot_sha256,
        "create": [operation.model_dump(mode="json") for operation in creates],
        "update": [operation.model_dump(mode="json") for operation in updates],
        "remove": [operation.model_dump(mode="json") for operation in removes],
    }
    return ManifestDryRunPlan(
        before_snapshot_sha256=before.snapshot_sha256,
        after_snapshot_sha256=after.snapshot_sha256,
        create=creates,
        update=updates,
        remove=removes,
        plan_sha256=_digest(material),
    )


def _validated_documents(documents: Mapping[str, bytes]) -> dict[str, bytes]:
    source: CandidateSourceSnapshot | None = None
    try:
        if not isinstance(documents, Mapping) or not 1 <= len(documents) <= MAX_DOCUMENTS:
            raise ValueError
        source = CandidateSourceSnapshot(
            manifest_bytes=b"",
            documents=tuple(
                CandidateDocumentSnapshot(relative_path=path, content=content)
                for path, content in sorted(
                    documents.items(), key=lambda item: item[0].encode("utf-8")
                )
            ),
        )
    except Exception:
        pass
    if source is None:
        raise ManifestWorkflowError("invalid_model") from None
    return {item.relative_path: item.content for item in source.documents}


def generate_manifest_draft(documents: Mapping[str, bytes]) -> bytes:
    """Generate deterministic hash evidence that is deliberately not a v1 manifest."""
    validated = _validated_documents(documents)
    return _canonical_json(
        {
            "draft_version": 1,
            "documents": {
                path: {"sha256": hashlib.sha256(content).hexdigest()}
                for path, content in validated.items()
            },
        }
    )


def _parse_draft(draft_bytes: bytes, documents: Mapping[str, bytes]) -> None:
    failed = False
    try:
        decoded = json.loads(draft_bytes.decode("utf-8"), object_pairs_hook=_strict_object)
        if not isinstance(decoded, dict) or set(decoded) != {"draft_version", "documents"}:
            raise ValueError
        if type(decoded["draft_version"]) is not int or decoded["draft_version"] != 1:
            raise ValueError
        entries = decoded["documents"]
        if not isinstance(entries, dict) or set(entries) != set(documents):
            raise ValueError
        for path, content in documents.items():
            entry = entries[path]
            if (
                not isinstance(entry, dict)
                or set(entry) != {"sha256"}
                or entry["sha256"] != hashlib.sha256(content).hexdigest()
            ):
                raise ValueError
    except Exception:
        failed = True
    if failed:
        raise ManifestWorkflowError("invalid_draft") from None


def approve_manifest_draft(
    *,
    draft_bytes: bytes,
    documents: Mapping[str, bytes],
    approvals: Mapping[str, ManifestDocumentApproval],
    policy: ManifestReviewPolicy,
) -> ReviewedManifestSnapshot:
    """Convert exact draft evidence only after complete explicit human approval."""
    validated = _validated_documents(documents)
    _parse_draft(draft_bytes, validated)
    if not isinstance(approvals, Mapping) or set(approvals) != set(validated):
        raise ManifestWorkflowError("approval_incomplete") from None
    manifest_documents: dict[str, object] = {}
    for path in validated:
        approval: ManifestDocumentApproval | None = None
        try:
            approval = _revalidate(approvals[path], ManifestDocumentApproval)
        except (KeyError, ManifestWorkflowError):
            pass
        if approval is None:
            raise ManifestWorkflowError("approval_incomplete") from None
        if approval.approved is not True:
            raise ManifestWorkflowError("approval_incomplete") from None
        manifest_documents[path] = {
            "title": approval.title,
            "url": approval.url,
            "sha256": hashlib.sha256(validated[path]).hexdigest(),
            "owner": approval.authority,
            "reviewed_at": approval.reviewed_at.isoformat(),
            "public": True,
        }
    manifest_bytes = _canonical_json({"version": 1, "documents": manifest_documents})
    source = CandidateSourceSnapshot(
        manifest_bytes=manifest_bytes,
        documents=tuple(
            CandidateDocumentSnapshot(relative_path=path, content=content)
            for path, content in validated.items()
        ),
    )
    return validate_reviewed_manifest(source, policy)


def plan_reviewed_candidate(
    *,
    corpus: ExactCorpusReference,
    source: CandidateSourceSnapshot,
    policy: ManifestReviewPolicy,
    embedding: EmbeddingSpecification,
    embed: CandidateEmbeddingFunction,
) -> CandidateIngestionPlan:
    """Require policy-valid reviewed evidence before using the existing planner."""
    reviewed = validate_reviewed_manifest(source, policy)
    reviewed_source = CandidateSourceSnapshot(
        manifest_bytes=reviewed.manifest_bytes,
        documents=source.documents,
    )
    try:
        return plan_candidate(
            corpus=corpus, source=reviewed_source, embedding=embedding, embed=embed
        )
    except IngestionPlanError:
        raise
