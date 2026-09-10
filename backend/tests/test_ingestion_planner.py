import builtins
import hashlib
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import warnings
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import TypeAdapter, ValidationError
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.ingest import planner as planner_module
from app.ingest.planner import (
    CHUNKING_ID,
    EMBEDDING_BATCH_SIZE,
    INGESTION_PLAN_CONTRACT_VERSION,
    MARKDOWN_PARSER_ID,
    MAX_DOCUMENT_BYTES,
    MAX_DOCUMENTS,
    MAX_EMBEDDING_DIMENSIONS,
    MAX_EMBEDDING_IDENTITY_CHARS,
    MAX_PROVENANCE_OWNER_CHARS,
    MAX_TOTAL_DOCUMENT_BYTES,
    NORMALIZATION_ID,
    PDF_PARSER_ID,
    CandidateDocumentSnapshot,
    CandidateIngestionPlan,
    CandidateSourceSnapshot,
    EmbeddingSpecification,
    ExistingCandidateDescriptor,
    IngestionPlanError,
    IngestionPlanModel,
    PlannedChunk,
    PlannedDocument,
    PlannedProvenance,
    classify_candidate_plan,
    plan_candidate,
)
from app.retrieval_contracts import ExactCorpusReference

_GUARD_MESSAGE = "I/O forbidden in ingestion planner tests"


