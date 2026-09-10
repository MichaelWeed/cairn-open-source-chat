"""Pure deterministic planning for one immutable reviewed corpus candidate."""

import hashlib
import json
import math
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from functools import partial
from typing import Annotated, Any, Literal, Protocol, Self, cast, get_args, get_origin
from urllib.parse import urlsplit

import pypdf
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

from app.ingest.chunking import chunk_text
from app.ingest.parsers import SUPPORTED_EXTENSIONS, extract_text
from app.ingest.provenance import (
    MAX_CITATION_TITLE_CHARS,
    MAX_MANIFEST_BYTES,
    SourceProvenance,
    parse_provenance_manifest,
)
from app.retrieval_contracts import (
    MAX_RETRIEVAL_DOCUMENT_CHARS,
    MAX_RETRIEVED_CHUNK_CHARS,
    ExactCorpusReference,
)

INGESTION_PLAN_CONTRACT_VERSION: Literal["1.0"] = "1.0"
NORMALIZATION_ID: Literal["cairn-nfc-lf-v1"] = "cairn-nfc-lf-v1"
CHUNKING_ID: Literal["cairn-boundary-chunks-v1:size=800:overlap=100"] = (
    "cairn-boundary-chunks-v1:size=800:overlap=100"
)
MARKDOWN_PARSER_ID = "cairn-markdown-utf8-v1"
PDF_PARSER_ID = f"cairn-pdf-pypdf-text-v1@{pypdf.__version__}"
EMBEDDING_BATCH_SIZE = 64

MAX_DOCUMENTS = 1_024
MAX_DOCUMENT_BYTES = 8_388_608
MAX_TOTAL_DOCUMENT_BYTES = 67_108_864
MAX_NORMALIZED_TEXT_CHARS = 8_388_608
MAX_TOTAL_NORMALIZED_TEXT_CHARS = 67_108_864
MAX_CHUNKS_PER_DOCUMENT = 16_384
MAX_TOTAL_CHUNKS = 65_536
MAX_EMBEDDING_IDENTITY_CHARS = 256
MAX_EMBEDDING_DIMENSIONS = 4_096
MAX_TOTAL_EMBEDDING_SCALARS = 8_388_608
MAX_PROVENANCE_OWNER_CHARS = 512

_SHA256_LENGTH = 64
_DOCUMENT_ID_LENGTH = 4 + _SHA256_LENGTH
_CHUNK_ID_LENGTH = 4 + _SHA256_LENGTH
_MISSING = object()

IngestionPlanErrorCode = Literal[
    "invalid_model",
    "invalid_manifest",
    "invalid_document",
    "unsupported_document",
    "bounds_exceeded",
    "extraction_failed",
    "embedding_failed",
    "malformed_embedding",
    "candidate_conflict",
]

_ERROR_MESSAGES: dict[IngestionPlanErrorCode, str] = {
    "invalid_model": "The ingestion plan model is invalid.",
    "invalid_manifest": "The provenance manifest is invalid.",
    "invalid_document": "A candidate document is invalid.",
    "unsupported_document": "A candidate document type is unsupported.",
    "bounds_exceeded": "The ingestion plan exceeds a supported bound.",
    "extraction_failed": "Candidate text extraction failed.",
    "embedding_failed": "Candidate embedding failed.",
    "malformed_embedding": "The embedding result is malformed.",
    "candidate_conflict": "The existing candidate conflicts with this plan.",
}


class IngestionPlanError(Exception):
    """Fixed, content-free planner failure."""

    def __init__(self, code: IngestionPlanErrorCode) -> None:
        if not isinstance(code, str) or code not in _ERROR_MESSAGES:
            raise ValueError("Unsupported ingestion plan error code.") from None
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])

    def __repr__(self) -> str:
        return f"IngestionPlanError(code={self.code!r})"

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
                "type": "ingestion_plan_error",
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


