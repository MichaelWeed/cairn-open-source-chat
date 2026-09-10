import hashlib
import io
import json
import sqlite3
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.config import Settings
from app.db import bootstrap
from app.ingest.parsers import SUPPORTED_EXTENSIONS
from app.ingest.pipeline import ingest_upload
from app.ingest.startup import CorpusIngestSummary, CorpusStartupError, corpus_paths, ingest_corpus
from app.main import create_app
from app.vectorstore import get_document_collection, get_vector_client


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
    font_dict = DictionaryObject()
    font_dict[NameObject("/F1")] = writer._add_object(font)
    resources[NameObject("/Font")] = font_dict
    page[NameObject("/Resources")] = resources

    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _write_provenance(corpus: Path) -> None:
    documents: dict[str, dict[str, object]] = {}
    for path in sorted(
        path
        for path in corpus.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    ):
        relative_path = path.relative_to(corpus).as_posix()
        documents[relative_path] = {
            "title": relative_path,
            "url": f"https://docs.example.com/{relative_path}",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "owner": "Test documentation owner",
            "reviewed_at": "2026-09-08",
            "public": True,
        }
    (corpus / "provenance.json").write_text(
        json.dumps({"version": 1, "documents": documents}), encoding="utf-8"
    )


def test_corpus_paths_rejects_missing_or_empty_supported_corpus(tmp_path: Path) -> None:
    with pytest.raises(CorpusStartupError, match="does not exist"):
        corpus_paths(tmp_path / "missing")

    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "ignored.txt").write_text("not an ingestion source")
    with pytest.raises(CorpusStartupError, match="no Markdown or PDF files"):
        corpus_paths(empty)