@pytest.fixture(autouse=True)
def no_ingestion_planner_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail immediately if planner code reaches any external or persistent boundary."""

    def forbidden(*_: object, **__: object) -> Any:
        raise AssertionError(_GUARD_MESSAGE)

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "iterdir", forbidden)
    monkeypatch.setattr(Path, "rglob", forbidden)
    monkeypatch.setattr(os, "walk", forbidden)
    monkeypatch.setattr(os, "getenv", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr("app.providers.gemini.GeminiProvider.stream", forbidden)
    monkeypatch.setattr("app.providers.ollama.OllamaProvider.stream", forbidden)
    monkeypatch.setattr("app.vectorstore.VectorStoreClient.__init__", forbidden)


def _pdf_bytes(text: str) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 10 100 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    font = DictionaryObject()
    font[NameObject("/Type")] = NameObject("/Font")
    font[NameObject("/Subtype")] = NameObject("/Type1")
    font[NameObject("/BaseFont")] = NameObject("/Helvetica")
    resources = DictionaryObject()
    fonts = DictionaryObject()
    fonts[NameObject("/F1")] = writer._add_object(font)
    resources[NameObject("/Font")] = fonts
    page[NameObject("/Resources")] = resources
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _manifest(documents: dict[str, bytes], **entry_changes: object) -> bytes:
    entries = {
        path: {
            "title": f"Title for {path}",
            "url": f"https://docs.example.com/{path}",
            "sha256": hashlib.sha256(content).hexdigest(),
            "owner": "Documentation team",
            "reviewed_at": "2026-09-08",
            "public": True,
            **entry_changes,
        }
        for path, content in documents.items()
    }
    return json.dumps({"version": 1, "documents": entries}).encode()


def _source(documents: dict[str, bytes]) -> CandidateSourceSnapshot:
    return CandidateSourceSnapshot(
        manifest_bytes=_manifest(documents),
        documents=tuple(
            CandidateDocumentSnapshot(relative_path=path, content=content)
            for path, content in documents.items()
        ),
    )


def _corpus(version: str = "2026.09.08") -> ExactCorpusReference:
    return ExactCorpusReference(corpus_id="example-support", corpus_version=version)


def _embedding(input: Sequence[str]) -> Sequence[Sequence[float]]:
    return tuple((float(index), float(len(text))) for index, text in enumerate(input))


def _record_rejected_embedding(
    calls: list[tuple[str, ...]], input: Sequence[str]
) -> Sequence[Sequence[float]]:
    calls.append(tuple(input))
    return ()


def _replace_model(sample: IngestionPlanModel, **update: object) -> object:
    replace = cast(Callable[..., object], cast(Any, sample).__replace__)
    return replace(**update)


def _construct_model(
    model: type[IngestionPlanModel],
    values: dict[str, object],
    *,
    deprecated: bool = False,
    fields_set: set[str] | None = None,
    extra: dict[str, object] | None = None,
) -> IngestionPlanModel:
    arguments = values | (extra or {})
    constructor = cast(
        Callable[..., IngestionPlanModel],
        model.construct if deprecated else model.model_construct,
    )
    return constructor(_fields_set=fields_set, **arguments)


def _plan(
    documents: dict[str, bytes] | None = None,
    *,
    version: str = "2026.09.08",
) -> CandidateIngestionPlan:
    return plan_candidate(
        corpus=_corpus(version),
        source=_source(documents or {"guide.md": b"Reviewed content"}),
        embedding=EmbeddingSpecification(identity="fixture-embedding-v1", dimensions=2),
        embed=_embedding,
    )


def test_planner_is_deterministic_ordered_and_candidate_isolated() -> None:
    documents = {
        "z.md": b"Line one\r\n\r\nCafe\xcc\x81  \xf0\x9f\x8c\x8d",
        "a.markdown": b"Alpha paragraph.\rSecond paragraph.",
    }
    first = _plan(documents)
    reordered = _plan(dict(reversed(tuple(documents.items()))))
    newer = _plan(documents, version="2026.09.09")

    assert first == reordered == _plan(documents)
    assert [document.relative_path for document in first.documents] == ["a.markdown", "z.md"]
    assert first.contract_version == INGESTION_PLAN_CONTRACT_VERSION
    assert all(document.normalization_id == NORMALIZATION_ID for document in first.documents)
    assert all(document.chunking_id == CHUNKING_ID for document in first.documents)
    assert all(document.parser_id == MARKDOWN_PARSER_ID for document in first.documents)
    assert first.documents[1].chunks[0].text == "Line one\n\nCaf\u00e9  \U0001f30d"
    assert first.plan_sha256 != newer.plan_sha256
    assert first.documents[0].document_id != newer.documents[0].document_id
    assert first.documents[0].chunks[0].chunk_id != newer.documents[0].chunks[0].chunk_id
    assert len(first.plan_sha256) == 64
    assert {
        "semantic": _plan().semantic_manifest_sha256,
        "document": _plan().documents[0].document_id,
        "chunk": _plan().documents[0].chunks[0].chunk_id,
        "document_plan": _plan().documents[0].document_plan_sha256,
        "plan": _plan().plan_sha256,
    } == {
        "semantic": "212c350d5d9ce486de2262864e7b4061334a69e102918eafca023e766101a234",
        "document": "doc_c571ca8283c3a3b51ea1ecaed9c79bfc1f36b784e7f3d63bada0fc541433b3de",
        "chunk": "chk_f446dcc6760e0da6751f674d396fa1d5aff78c1578fd0c37ca756bce4af3f602",
        "document_plan": "736eaab5f4a248488aaa1147a4d6efaf9b81aebc90eea97f2bc03d84ee802ae1",
        "plan": "8796b9754d16c80feccc9b21fbef926900483d92a2d01efd439b132657685a25",
    }


def test_provenance_and_disposition_are_exact() -> None:
    plan = _plan()
    document = plan.documents[0]
    chunk = document.chunks[0]

    assert document.provenance.title == "Title for guide.md"
    assert document.provenance.url == "https://docs.example.com/guide.md"
    assert document.provenance.owner == "Documentation team"
    assert document.provenance.reviewed_at == date(2026, 9, 8)
    assert chunk.citation_title == document.provenance.title
    assert chunk.citation_url == document.provenance.url
    assert classify_candidate_plan(plan, None).kind == "new"
    existing = ExistingCandidateDescriptor(
        contract_version="1.0", corpus=plan.corpus, plan_sha256=plan.plan_sha256
    )
    assert classify_candidate_plan(plan, existing).kind == "identical"
    conflict = existing.model_copy(update={"plan_sha256": "0" * 64})
    assert classify_candidate_plan(plan, conflict).kind == "conflict"
    wrong = existing.model_copy(update={"corpus": _corpus("2026.09.09")})
    with pytest.raises(IngestionPlanError) as caught:
        classify_candidate_plan(plan, wrong)
    assert caught.value.code == "candidate_conflict"


def test_all_input_is_validated_before_embedding_and_failures_are_content_free() -> None:
    calls: list[tuple[str, ...]] = []

    def embed(input: Sequence[str]) -> Sequence[Sequence[float]]:
        calls.append(tuple(input))
        return tuple((1.0, 2.0) for _ in input)

    source = CandidateSourceSnapshot(
        manifest_bytes=_manifest({"guide.md": b"different"}),
        documents=(CandidateDocumentSnapshot(relative_path="guide.md", content=b"content"),),
    )
    with pytest.raises(IngestionPlanError) as caught:
        plan_candidate(
            corpus=_corpus(),
            source=source,
            embedding=EmbeddingSpecification(identity="fixture", dimensions=2),
            embed=embed,
        )
    assert caught.value.code == "invalid_manifest"
    assert calls == []
    rendered = (
        str(caught.value)
        + repr(caught.value)
        + repr(caught.value.errors(include_input=True))
        + caught.value.json(include_input=True)
        + repr(caught.value.__cause__)
        + repr(caught.value.__context__)
    )
    assert "different" not in rendered
    assert "guide.md" not in rendered


def test_model_validation_is_content_free_across_pydantic_entrypoints() -> None:
    secret = "model-secret-sentinel"
    model = CandidateDocumentSnapshot
    calls: tuple[Callable[[], object], ...] = (
        lambda: cast(Callable[..., CandidateDocumentSnapshot], model)(
            relative_path="guide.md", content=b"ok", message=secret
        ),
        lambda: model.model_validate(
            {"relative_path": "guide.md", "content": b"ok", "message": secret}
        ),
        lambda: model.model_validate_json(
            json.dumps({"relative_path": "guide.md", "content": "ok", "message": secret})
        ),
        lambda: model.model_validate_strings(
            {"relative_path": "guide.md", "content": "ok", "message": secret}
        ),
        lambda: TypeAdapter(model).validate_python(
            {"relative_path": "guide.md", "content": b"ok", "message": secret}
        ),
        lambda: TypeAdapter(model).validate_json(
            json.dumps({"relative_path": "guide.md", "content": "ok", "message": secret})
        ),
        lambda: TypeAdapter(model).validate_strings(
            {"relative_path": "guide.md", "content": "ok", "message": secret}
        ),
        lambda: model.model_validate(
            {"relative_path": "guide.md", "content": b"ok", "message": secret}, extra="allow"
        ),
        lambda: model.model_validate(
            {"relative_path": "guide.md", "content": b"ok", "message": secret}, extra="ignore"
        ),
    )
    for call in calls:
        with pytest.raises(IngestionPlanError) as caught:
            call()
        error = caught.value
        rendered = (
            str(error)
            + repr(error)
            + repr(error.errors(include_input=True))
            + error.json(include_input=True)
        )
        assert secret not in rendered
        assert error.code == "invalid_model"
        assert error.__cause__ is None
        assert error.__context__ is None


def test_models_recompute_counts_ids_and_digests_and_are_frozen() -> None:
    plan = _plan()
    with pytest.raises(ValidationError):
        plan.document_count = 2
    values = plan.model_dump()
    values["chunk_count"] += 1
    with pytest.raises(IngestionPlanError):
        CandidateIngestionPlan.model_validate(values)
    chunk_values = plan.documents[0].chunks[0].model_dump()
    chunk_values["text_sha256"] = "0" * 64
    with pytest.raises(IngestionPlanError):
        PlannedChunk.model_validate(chunk_values)


def test_manifest_format_and_key_order_do_not_change_semantics() -> None:
    documents = {"b.md": b"Bravo", "a.md": b"Alpha"}
    source = _source(documents)
    parsed = json.loads(source.manifest_bytes)
    formatted = json.dumps(parsed, indent=4, sort_keys=True).encode()
    reordered_entries = dict(reversed(tuple(parsed["documents"].items())))
    reordered = json.dumps({"documents": reordered_entries, "version": 1}).encode()

    plans = tuple(
        plan_candidate(
            corpus=_corpus(),
            source=source.model_copy(update={"manifest_bytes": manifest}),
            embedding=EmbeddingSpecification(identity="fixture-embedding-v1", dimensions=2),
            embed=_embedding,
        )
        for manifest in (source.manifest_bytes, formatted, reordered)
    )
    assert plans[0] == plans[1] == plans[2]
    assert len({plan.semantic_manifest_sha256 for plan in plans}) == 1


@pytest.mark.parametrize("field", ("title", "url", "owner", "reviewed_at"))
def test_semantic_provenance_changes_are_preserved_and_change_plan(field: str) -> None:
    content = b"Reviewed content"
    source = _source({"guide.md": content})
    changed_value: object = {
        "title": "Replacement title",
        "url": "https://docs.example.com/replacement",
        "owner": "Replacement owner",
        "reviewed_at": "2026-09-09",
    }[field]
    changed_manifest = _manifest({"guide.md": content}, **{field: changed_value})
    original = _plan()
    changed = plan_candidate(
        corpus=_corpus(),
        source=source.model_copy(update={"manifest_bytes": changed_manifest}),
        embedding=EmbeddingSpecification(identity="fixture-embedding-v1", dimensions=2),
        embed=_embedding,
    )

    assert changed.plan_sha256 != original.plan_sha256
    assert changed.semantic_manifest_sha256 != original.semantic_manifest_sha256
    assert changed.documents[0].provenance.source_sha256 == (
        original.documents[0].provenance.source_sha256
    )
    expected_value = (
        date.fromisoformat(cast(str, changed_value))
        if field == "reviewed_at"
        else changed_value
    )
    assert getattr(changed.documents[0].provenance, field) == expected_value


def test_source_byte_change_updates_content_digests_and_stale_hash_fails_first() -> None:
    original = _plan({"guide.md": b"Reviewed content"})
    updated = _plan({"guide.md": b"Reviewed contenU"})
    assert updated.documents[0].normalized_text_sha256 != (
        original.documents[0].normalized_text_sha256
    )
    assert updated.documents[0].chunks[0].text_sha256 != (
        original.documents[0].chunks[0].text_sha256
    )
    assert updated.plan_sha256 != original.plan_sha256

    calls: list[tuple[str, ...]] = []
    stale_source = CandidateSourceSnapshot(
        manifest_bytes=_manifest({"guide.md": b"Reviewed content"}),
        documents=(CandidateDocumentSnapshot(relative_path="guide.md", content=b"changed"),),
    )
    with pytest.raises(IngestionPlanError, match="manifest") as caught:
        plan_candidate(
            corpus=_corpus(),
            source=stale_source,
            embedding=EmbeddingSpecification(identity="fixture", dimensions=2),
            embed=lambda input: _record_rejected_embedding(calls, input),
        )
    assert caught.value.code == "invalid_manifest"
    assert calls == []


def test_markdown_chunk_boundaries_overlap_and_golden_digests() -> None:
    text = "  Cafe\u0301  \U0001f30d  " + "A" * 600 + "\r\n\r\n" + "B" * 300 + "  "
    plan = _plan({"guide.md": text.encode()})
    document = plan.documents[0]
    assert [chunk.text for chunk in document.chunks] == [
        "Caf\u00e9  \U0001f30d  " + "A" * 600,
        "A" * 99 + "\n\n" + "B" * 300,
    ]
    assert [chunk.text_sha256 for chunk in document.chunks] == [
        "f90376db39bbd3bcde840222d57ec5ea298bd35504341d8ce0035ba80077a2fe",
        "ea94237bd31a271fbf1d53f3e8880a2bb30a76121e478fcd01e3f22390b0de11",
    ]


def test_pdf_parser_identity_and_golden_plan() -> None:
    plan = _plan({"manual.pdf": _pdf_bytes("Hello deterministic PDF")})
    document = plan.documents[0]
    assert document.parser_id == PDF_PARSER_ID
    assert document.chunks[0].text == "Hello deterministic PDF"
    assert document.normalized_text_sha256 == (
        "784eac272005db1f8c70a3157ebdf736517888dc017428d8126302aec703a3e7"
    )
    assert plan.plan_sha256 == ("3aea211e6ea24c5bc610712661a2de991bc3e1ef48847710b9340f8d86f6a5ed")


def test_ids_are_stable_unique_and_candidate_isolated() -> None:
    repeated = ("same paragraph. " * 100).encode()
    documents = {"one/repeat.md": repeated, "two/repeat.md": repeated}
    first = _plan(documents)
    retry = _plan(documents)
    newer = _plan(documents, version="2026.09.09")

    assert first == retry
    assert len({document.document_id for document in first.documents}) == 2
    chunk_ids = [chunk.chunk_id for doc in first.documents for chunk in doc.chunks]
    assert len(chunk_ids) == len(set(chunk_ids))
    assert [doc.document_id for doc in first.documents] != [
        doc.document_id for doc in newer.documents
    ]
    assert chunk_ids != [chunk.chunk_id for doc in newer.documents for chunk in doc.chunks]


def test_embedding_batches_are_exact_immutable_and_positional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    texts = tuple(f"chunk-{index}" for index in range(130))
    monkeypatch.setattr(planner_module, "chunk_text", lambda *_args, **_kwargs: list(texts))
    batches: list[tuple[str, ...]] = []
    position = 0

    def embed(input: Sequence[str]) -> Sequence[Sequence[float]]:
        nonlocal position
        assert isinstance(input, tuple)
        batches.append(tuple(input))
        result = tuple((float(index),) for index in range(position, position + len(input)))
        position += len(input)
        return result

    plan = plan_candidate(
        corpus=_corpus(),
        source=_source({"guide.md": b"content"}),
        embedding=EmbeddingSpecification(identity="fixture", dimensions=1),
        embed=embed,
    )
    assert [len(batch) for batch in batches] == [64, 64, 2]
    assert tuple(text for batch in batches for text in batch) == texts
    assert [chunk.embedding for chunk in plan.documents[0].chunks] == [
        (float(index),) for index in range(130)
    ]


@pytest.mark.parametrize(
    "result",
    (
        (),
        ((1.0,), (2.0,)),
        ((1.0,),),
        ((1.0, 2.0, 3.0),),
        (1.0,),
        ((True, 2.0),),
        ((1, 2.0),),
        (("1", 2.0),),
        ((float("nan"), 2.0),),
        ((float("inf"), 2.0),),
    ),
)
def test_malformed_embedding_results_fail_closed(result: object) -> None:
    def malformed(input: Sequence[str]) -> Sequence[Sequence[float]]:
        del input
        return cast(Sequence[Sequence[float]], result)

    with pytest.raises(IngestionPlanError) as caught:
        plan_candidate(
            corpus=_corpus(),
            source=_source({"guide.md": b"content"}),
            embedding=EmbeddingSpecification(identity="fixture", dimensions=2),
            embed=malformed,
        )
    assert caught.value.code == "malformed_embedding"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_embedding_exception_and_later_batch_failure_have_no_partial_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "raw-embedding-secret"

    def failed(input: Sequence[str]) -> Sequence[Sequence[float]]:
        del input
        raise RuntimeError(secret)

    with pytest.raises(IngestionPlanError) as first:
        plan_candidate(
            corpus=_corpus(),
            source=_source({"guide.md": b"content"}),
            embedding=EmbeddingSpecification(identity="fixture", dimensions=2),
            embed=failed,
        )
    assert first.value.code == "embedding_failed"
    assert secret not in repr(first.value)
    assert first.value.__cause__ is None
    assert first.value.__context__ is None

    monkeypatch.setattr(planner_module, "chunk_text", lambda *_a, **_k: ["x"] * 65)
    calls = 0

    def fails_second(input: Sequence[str]) -> Sequence[Sequence[float]]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError(secret)
        return tuple((1.0, 2.0) for _ in input)

    with pytest.raises(IngestionPlanError) as second:
        plan_candidate(
            corpus=_corpus(),
            source=_source({"guide.md": b"content"}),
            embedding=EmbeddingSpecification(identity="fixture", dimensions=2),
            embed=fails_second,
        )
    assert second.value.code == "embedding_failed"
    assert calls == 2


def test_negative_zero_normalizes_without_changing_other_floats() -> None:
    plan = plan_candidate(
        corpus=_corpus(),
        source=_source({"guide.md": b"content"}),
        embedding=EmbeddingSpecification(identity="fixture", dimensions=2),
        embed=lambda _input: ((-0.0, 0.125),),
    )
    vector = plan.documents[0].chunks[0].embedding
    assert vector == (0.0, 0.125)
    assert str(vector[0].hex()) == "0x0.0p+0"
    positive = plan_candidate(
        corpus=_corpus(),
        source=_source({"guide.md": b"content"}),
        embedding=EmbeddingSpecification(identity="fixture", dimensions=2),
        embed=lambda _input: ((0.0, 0.125),),
    )
    assert plan == positive


@pytest.mark.parametrize(
    ("source", "code"),
    (
        (
            lambda: CandidateSourceSnapshot(
                manifest_bytes=_manifest({"guide.txt": b"content"}),
                documents=(
                    CandidateDocumentSnapshot(relative_path="guide.txt", content=b"content"),
                ),
            ),
            "unsupported_document",
        ),
        (
            lambda: _source({"guide.md": b"   \r\n\t"}),
            "invalid_document",
        ),
        (
            lambda: CandidateSourceSnapshot(
                manifest_bytes=b"{",
                documents=(
                    CandidateDocumentSnapshot(relative_path="guide.md", content=b"content"),
                ),
            ),
            "invalid_manifest",
        ),
    ),
)
def test_invalid_candidates_use_fixed_errors(
    source: Callable[[], CandidateSourceSnapshot], code: str
) -> None:
    with pytest.raises(IngestionPlanError) as caught:
        plan_candidate(
            corpus=_corpus(),
            source=source(),
            embedding=EmbeddingSpecification(identity="fixture", dimensions=2),
            embed=_embedding,
        )
    assert caught.value.code == code


def test_snapshot_and_embedding_model_bounds() -> None:
    assert (
        len(CandidateDocumentSnapshot(relative_path="p" * 4096, content=b"x").relative_path) == 4096
    )
    assert (
        len(
            CandidateDocumentSnapshot(
                relative_path="guide.md", content=b"x" * MAX_DOCUMENT_BYTES
            ).content
        )
        == MAX_DOCUMENT_BYTES
    )
    with pytest.raises(IngestionPlanError):
        CandidateDocumentSnapshot(relative_path="p" * 4097, content=b"x")
    with pytest.raises(IngestionPlanError):
        CandidateDocumentSnapshot(relative_path="guide.md", content=b"x" * (MAX_DOCUMENT_BYTES + 1))
    with pytest.raises(IngestionPlanError):
        CandidateDocumentSnapshot(relative_path="guide.md", content=b"")

    documents = tuple(
        CandidateDocumentSnapshot(relative_path=f"{index}.md", content=b"x")
        for index in range(MAX_DOCUMENTS)
    )
    manifest = _manifest({"guide.md": b"x"})
    assert (
        len(CandidateSourceSnapshot(manifest_bytes=manifest, documents=documents).documents)
        == MAX_DOCUMENTS
    )
    with pytest.raises(IngestionPlanError):
        CandidateSourceSnapshot(
            manifest_bytes=manifest,
            documents=documents
            + (CandidateDocumentSnapshot(relative_path="overflow.md", content=b"x"),),
        )

    large_content = b"x" * MAX_DOCUMENT_BYTES
    exact_large_documents = tuple(
        CandidateDocumentSnapshot.model_construct(
            relative_path=f"large-{index}.md", content=large_content
        )
        for index in range(8)
    )
    assert (
        len(CandidateSourceSnapshot(manifest_bytes=b"", documents=exact_large_documents).documents)
        == 8
    )
    large_documents = tuple(
        CandidateDocumentSnapshot.model_construct(
            relative_path=f"large-{index}.md", content=large_content
        )
        for index in range(9)
    )
    with pytest.raises(IngestionPlanError):
        CandidateSourceSnapshot(manifest_bytes=b"", documents=large_documents)
    assert 8 * MAX_DOCUMENT_BYTES == MAX_TOTAL_DOCUMENT_BYTES

    assert (
        len(
            CandidateSourceSnapshot(
                manifest_bytes=b"x" * 1_048_576,
                documents=(CandidateDocumentSnapshot(relative_path="guide.md", content=b"x"),),
            ).manifest_bytes
        )
        == 1_048_576
    )
    with pytest.raises(IngestionPlanError):
        CandidateSourceSnapshot(
            manifest_bytes=b"x" * 1_048_577,
            documents=(CandidateDocumentSnapshot(relative_path="guide.md", content=b"x"),),
        )

    assert (
        EmbeddingSpecification(
            identity="x" * MAX_EMBEDDING_IDENTITY_CHARS,
            dimensions=MAX_EMBEDDING_DIMENSIONS,
        ).dimensions
        == MAX_EMBEDDING_DIMENSIONS
    )
    for values in (
        {"identity": " x", "dimensions": 1},
        {"identity": "x\n", "dimensions": 1},
        {"identity": "x" * (MAX_EMBEDDING_IDENTITY_CHARS + 1), "dimensions": 1},
        {"identity": "x", "dimensions": 0},
        {"identity": "x", "dimensions": MAX_EMBEDDING_DIMENSIONS + 1},
        {"identity": "x", "dimensions": True},
    ):
        with pytest.raises(IngestionPlanError):
            EmbeddingSpecification.model_validate(values)


def test_owner_limit_chunk_and_scalar_bounds_fail_before_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    source = CandidateSourceSnapshot(
        manifest_bytes=_manifest({"guide.md": b"content"}, owner="o" * 513),
        documents=(CandidateDocumentSnapshot(relative_path="guide.md", content=b"content"),),
    )
    with pytest.raises(IngestionPlanError) as owner:
        plan_candidate(
            corpus=_corpus(),
            source=source,
            embedding=EmbeddingSpecification(identity="fixture", dimensions=1),
            embed=lambda input: _record_rejected_embedding(calls, input),
        )
    assert owner.value.code == "bounds_exceeded"
    assert calls == []
    assert MAX_PROVENANCE_OWNER_CHARS == 512

    monkeypatch.setattr(planner_module, "chunk_text", lambda *_a, **_k: ["x"] * 16_385)
    with pytest.raises(IngestionPlanError) as chunks:
        plan_candidate(
            corpus=_corpus(),
            source=_source({"guide.md": b"content"}),
            embedding=EmbeddingSpecification(identity="fixture", dimensions=1),
            embed=lambda input: _record_rejected_embedding(calls, input),
        )
    assert chunks.value.code == "bounds_exceeded"
    assert calls == []

    monkeypatch.setattr(planner_module, "chunk_text", lambda *_a, **_k: ["x"] * 2_049)
    with pytest.raises(IngestionPlanError) as scalars:
        plan_candidate(
            corpus=_corpus(),
            source=_source({"guide.md": b"content"}),
            embedding=EmbeddingSpecification(identity="fixture", dimensions=4_096),
            embed=lambda input: _record_rejected_embedding(calls, input),
        )
    assert scalars.value.code == "bounds_exceeded"
    assert calls == []


def test_planner_exact_text_chunk_count_and_scalar_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exact_text = "N" * 8_388_608
    monkeypatch.setattr(planner_module, "extract_text", lambda *_a, **_k: exact_text)
    monkeypatch.setattr(planner_module, "chunk_text", lambda *_a, **_k: ["x"])
    accepted = plan_candidate(
        corpus=_corpus(),
        source=_source({"guide.md": b"source"}),
        embedding=EmbeddingSpecification(identity="fixture", dimensions=1),
        embed=lambda _input: ((1.0,),),
    )
    assert accepted.document_count == 1

    monkeypatch.setattr(planner_module, "extract_text", lambda *_a, **_k: exact_text + "x")
    with pytest.raises(IngestionPlanError) as text_over:
        plan_candidate(
            corpus=_corpus(),
            source=_source({"guide.md": b"source"}),
            embedding=EmbeddingSpecification(identity="fixture", dimensions=1),
            embed=lambda _input: ((1.0,),),
        )
    assert text_over.value.code == "bounds_exceeded"

    monkeypatch.setattr(planner_module, "extract_text", lambda *_a, **_k: exact_text)
    aggregate_documents = {f"{index}.md": b"x" for index in range(8)}
    aggregate = plan_candidate(
        corpus=_corpus(),
        source=_source(aggregate_documents),
        embedding=EmbeddingSpecification(identity="fixture", dimensions=1),
        embed=lambda input: tuple((1.0,) for _ in input),
    )
    assert aggregate.document_count == 8
    over_documents = aggregate_documents | {"8.md": b"x"}
    with pytest.raises(IngestionPlanError) as total_text_over:
        plan_candidate(
            corpus=_corpus(),
            source=_source(over_documents),
            embedding=EmbeddingSpecification(identity="fixture", dimensions=1),
            embed=lambda input: tuple((1.0,) for _ in input),
        )
    assert total_text_over.value.code == "bounds_exceeded"

    monkeypatch.setattr(planner_module, "extract_text", lambda *_a, **_k: "source")
    monkeypatch.setattr(planner_module, "chunk_text", lambda *_a, **_k: ["x" * 3_000])
    assert _plan({"guide.md": b"source"}).documents[0].chunks[0].text == "x" * 3_000
    monkeypatch.setattr(planner_module, "chunk_text", lambda *_a, **_k: ["x" * 3_001])
    with pytest.raises(IngestionPlanError) as chunk_text_over:
        _plan({"guide.md": b"source"})
    assert chunk_text_over.value.code == "bounds_exceeded"

    calls: list[int] = []

    def stop_at_embedding(input: Sequence[str]) -> Sequence[Sequence[float]]:
        calls.append(len(input))
        raise RuntimeError("expected stop")

    monkeypatch.setattr(planner_module, "chunk_text", lambda *_a, **_k: ["x"] * 16_384)
    with pytest.raises(IngestionPlanError) as exact_chunks:
        plan_candidate(
            corpus=_corpus(),
            source=_source({"guide.md": b"source"}),
            embedding=EmbeddingSpecification(identity="fixture", dimensions=1),
            embed=stop_at_embedding,
        )
    assert exact_chunks.value.code == "embedding_failed"
    assert calls == [EMBEDDING_BATCH_SIZE]
    calls.clear()
    monkeypatch.setattr(planner_module, "chunk_text", lambda *_a, **_k: ["x"] * 16_385)
    with pytest.raises(IngestionPlanError) as over_chunks:
        plan_candidate(
            corpus=_corpus(),
            source=_source({"guide.md": b"source"}),
            embedding=EmbeddingSpecification(identity="fixture", dimensions=1),
            embed=stop_at_embedding,
        )
    assert over_chunks.value.code == "bounds_exceeded"
    assert calls == []

    monkeypatch.setattr(planner_module, "chunk_text", lambda *_a, **_k: ["x"] * 2_048)
    with pytest.raises(IngestionPlanError) as exact_scalars:
        plan_candidate(
            corpus=_corpus(),
            source=_source({"guide.md": b"source"}),
            embedding=EmbeddingSpecification(identity="fixture", dimensions=4_096),
            embed=stop_at_embedding,
        )
    assert exact_scalars.value.code == "embedding_failed"
    assert calls == [EMBEDDING_BATCH_SIZE]

    calls.clear()
    monkeypatch.setattr(planner_module, "extract_text", lambda path, _content: path)
    chunk_counts = {f"{index}.md": 16_384 for index in range(4)}
    monkeypatch.setattr(
        planner_module,
        "chunk_text",
        lambda text, **_kwargs: ["x"] * chunk_counts[text],
    )
    four_documents = {path: b"x" for path in chunk_counts}
    with pytest.raises(IngestionPlanError) as exact_total_chunks:
        plan_candidate(
            corpus=_corpus(),
            source=_source(four_documents),
            embedding=EmbeddingSpecification(identity="fixture", dimensions=1),
            embed=stop_at_embedding,
        )
    assert exact_total_chunks.value.code == "embedding_failed"
    assert calls == [EMBEDDING_BATCH_SIZE]

    calls.clear()
    chunk_counts["4.md"] = 1
    with pytest.raises(IngestionPlanError) as over_total_chunks:
        plan_candidate(
            corpus=_corpus(),
            source=_source(four_documents | {"4.md": b"x"}),
            embedding=EmbeddingSpecification(identity="fixture", dimensions=1),
            embed=stop_at_embedding,
        )
    assert over_total_chunks.value.code == "bounds_exceeded"
    assert calls == []


def _all_m7_model_samples(plan: CandidateIngestionPlan) -> tuple[IngestionPlanModel, ...]:
    return (
        CandidateDocumentSnapshot(relative_path="guide.md", content=b"content"),
        _source({"guide.md": b"content"}),
        plan.embedding,
        plan.documents[0].provenance,
        plan.documents[0].chunks[0],
        plan.documents[0],
        plan,
        ExistingCandidateDescriptor(
            contract_version="1.0", corpus=plan.corpus, plan_sha256=plan.plan_sha256
        ),
        classify_candidate_plan(plan, None),
    )


def _assert_invalid_model_is_content_free(operation: Callable[[], object], *secrets: str) -> None:
    with pytest.raises(IngestionPlanError) as caught:
        operation()
    error = caught.value
    rendered = (
        str(error)
        + repr(error)
        + repr(error.args)
        + repr(error.__dict__)
        + repr(error.errors(include_input=True, include_context=True))
        + error.json(include_input=True, include_context=True)
        + repr(error.__cause__)
        + repr(error.__context__)
    )
    assert error.code == "invalid_model"
    assert error.__cause__ is None
    assert error.__context__ is None
    for secret in secrets:
        assert secret not in rendered


@pytest.mark.parametrize(
    "surface", ["model_construct", "construct", "model_copy", "copy", "replace"]
)
def test_all_m7_models_seal_public_copy_and_construct_bypasses(surface: str) -> None:
    secret = f"{surface}-content-secret-sentinel"
    for sample in _all_m7_model_samples(_plan()):
        model = type(sample)
        values = sample.model_dump(round_trip=True)

        def operation(
            model: type[IngestionPlanModel] = model,
            values: dict[str, object] = values,
            sample: IngestionPlanModel = sample,
        ) -> object:
            if surface == "model_construct":
                return _construct_model(model, values, extra={"unknown": secret})
            if surface == "construct":
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    return _construct_model(
                        model, values, deprecated=True, extra={"unknown": secret}
                    )
            if surface == "model_copy":
                return sample.model_copy(update={"unknown": secret})
            if surface == "replace":
                return _replace_model(sample, unknown=secret)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                return sample.copy(update={"unknown": secret})

        _assert_invalid_model_is_content_free(operation, secret, "unknown")


@pytest.mark.parametrize("surface", ["model_construct", "construct"])
def test_all_m7_models_reject_missing_fields_and_fields_set_content(surface: str) -> None:
    secret = f"{surface}-fields-set-content-secret"
    for sample in _all_m7_model_samples(_plan()):
        model = type(sample)
        values = sample.model_dump(round_trip=True)
        missing = next(iter(model.model_fields))
        values.pop(missing)

        def operation(
            model: type[IngestionPlanModel] = model,
            values: dict[str, object] = values,
        ) -> object:
            if surface == "model_construct":
                return _construct_model(model, values)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                return _construct_model(model, values, deprecated=True)

        _assert_invalid_model_is_content_free(operation, secret)

        complete_values = sample.model_dump(round_trip=True)

        def fields_set_operation(
            model: type[IngestionPlanModel] = model,
            complete_values: dict[str, object] = complete_values,
        ) -> object:
            if surface == "model_construct":
                return _construct_model(model, complete_values, fields_set={secret})
            else:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    return _construct_model(
                        model,
                        complete_values,
                        deprecated=True,
                        fields_set={secret},
                    )

        _assert_invalid_model_is_content_free(fields_set_operation, secret)


def test_all_m7_models_preserve_valid_copy_construct_and_adapter_paths() -> None:
    for sample in _all_m7_model_samples(_plan()):
        model = type(sample)
        values = sample.model_dump(round_trip=True)
        assert model.model_construct(**values) == sample
        first_field = next(iter(model.model_fields))
        constructed = model.model_construct(_fields_set={first_field}, **values)
        assert constructed == sample
        assert constructed.model_fields_set == {first_field}
        assert sample.model_copy() == sample
        assert sample.model_copy(deep=True) == sample
        assert _replace_model(sample) == sample
        valid_update = {first_field: getattr(sample, first_field)}
        assert sample.model_copy(update=valid_update) == sample
        assert _replace_model(sample, **valid_update) == sample
        assert TypeAdapter(model).validate_python(values) == sample
        encoded = sample.model_dump_json()
        assert model.model_validate_json(encoded) == sample
        assert TypeAdapter(model).validate_json(encoded) == sample
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            assert model.construct(**values) == sample
            assert sample.copy() == sample
            assert sample.copy(deep=True) == sample
            assert sample.copy(update=valid_update) == sample


@pytest.mark.parametrize("mode", ["include", "exclude"])
def test_deprecated_copy_cannot_create_partial_invalid_models(mode: str) -> None:
    for sample in _all_m7_model_samples(_plan()):
        missing = next(iter(type(sample).model_fields))

        def operation(sample: IngestionPlanModel = sample, missing: str = missing) -> object:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                if mode == "include":
                    return sample.copy(include=set())
                return sample.copy(exclude={missing})

        _assert_invalid_model_is_content_free(operation)


def _invalid_known_update(sample: IngestionPlanModel, secret: str) -> dict[str, object]:
    if isinstance(sample, CandidateDocumentSnapshot | PlannedDocument):
        return {"relative_path": f"../{secret}"}
    if isinstance(sample, CandidateSourceSnapshot):
        return {"documents": secret}
    if isinstance(sample, EmbeddingSpecification):
        return {"identity": f"{secret}\n"}
    if isinstance(sample, PlannedProvenance):
        return {"url": f"https://user:{secret}@example.com"}
    if isinstance(sample, PlannedChunk):
        return {"text": f"{secret}\ud800"}
    if isinstance(sample, CandidateIngestionPlan | ExistingCandidateDescriptor):
        return {"contract_version": secret}
    return {"kind": secret}


@pytest.mark.parametrize(
    "surface",
    [
        "constructor",
        "model_validate",
        "type_adapter",
        "model_construct",
        "construct",
        "model_copy",
        "copy",
        "replace",
    ],
)
def test_all_m7_models_reject_invalid_known_values_without_disclosure(
    surface: str,
) -> None:
    secret = f"{surface}-invalid-known-content-secret"
    for sample in _all_m7_model_samples(_plan()):
        model = type(sample)
        values = sample.model_dump(round_trip=True)
        update = _invalid_known_update(sample, secret)
        invalid_values = values | update

        def operation(
            model: type[IngestionPlanModel] = model,
            invalid_values: dict[str, object] = invalid_values,
            sample: IngestionPlanModel = sample,
            update: dict[str, object] = update,
        ) -> object:
            if surface == "constructor":
                return model(**invalid_values)
            if surface == "model_validate":
                return model.model_validate(invalid_values)
            if surface == "type_adapter":
                return TypeAdapter(model).validate_python(invalid_values)
            if surface == "model_construct":
                return _construct_model(model, invalid_values)
            if surface == "construct":
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    return _construct_model(model, invalid_values, deprecated=True)
            if surface == "model_copy":
                return sample.model_copy(update=update)
            if surface == "replace":
                return _replace_model(sample, **update)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                return sample.copy(update=update)

        _assert_invalid_model_is_content_free(operation, secret)


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_plan_rejects_forged_document_namespace_with_recomputed_dependents() -> None:
    plan = _plan()
    document = plan.documents[0]
    forged_document_id = "doc_" + "f" * 64
    forged_chunks = []
    for chunk in document.chunks:
        chunk_values = chunk.model_dump(round_trip=True)
        chunk_values["document_id"] = forged_document_id
        chunk_values["chunk_id"] = "chk_" + _canonical_digest(
            {
                "namespace": "cairn-chunk-v1",
                "document_id": forged_document_id,
                "chunk_index": chunk.chunk_index,
                "text_sha256": chunk.text_sha256,
            }
        )
        forged_chunks.append(PlannedChunk.model_validate(chunk_values))

    document_values = document.model_dump(round_trip=True)
    document_values["document_id"] = forged_document_id
    document_values["chunks"] = tuple(forged_chunks)
    document_material = {
        key: value for key, value in document_values.items() if key != "document_plan_sha256"
    }
    document_material["provenance"] = document.provenance.model_dump(mode="json")
    document_material["chunks"] = [
        {
            **chunk.model_dump(mode="json", exclude={"embedding"}),
            "embedding": [(0.0 if scalar == 0.0 else scalar).hex() for scalar in chunk.embedding],
        }
        for chunk in forged_chunks
    ]
    document_values["document_plan_sha256"] = _canonical_digest(document_material)
    forged_document = PlannedDocument.model_validate(document_values)

    plan_values = plan.model_dump(round_trip=True)
    plan_values["documents"] = (forged_document,)
    plan_values["plan_sha256"] = _canonical_digest(
        {
            "contract_version": plan.contract_version,
            "corpus": plan.corpus.model_dump(mode="json"),
            "embedding": plan.embedding.model_dump(mode="json"),
            "semantic_manifest_sha256": plan.semantic_manifest_sha256,
            "documents": [
                {
                    "document_plan_sha256": forged_document.document_plan_sha256,
                    "chunk_ids": [chunk.chunk_id for chunk in forged_chunks],
                    "embedding_sha256s": [chunk.embedding_sha256 for chunk in forged_chunks],
                }
            ],
        }
    )

    _assert_invalid_model_is_content_free(
        lambda: CandidateIngestionPlan.model_validate(plan_values),
        forged_document_id,
    )


def test_plan_candidate_rejects_incomplete_instance_with_fixed_error() -> None:
    incomplete = object.__new__(CandidateSourceSnapshot)
    _assert_invalid_model_is_content_free(lambda: incomplete.model_copy())
    _assert_invalid_model_is_content_free(
        lambda: plan_candidate(
            corpus=_corpus(),
            source=incomplete,
            embedding=EmbeddingSpecification(identity="fixture", dimensions=2),
            embed=_embedding,
        )
    )


def _unknown_model_validation_calls(
    model: type[IngestionPlanModel],
    python_values: dict[str, object],
    json_values: dict[str, object],
) -> tuple[Callable[[], object], ...]:
    return (
        lambda: model.model_validate(python_values),
        lambda: model.model_validate_json(json.dumps(json_values)),
        lambda: model.model_validate_strings(python_values),
        lambda: TypeAdapter(model).validate_python(python_values),
        lambda: TypeAdapter(model).validate_json(json.dumps(json_values)),
        lambda: TypeAdapter(model).validate_strings(python_values),
        lambda: model.model_validate(python_values, extra="allow"),
        lambda: model.model_validate(python_values, extra="ignore"),
        lambda: model.model_validate_json(json.dumps(json_values), extra="allow"),
        lambda: model.model_validate_json(json.dumps(json_values), extra="ignore"),
        lambda: TypeAdapter(model).validate_python(python_values, extra="allow"),
        lambda: TypeAdapter(model).validate_python(python_values, extra="ignore"),
        lambda: TypeAdapter(model).validate_json(json.dumps(json_values), extra="allow"),
        lambda: TypeAdapter(model).validate_json(json.dumps(json_values), extra="ignore"),
    )


def test_all_m7_models_reject_unknown_values_without_disclosure() -> None:
    secret = "all-model-entrypoint-sentinel"
    for sample in _all_m7_model_samples(_plan()):
        model = type(sample)
        python_values = sample.model_dump(mode="python") | {"unknown": secret}
        json_values = sample.model_dump(mode="json") | {"unknown": secret}
        calls = _unknown_model_validation_calls(model, python_values, json_values)
        for call in calls:
            with pytest.raises(IngestionPlanError) as caught:
                call()
            rendered = (
                str(caught.value)
                + repr(caught.value)
                + repr(caught.value.errors(include_input=True, include_context=True))
                + caught.value.json(include_input=True, include_context=True)
            )
            assert secret not in rendered
            assert caught.value.code == "invalid_model"
            assert caught.value.__cause__ is None
            assert caught.value.__context__ is None


def test_valid_json_mode_preserves_date_bytes_and_tuple_behavior() -> None:
    source = _source({"guide.md": b"content"})
    restored_source = CandidateSourceSnapshot.model_validate_json(source.model_dump_json())
    assert restored_source == source
    provenance = _plan().documents[0].provenance
    restored_provenance = PlannedProvenance.model_validate_json(provenance.model_dump_json())
    assert restored_provenance == provenance
    assert CandidateDocumentSnapshot.model_validate_strings(
        {"relative_path": "guide.md", "content": "content"}
    ) == CandidateDocumentSnapshot(relative_path="guide.md", content=b"content")


def test_surrogates_fail_with_fixed_content_free_codes() -> None:
    secret_path = "guide-\ud800.md"
    with pytest.raises(IngestionPlanError) as invalid_path:
        CandidateDocumentSnapshot(relative_path=secret_path, content=b"content")
    assert invalid_path.value.code == "invalid_model"
    assert secret_path not in repr(invalid_path.value.errors(include_input=True))

    content = b"content"
    entry = {
        "title": "title-\ud800",
        "url": "https://docs.example.com/guide",
        "sha256": hashlib.sha256(content).hexdigest(),
        "owner": "owner",
        "reviewed_at": "2026-09-08",
        "public": True,
    }
    manifest = json.dumps(
        {"version": 1, "documents": {"guide.md": entry}}, ensure_ascii=True
    ).encode()
    source = CandidateSourceSnapshot(
        manifest_bytes=manifest,
        documents=(CandidateDocumentSnapshot(relative_path="guide.md", content=content),),
    )
    with pytest.raises(IngestionPlanError) as invalid_manifest:
        plan_candidate(
            corpus=_corpus(),
            source=source,
            embedding=EmbeddingSpecification(identity="fixture", dimensions=2),
            embed=_embedding,
        )
    assert invalid_manifest.value.code == "invalid_manifest"
    assert "surrogate" not in repr(invalid_manifest.value)


def test_no_io_guard_directly_denies_every_boundary() -> None:
    from app.providers.gemini import GeminiProvider
    from app.providers.ollama import OllamaProvider
    from app.vectorstore import VectorStoreClient

    def direct_connect() -> None:
        with socket.socket() as candidate:
            candidate.connect(("127.0.0.1", 9))

    operations: tuple[Callable[[], object], ...] = (
        lambda: open("ignored"),  # noqa: PTH123
        lambda: Path("ignored").open(),
        lambda: Path("ignored").read_bytes(),
        lambda: tuple(os.walk("ignored")),
        lambda: os.getenv("GOOGLE_API_KEY"),
        lambda: socket.getaddrinfo("localhost", 0),
        lambda: socket.create_connection(("localhost", 9)),
        direct_connect,
        lambda: sqlite3.connect(":memory:"),
        lambda: GeminiProvider.stream(object(), []),  # type: ignore[arg-type]
        lambda: OllamaProvider.stream(object(), []),  # type: ignore[arg-type]
        lambda: VectorStoreClient(Path("ignored")),
    )
    for operation in operations:
        with pytest.raises(AssertionError, match=_GUARD_MESSAGE):
            operation()


def test_hash_seed_does_not_change_serialized_plan() -> None:
    script = """