def _content_free_validation(
    model: type["IngestionPlanModel"],
    value: Any,
    handler: Callable[[Any], Any],
    mode: str,
) -> Any:
    failed = False
    result: Any = _MISSING
    try:
        if isinstance(value, Mapping) and any(key not in model.model_fields for key in value):
            failed = True
        else:
            if mode in {"json", "string"} and isinstance(value, Mapping):
                value = {
                    key: _json_to_strict_python(model.model_fields[key].annotation, item)
                    for key, item in value.items()
                }
            result = handler(value)
    except IngestionPlanError:
        raise
    except Exception:
        failed = True
    if failed or result is _MISSING:
        raise IngestionPlanError("invalid_model") from None
    return result


def _json_to_strict_python(annotation: Any, value: Any) -> Any:
    """Restore Pydantic JSON/string primitives before a strict wrapped schema."""
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
        and issubclass(annotation, IngestionPlanModel)
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


class IngestionPlanModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        hide_input_in_errors=True,
    )

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

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        del deep
        values = self.model_dump()
        if update is not None:
            values.update(update)
        return type(self).model_validate(values)


def _strict_utf8(value: str) -> str:
    failed = False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        failed = True
    if failed:
        raise ValueError("string is not strict UTF-8")
    return value


def _stable_relative_path(value: str) -> str:
    _strict_utf8(value)
    parts = value.split("/")
    if (
        not 1 <= len(value) <= MAX_RETRIEVAL_DOCUMENT_CHARS
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError("path is invalid")
    return value


def _sha256_hex(value: str) -> str:
    if len(value) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError("digest is invalid")
    return value


def _identifier(value: str, prefix: str) -> str:
    if not value.startswith(prefix):
        raise ValueError("identifier is invalid")
    _sha256_hex(value.removeprefix(prefix))
    return value


class CandidateDocumentSnapshot(IngestionPlanModel):
    relative_path: Annotated[str, Field(min_length=1, max_length=MAX_RETRIEVAL_DOCUMENT_CHARS)]
    content: Annotated[bytes, Field(min_length=1, max_length=MAX_DOCUMENT_BYTES)]

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        return _stable_relative_path(value)


class CandidateSourceSnapshot(IngestionPlanModel):
    manifest_bytes: Annotated[bytes, Field(max_length=MAX_MANIFEST_BYTES)]
    documents: Annotated[
        tuple[CandidateDocumentSnapshot, ...], Field(min_length=1, max_length=MAX_DOCUMENTS)
    ]

    @model_validator(mode="after")
    def validate_documents(self) -> "CandidateSourceSnapshot":
        paths = [document.relative_path for document in self.documents]
        if len(paths) != len(set(paths)):
            raise ValueError("duplicate document path")
        if sum(len(document.content) for document in self.documents) > MAX_TOTAL_DOCUMENT_BYTES:
            raise ValueError("document bytes exceed aggregate bound")
        return self


class EmbeddingSpecification(IngestionPlanModel):
    identity: Annotated[str, Field(min_length=1, max_length=MAX_EMBEDDING_IDENTITY_CHARS)]
    dimensions: Annotated[StrictInt, Field(ge=1, le=MAX_EMBEDDING_DIMENSIONS)]

    @field_validator("identity")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        _strict_utf8(value)
        if value != value.strip() or any(not 0x20 <= ord(character) <= 0x7E for character in value):
            raise ValueError("embedding identity is invalid")
        return value


class PlannedProvenance(IngestionPlanModel):
    title: Annotated[str, Field(min_length=1, max_length=MAX_CITATION_TITLE_CHARS)]
    url: Annotated[str, Field(min_length=1, max_length=MAX_MANIFEST_BYTES)]
    owner: Annotated[str, Field(min_length=1, max_length=MAX_PROVENANCE_OWNER_CHARS)]
    reviewed_at: date
    source_sha256: str

    @field_validator("title", "owner")
    @classmethod
    def validate_text(cls, value: str) -> str:
        _strict_utf8(value)
        if not value.strip():
            raise ValueError("provenance text is blank")
        return value

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        _strict_utf8(value)
        if any(character.isspace() for character in value):
            raise ValueError("URL is invalid")
        parsed = urlsplit(value)
        _ = parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("URL is invalid")
        return value

    @field_validator("source_sha256")
    @classmethod
    def validate_source_sha256(cls, value: str) -> str:
        return _sha256_hex(value)


class PlannedChunk(IngestionPlanModel):
    chunk_id: Annotated[str, Field(min_length=_CHUNK_ID_LENGTH, max_length=_CHUNK_ID_LENGTH)]
    document_id: Annotated[
        str, Field(min_length=_DOCUMENT_ID_LENGTH, max_length=_DOCUMENT_ID_LENGTH)
    ]
    chunk_index: Annotated[StrictInt, Field(ge=0, lt=MAX_CHUNKS_PER_DOCUMENT)]
    text: Annotated[str, Field(min_length=1, max_length=MAX_RETRIEVED_CHUNK_CHARS)]
    text_sha256: str
    embedding: Annotated[
        tuple[float, ...], Field(min_length=1, max_length=MAX_EMBEDDING_DIMENSIONS)
    ]
    embedding_sha256: str
    citation_title: Annotated[str, Field(min_length=1, max_length=MAX_CITATION_TITLE_CHARS)]
    citation_url: Annotated[str, Field(min_length=1, max_length=MAX_MANIFEST_BYTES)]

    @field_validator("chunk_id")
    @classmethod
    def validate_chunk_id(cls, value: str) -> str:
        return _identifier(value, "chk_")

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        return _identifier(value, "doc_")

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _strict_utf8(value)

    @field_validator("citation_title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        _strict_utf8(value)
        if not value.strip():
            raise ValueError("citation title is blank")
        return value

    @field_validator("citation_url")
    @classmethod
    def validate_citation_url(cls, value: str) -> str:
        return PlannedProvenance.validate_url(value)

    @field_validator("text_sha256", "embedding_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        return _sha256_hex(value)

    @field_validator("embedding", mode="before")
    @classmethod
    def validate_embedding_values(cls, value: object, info: ValidationInfo) -> object:
        sequence_type = list if info.mode == "json" else tuple
        if not isinstance(value, sequence_type) or any(
            type(scalar) is not float for scalar in cast(Sequence[object], value)
        ):
            raise ValueError("embedding is invalid")
        if any(not math.isfinite(cast(float, scalar)) for scalar in cast(Sequence[object], value)):
            raise ValueError("embedding is invalid")
        return value

    @model_validator(mode="after")
    def validate_digests(self) -> "PlannedChunk":
        if self.text_sha256 != _text_sha256(self.text):
            raise ValueError("text digest is invalid")
        if self.embedding_sha256 != _embedding_sha256(self.embedding):
            raise ValueError("embedding digest is invalid")
        expected_id = _chunk_id(self.document_id, self.chunk_index, self.text_sha256)
        if self.chunk_id != expected_id:
            raise ValueError("chunk ID is invalid")
        return self


class PlannedDocument(IngestionPlanModel):
    document_id: Annotated[
        str, Field(min_length=_DOCUMENT_ID_LENGTH, max_length=_DOCUMENT_ID_LENGTH)
    ]
    relative_path: Annotated[str, Field(min_length=1, max_length=MAX_RETRIEVAL_DOCUMENT_CHARS)]
    parser_id: str
    normalization_id: Literal["cairn-nfc-lf-v1"]
    chunking_id: Literal["cairn-boundary-chunks-v1:size=800:overlap=100"]
    normalized_text_sha256: str
    provenance: PlannedProvenance
    chunks: Annotated[
        tuple[PlannedChunk, ...], Field(min_length=1, max_length=MAX_CHUNKS_PER_DOCUMENT)
    ]
    document_plan_sha256: str

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        return _identifier(value, "doc_")

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        return _stable_relative_path(value)

    @field_validator("parser_id")
    @classmethod
    def validate_parser_id(cls, value: str) -> str:
        if value not in {MARKDOWN_PARSER_ID, PDF_PARSER_ID}:
            raise ValueError("parser identity is invalid")
        return value

    @field_validator("normalized_text_sha256", "document_plan_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        return _sha256_hex(value)

    @model_validator(mode="after")
    def validate_relationships(self) -> "PlannedDocument":
        for index, chunk in enumerate(self.chunks):
            if chunk.document_id != self.document_id or chunk.chunk_index != index:
                raise ValueError("chunk relationship is invalid")
            if (
                chunk.citation_title != self.provenance.title
                or chunk.citation_url != self.provenance.url
            ):
                raise ValueError("chunk provenance is invalid")
        if self.document_plan_sha256 != _document_plan_sha256(self):
            raise ValueError("document plan digest is invalid")
        return self


class CandidateIngestionPlan(IngestionPlanModel):
    contract_version: Literal["1.0"]
    corpus: ExactCorpusReference
    embedding: EmbeddingSpecification
    semantic_manifest_sha256: str
    documents: Annotated[tuple[PlannedDocument, ...], Field(min_length=1, max_length=MAX_DOCUMENTS)]
    document_count: Annotated[StrictInt, Field(ge=1, le=MAX_DOCUMENTS)]
    chunk_count: Annotated[StrictInt, Field(ge=1, le=MAX_TOTAL_CHUNKS)]
    plan_sha256: str

    @field_validator("semantic_manifest_sha256", "plan_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        return _sha256_hex(value)

    @model_validator(mode="after")
    def validate_plan(self) -> "CandidateIngestionPlan":
        if self.document_count != len(self.documents):
            raise ValueError("document count is invalid")
        actual_chunk_count = sum(len(document.chunks) for document in self.documents)
        if self.chunk_count != actual_chunk_count:
            raise ValueError("chunk count is invalid")
        paths = [document.relative_path for document in self.documents]
        if paths != sorted(paths, key=lambda path: path.encode("utf-8")):
            raise ValueError("document order is invalid")
        document_ids = [document.document_id for document in self.documents]
        chunk_ids = [chunk.chunk_id for document in self.documents for chunk in document.chunks]
        if len(document_ids) != len(set(document_ids)) or len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("plan identities are not unique")
        chunks = [chunk for document in self.documents for chunk in document.chunks]
        if any(len(chunk.embedding) != self.embedding.dimensions for chunk in chunks):
            raise ValueError("embedding dimensions are inconsistent")
        if len(chunks) * self.embedding.dimensions > MAX_TOTAL_EMBEDDING_SCALARS:
            raise ValueError("embedding scalar count is invalid")
        if self.plan_sha256 != _plan_sha256(self):
            raise ValueError("plan digest is invalid")
        return self


class ExistingCandidateDescriptor(IngestionPlanModel):
    contract_version: Literal["1.0"]
    corpus: ExactCorpusReference
    plan_sha256: str

    @field_validator("plan_sha256")
    @classmethod
    def validate_plan_sha256(cls, value: str) -> str:
        return _sha256_hex(value)


class CandidatePlanDisposition(IngestionPlanModel):
    kind: Literal["new", "identical", "conflict"]
    corpus: ExactCorpusReference
    plan_sha256: str

    @field_validator("plan_sha256")
    @classmethod
    def validate_plan_sha256(cls, value: str) -> str:
        return _sha256_hex(value)


class CandidateEmbeddingFunction(Protocol):
    def __call__(self, input: Sequence[str]) -> Sequence[Sequence[float]]: ...


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8", errors="strict")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()


def _embedding_material(embedding: Sequence[float]) -> list[str]:
    return [(0.0 if value == 0.0 else value).hex() for value in embedding]


def _embedding_sha256(embedding: Sequence[float]) -> str:
    return _digest(_embedding_material(embedding))


def _document_id(corpus: ExactCorpusReference, relative_path: str) -> str:
    return "doc_" + _digest(
        {
            "namespace": "cairn-document-v1",
            "corpus_id": corpus.corpus_id,
            "corpus_version": corpus.corpus_version,
            "relative_path": relative_path,
        }
    )


def _chunk_id(document_id: str, chunk_index: int, text_sha256: str) -> str:
    return "chk_" + _digest(
        {
            "namespace": "cairn-chunk-v1",
            "document_id": document_id,
            "chunk_index": chunk_index,
            "text_sha256": text_sha256,
        }
    )


def _provenance_material(provenance: PlannedProvenance) -> dict[str, object]:
    return {
        "title": provenance.title,
        "url": provenance.url,
        "owner": provenance.owner,
        "reviewed_at": provenance.reviewed_at.isoformat(),
        "source_sha256": provenance.source_sha256,
    }


def _chunk_material(chunk: PlannedChunk) -> dict[str, object]:
    return {
        "chunk_id": chunk.chunk_id,
        "document_id": chunk.document_id,
        "chunk_index": chunk.chunk_index,
        "text": chunk.text,
        "text_sha256": chunk.text_sha256,
        "embedding": _embedding_material(chunk.embedding),
        "embedding_sha256": chunk.embedding_sha256,
        "citation_title": chunk.citation_title,
        "citation_url": chunk.citation_url,
    }


def _document_material(document: PlannedDocument) -> dict[str, object]:
    return {
        "document_id": document.document_id,
        "relative_path": document.relative_path,
        "parser_id": document.parser_id,
        "normalization_id": document.normalization_id,
        "chunking_id": document.chunking_id,
        "normalized_text_sha256": document.normalized_text_sha256,
        "provenance": _provenance_material(document.provenance),
        "chunks": [_chunk_material(chunk) for chunk in document.chunks],
    }


def _document_plan_sha256(document: PlannedDocument) -> str:
    return _digest(_document_material(document))


def _plan_material(plan: CandidateIngestionPlan) -> dict[str, object]:
    return {
        "contract_version": plan.contract_version,
        "corpus": plan.corpus.model_dump(mode="json"),
        "embedding": plan.embedding.model_dump(mode="json"),
        "semantic_manifest_sha256": plan.semantic_manifest_sha256,
        "documents": [
            {
                "document_plan_sha256": document.document_plan_sha256,
                "chunk_ids": [chunk.chunk_id for chunk in document.chunks],
                "embedding_sha256s": [chunk.embedding_sha256 for chunk in document.chunks],
            }
            for document in plan.documents
        ],
    }


def _plan_sha256(plan: CandidateIngestionPlan) -> str:
    return _digest(_plan_material(plan))


def _safe_call[T](call: Callable[[], T], code: IngestionPlanErrorCode) -> T:
    result: T | object = _MISSING
    try:
        result = call()
    except Exception:
        result = _MISSING
    if result is _MISSING:
        raise IngestionPlanError(code) from None
    return cast(T, result)


def _strict_utf8_for_plan(value: str, code: IngestionPlanErrorCode) -> None:
    failed = False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        failed = True
    if failed:
        raise IngestionPlanError(code) from None


def _semantic_manifest_sha256(
    provenance: Mapping[str, SourceProvenance],
) -> str:
    documents = {
        path: {
            "title": source.title,
            "url": source.url,
            "sha256": source.sha256,
            "owner": source.owner,
            "reviewed_at": source.reviewed_at.isoformat(),
            "public": True,
        }
        for path, source in sorted(provenance.items(), key=lambda item: item[0].encode("utf-8"))
    }
    return _digest({"version": 1, "documents": documents})


def _normalized_embedding_batch(
    value: object, expected_count: int, dimensions: int
) -> tuple[tuple[float, ...], ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, str | bytes | bytearray)
        or len(value) != expected_count
    ):
        raise IngestionPlanError("malformed_embedding") from None
    normalized: list[tuple[float, ...]] = []
    for row in value:
        if (
            not isinstance(row, Sequence)
            or isinstance(row, str | bytes | bytearray)
            or len(row) != dimensions
            or any(type(scalar) is not float or not math.isfinite(scalar) for scalar in row)
        ):
            raise IngestionPlanError("malformed_embedding") from None
        normalized.append(tuple(0.0 if scalar == 0.0 else scalar for scalar in row))
    return tuple(normalized)


@dataclass(frozen=True)
class _PreparedDocument:
    document_id: str
    relative_path: str
    parser_id: str
    normalized_text_sha256: str
    provenance: PlannedProvenance
    chunks: tuple[str, ...]


def plan_candidate(
    *,
    corpus: ExactCorpusReference,
    source: CandidateSourceSnapshot,
    embedding: EmbeddingSpecification,
    embed: CandidateEmbeddingFunction,
) -> CandidateIngestionPlan:
    """Return one complete deterministic plan without performing any I/O."""
    if (
        not isinstance(corpus, ExactCorpusReference)
        or not isinstance(source, CandidateSourceSnapshot)
        or not isinstance(embedding, EmbeddingSpecification)
        or not callable(embed)
    ):
        raise IngestionPlanError("invalid_model") from None

    documents_by_path = {document.relative_path: document.content for document in source.documents}
    parsed_provenance: dict[str, SourceProvenance] = _safe_call(
        lambda: parse_provenance_manifest(source.manifest_bytes, documents_by_path),
        "invalid_manifest",
    )
    for path, manifest_provenance in parsed_provenance.items():
        _strict_utf8_for_plan(path, "invalid_manifest")
        for value in (
            manifest_provenance.title,
            manifest_provenance.url,
            manifest_provenance.owner,
            manifest_provenance.sha256,
            manifest_provenance.reviewed_at.isoformat(),
        ):
            _strict_utf8_for_plan(value, "invalid_manifest")
        if len(manifest_provenance.owner) > MAX_PROVENANCE_OWNER_CHARS:
            raise IngestionPlanError("bounds_exceeded") from None

    semantic_manifest_sha256 = _safe_call(
        lambda: _semantic_manifest_sha256(parsed_provenance), "invalid_manifest"
    )
    prepared_documents: list[_PreparedDocument] = []
    all_chunk_texts: list[str] = []
    total_normalized_chars = 0
    total_chunks = 0

    for document in sorted(source.documents, key=lambda item: item.relative_path.encode("utf-8")):
        path = document.relative_path
        _strict_utf8_for_plan(path, "invalid_document")
        suffix = "." + path.rsplit(".", 1)[1].lower() if "." in path else ""
        if suffix not in SUPPORTED_EXTENSIONS:
            raise IngestionPlanError("unsupported_document") from None
        extracted = _safe_call(
            partial(extract_text, path, document.content),
            "extraction_failed",
        )
        if not isinstance(extracted, str):
            raise IngestionPlanError("extraction_failed") from None
        normalized = unicodedata.normalize(
            "NFC", extracted.replace("\r\n", "\n").replace("\r", "\n")
        )
        _strict_utf8_for_plan(normalized, "invalid_document")
        if not normalized.strip():
            raise IngestionPlanError("invalid_document") from None
        if len(normalized) > MAX_NORMALIZED_TEXT_CHARS:
            raise IngestionPlanError("bounds_exceeded") from None
        total_normalized_chars += len(normalized)
        if total_normalized_chars > MAX_TOTAL_NORMALIZED_TEXT_CHARS:
            raise IngestionPlanError("bounds_exceeded") from None
        chunks = _safe_call(
            partial(chunk_text, normalized, chunk_size=800, overlap=100),
            "invalid_document",
        )
        if not isinstance(chunks, list) or not chunks:
            raise IngestionPlanError("invalid_document") from None
        if len(chunks) > MAX_CHUNKS_PER_DOCUMENT:
            raise IngestionPlanError("bounds_exceeded") from None
        for chunk in chunks:
            if not isinstance(chunk, str) or not 1 <= len(chunk) <= MAX_RETRIEVED_CHUNK_CHARS:
                raise IngestionPlanError("bounds_exceeded") from None
            _strict_utf8_for_plan(chunk, "invalid_document")
        total_chunks += len(chunks)
        if total_chunks > MAX_TOTAL_CHUNKS:
            raise IngestionPlanError("bounds_exceeded") from None
        document_id = _safe_call(partial(_document_id, corpus, path), "invalid_document")
        source_provenance = parsed_provenance[path]
        planned_provenance = PlannedProvenance(
            title=source_provenance.title,
            url=source_provenance.url,
            owner=source_provenance.owner,
            reviewed_at=source_provenance.reviewed_at,
            source_sha256=source_provenance.sha256,
        )
        prepared_documents.append(
            _PreparedDocument(
                document_id=document_id,
                relative_path=path,
                parser_id=PDF_PARSER_ID if suffix == ".pdf" else MARKDOWN_PARSER_ID,
                normalized_text_sha256=_text_sha256(normalized),
                provenance=planned_provenance,
                chunks=tuple(chunks),
            )
        )
        all_chunk_texts.extend(chunks)

    if total_chunks * embedding.dimensions > MAX_TOTAL_EMBEDDING_SCALARS:
        raise IngestionPlanError("bounds_exceeded") from None

    embeddings: list[tuple[float, ...]] = []
    for start in range(0, len(all_chunk_texts), EMBEDDING_BATCH_SIZE):
        batch = tuple(all_chunk_texts[start : start + EMBEDDING_BATCH_SIZE])
        raw_batch = _safe_call(partial(embed, batch), "embedding_failed")
        embeddings.extend(_normalized_embedding_batch(raw_batch, len(batch), embedding.dimensions))

    planned_documents: list[PlannedDocument] = []
    embedding_index = 0
    for prepared in prepared_documents:
        document_id = prepared.document_id
        provenance = prepared.provenance
        planned_chunks: list[PlannedChunk] = []
        for chunk_index, text in enumerate(prepared.chunks):
            vector = embeddings[embedding_index]
            embedding_index += 1
            text_sha256 = _text_sha256(text)
            planned_chunks.append(
                PlannedChunk(
                    chunk_id=_chunk_id(document_id, chunk_index, text_sha256),
                    document_id=document_id,
                    chunk_index=chunk_index,
                    text=text,
                    text_sha256=text_sha256,
                    embedding=vector,
                    embedding_sha256=_embedding_sha256(vector),
                    citation_title=provenance.title,
                    citation_url=provenance.url,
                )
            )
        temporary = PlannedDocument.model_construct(
            document_id=document_id,
            relative_path=prepared.relative_path,
            parser_id=prepared.parser_id,
            normalization_id=NORMALIZATION_ID,
            chunking_id=CHUNKING_ID,
            normalized_text_sha256=prepared.normalized_text_sha256,
            provenance=provenance,
            chunks=tuple(planned_chunks),
            document_plan_sha256="0" * _SHA256_LENGTH,
        )
        planned_documents.append(
            PlannedDocument(
                document_id=document_id,
                relative_path=prepared.relative_path,
                parser_id=prepared.parser_id,
                normalization_id=NORMALIZATION_ID,
                chunking_id=CHUNKING_ID,
                normalized_text_sha256=prepared.normalized_text_sha256,
                provenance=provenance,
                chunks=tuple(planned_chunks),
                document_plan_sha256=_document_plan_sha256(temporary),
            )
        )

    temporary_plan = CandidateIngestionPlan.model_construct(
        contract_version=INGESTION_PLAN_CONTRACT_VERSION,
        corpus=corpus,
        embedding=embedding,
        semantic_manifest_sha256=semantic_manifest_sha256,
        documents=tuple(planned_documents),
        document_count=len(planned_documents),
        chunk_count=total_chunks,
        plan_sha256="0" * _SHA256_LENGTH,
    )
    return CandidateIngestionPlan(
        contract_version=INGESTION_PLAN_CONTRACT_VERSION,
        corpus=corpus,
        embedding=embedding,
        semantic_manifest_sha256=semantic_manifest_sha256,
        documents=tuple(planned_documents),
        document_count=len(planned_documents),
        chunk_count=total_chunks,
        plan_sha256=_plan_sha256(temporary_plan),
    )


def classify_candidate_plan(
    plan: CandidateIngestionPlan,
    existing: ExistingCandidateDescriptor | None,
) -> CandidatePlanDisposition:
    """Compare a complete plan with an optional exact-candidate descriptor."""
    if not isinstance(plan, CandidateIngestionPlan) or (
        existing is not None and not isinstance(existing, ExistingCandidateDescriptor)
    ):
        raise IngestionPlanError("invalid_model") from None
    if existing is None:
        kind: Literal["new", "identical", "conflict"] = "new"
    elif existing.corpus != plan.corpus:
        raise IngestionPlanError("candidate_conflict") from None
    elif existing.plan_sha256 == plan.plan_sha256:
        kind = "identical"
    else:
        kind = "conflict"
    return CandidatePlanDisposition(kind=kind, corpus=plan.corpus, plan_sha256=plan.plan_sha256)