def test_ingest_corpus_uses_the_given_handles_for_markdown_and_pdf(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")
    corpus = tmp_path / "corpus"
    nested = corpus / "guides"
    nested.mkdir(parents=True)
    (nested / "returns.md").write_text("# Returns\n\nReturns are accepted within 30 days.")
    (corpus / "warranty.pdf").write_bytes(_pdf_bytes("Warranty coverage lasts one year."))
    _write_provenance(corpus)

    settings = Settings(database_path=tmp_path / "cairn.db", chroma_path=tmp_path / "chroma")
    db = bootstrap(settings.database_path)
    client = get_vector_client(settings)
    collection = get_document_collection(client, settings)
    try:
        summary = ingest_corpus(db=db, collection=collection, corpus_path=corpus)

        assert summary.document_count == 2
        assert summary.chunk_count >= 2
        assert collection.count() == summary.chunk_count
        sources = {row[0] for row in db.execute("SELECT source FROM documents")}
        assert sources == {"guides/returns.md", "warranty.pdf"}
    finally:
        db.close()


def test_ingest_corpus_removes_stale_corpus_vectors_and_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    present = corpus / "present.md"
    removed = corpus / "removed.md"
    present.write_text("Present corpus content")
    removed.write_text("Removed corpus content")
    _write_provenance(corpus)
    settings = Settings(database_path=tmp_path / "cairn.db", chroma_path=tmp_path / "chroma")
    db = bootstrap(settings.database_path)
    collection = get_document_collection(get_vector_client(settings), settings)
    try:
        ingest_corpus(db=db, collection=collection, corpus_path=corpus)
        ingest_upload(
            db=db,
            collection=collection,
            document_id="upload:kept",
            filename="kept.md",
            content=b"Non-corpus content",
        )
        removed.unlink()
        _write_provenance(corpus)

        ingest_corpus(db=db, collection=collection, corpus_path=corpus)

        assert collection.get(ids=["corpus:removed.md::chunk::0"])["ids"] == []
        assert db.execute("SELECT id FROM documents ORDER BY id").fetchall() == [
            ("corpus:present.md",),
            ("upload:kept",),
        ]
    finally:
        db.close()


def test_ingest_corpus_removes_emptied_corpus_vectors_and_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    present = corpus / "present.md"
    emptied = corpus / "emptied.md"
    present.write_text("Present corpus content")
    emptied.write_text("Retrievable former content")
    _write_provenance(corpus)
    settings = Settings(database_path=tmp_path / "cairn.db", chroma_path=tmp_path / "chroma")
    db = bootstrap(settings.database_path)
    collection = get_document_collection(get_vector_client(settings), settings)
    try:
        ingest_corpus(db=db, collection=collection, corpus_path=corpus)
        ingest_upload(
            db=db,
            collection=collection,
            document_id="upload:kept",
            filename="kept.md",
            content=b"Non-corpus content",
        )
        emptied.write_text("")
        _write_provenance(corpus)

        ingest_corpus(db=db, collection=collection, corpus_path=corpus)

        assert collection.get(ids=["corpus:emptied.md::chunk::0"])["ids"] == []
        result = collection.query(query_texts=["Retrievable former content"], n_results=10)
        assert all(
            "Retrievable former content" not in document
            for document in result["documents"][0]
        )
        assert db.execute("SELECT id FROM documents ORDER BY id").fetchall() == [
            ("corpus:present.md",),
            ("upload:kept",),
        ]
    finally:
        db.close()


def test_ingest_corpus_reconciles_renamed_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    original = corpus / "original.md"
    original.write_text("Renamed corpus content")
    _write_provenance(corpus)
    settings = Settings(database_path=tmp_path / "cairn.db", chroma_path=tmp_path / "chroma")
    db = bootstrap(settings.database_path)
    collection = get_document_collection(get_vector_client(settings), settings)
    try:
        ingest_corpus(db=db, collection=collection, corpus_path=corpus)
        original.rename(corpus / "renamed.md")
        _write_provenance(corpus)

        ingest_corpus(db=db, collection=collection, corpus_path=corpus)

        assert db.execute("SELECT id FROM documents ORDER BY id").fetchall() == [
            ("corpus:renamed.md",)
        ]
        assert collection.get(ids=["corpus:original.md::chunk::0"])["ids"] == []
    finally:
        db.close()


def test_ingest_corpus_does_not_reconcile_when_current_ingestion_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    current = corpus / "current.md"
    stale = corpus / "stale.md"
    current.write_text("Current corpus content")
    stale.write_text("Stale corpus content")
    _write_provenance(corpus)
    settings = Settings(database_path=tmp_path / "cairn.db", chroma_path=tmp_path / "chroma")
    db = bootstrap(settings.database_path)
    collection = get_document_collection(get_vector_client(settings), settings)
    try:
        ingest_corpus(db=db, collection=collection, corpus_path=corpus)
        stale.unlink()
        _write_provenance(corpus)

        def fail_current_ingestion(**_: object) -> object:
            raise RuntimeError("current ingestion failed")

        monkeypatch.setattr("app.ingest.startup.ingest_upload", fail_current_ingestion)
        with pytest.raises(RuntimeError, match="current ingestion failed"):
            ingest_corpus(db=db, collection=collection, corpus_path=corpus)

        assert db.execute("SELECT id FROM documents WHERE id = ?", ("corpus:stale.md",)).fetchone()
        assert collection.get(ids=["corpus:stale.md::chunk::0"])["ids"] == [
            "corpus:stale.md::chunk::0"
        ]
    finally:
        db.close()


def test_ingest_corpus_propagates_stale_vector_deletion_failure_before_metadata_delete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    current = corpus / "current.md"
    stale = corpus / "stale.md"
    current.write_text("Current corpus content")
    stale.write_text("Stale corpus content")
    _write_provenance(corpus)
    settings = Settings(database_path=tmp_path / "cairn.db", chroma_path=tmp_path / "chroma")
    db = bootstrap(settings.database_path)
    collection = get_document_collection(get_vector_client(settings), settings)
    try:
        ingest_corpus(db=db, collection=collection, corpus_path=corpus)
        stale.unlink()
        _write_provenance(corpus)

        def fail_vector_delete(*, ids: list[str]) -> None:
            assert ids == ["corpus:stale.md::chunk::0"]
            raise RuntimeError("vector deletion failed")

        monkeypatch.setattr(collection, "delete", fail_vector_delete)
        with pytest.raises(RuntimeError, match="vector deletion failed"):
            ingest_corpus(db=db, collection=collection, corpus_path=corpus)

        assert db.execute("SELECT id FROM documents WHERE id = ?", ("corpus:stale.md",)).fetchone()
        assert collection.get(ids=["corpus:stale.md::chunk::0"])["ids"] == [
            "corpus:stale.md::chunk::0"
        ]
    finally:
        db.close()


def test_lifespan_ingests_through_its_own_app_state_handles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "support.md").write_text("Support policy")
    settings = Settings(
        database_path=tmp_path / "cairn.db",
        chroma_path=tmp_path / "chroma",
        corpus_path=corpus,
    )
    observed: dict[str, object] = {}

    def record_ingestion(
        *, db: sqlite3.Connection, collection: object, corpus_path: Path
    ) -> CorpusIngestSummary:
        observed.update({"db": db, "collection": collection, "corpus_path": corpus_path})
        return CorpusIngestSummary(document_count=1, chunk_count=1)

    monkeypatch.setattr("app.main.ingest_corpus", record_ingestion)
    app = create_app(settings)
    with TestClient(app) as client:
        assert observed["db"] is app.state.db
        assert observed["collection"] is app.state.document_collection
        assert observed["corpus_path"] == corpus
        response = client.get("/readyz")
        assert response.status_code == 200
        assert response.json()["checks"]["corpus"] is True


def test_lifespan_refuses_readiness_when_configured_corpus_is_empty(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    settings = Settings(
        database_path=tmp_path / "cairn.db",
        chroma_path=tmp_path / "chroma",
        corpus_path=corpus,
    )

    with pytest.raises(CorpusStartupError, match="no Markdown or PDF files"):
        with TestClient(create_app(settings)):
            pass


def test_live_compose_requires_real_providers_and_a_read_only_corpus_mount() -> None:
    repository_root = Path(__file__).parents[2]
    live_compose = repository_root / "compose.live.yaml"
    parsed = yaml.safe_load(live_compose.read_text())
    backend = parsed["services"]["backend"]

    assert backend["environment"]["PROVIDER"] == "ollama"
    assert backend["environment"]["EMBEDDING_PROVIDER"] == "ollama"
    assert backend["environment"]["CORPUS_PATH"] == "/corpus"
    assert any(
        "CAIRN_CORPUS_PATH" in volume and ":/corpus:ro" in volume
        for volume in backend["volumes"]
    )
    assert all(":-./eval/corpus" not in volume for volume in backend["volumes"])