import json
from app.ingest.planner import CandidateDocumentSnapshot, CandidateSourceSnapshot
from app.ingest.planner import EmbeddingSpecification, plan_candidate
from app.retrieval_contracts import ExactCorpusReference
import hashlib
docs = {"z.md": b"Zulu", "a.md": b"Alpha"}
entries = {path: {"title": path, "url": "https://example.com/" + path,
    "sha256": hashlib.sha256(content).hexdigest(), "owner": "owner",
    "reviewed_at": "2026-09-08", "public": True} for path, content in docs.items()}
source = CandidateSourceSnapshot(manifest_bytes=json.dumps({"version": 1,
    "documents": entries}).encode(), documents=tuple(CandidateDocumentSnapshot(
    relative_path=path, content=content) for path, content in docs.items()))
plan = plan_candidate(corpus=ExactCorpusReference(corpus_id="example-support",
    corpus_version="2026.09.08"), source=source,
    embedding=EmbeddingSpecification(identity="fixture", dimensions=1),
    embed=lambda texts: tuple((float(len(text)),) for text in texts))
print(plan.model_dump_json())
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = "."
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    outputs = []
    for seed in ("1", "8675309"):
        environment["PYTHONHASHSEED"] = seed
        completed = subprocess.run(  # noqa: S603
            [sys.executable, "-c", script],
            cwd=Path(__file__).parents[1],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        outputs.append(completed.stdout)
    assert outputs[0] == outputs[1]


def test_interruption_has_no_planner_state_and_retry_is_complete() -> None:
    calls = 0

    def interrupted(input: Sequence[str]) -> Sequence[Sequence[float]]:
        nonlocal calls
        del input
        calls += 1
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        plan_candidate(
            corpus=_corpus(),
            source=_source({"guide.md": b"content"}),
            embedding=EmbeddingSpecification(identity="fixture", dimensions=2),
            embed=interrupted,
        )
    assert calls == 1
    assert _plan({"guide.md": b"content"}) == _plan({"guide.md": b"content"})
