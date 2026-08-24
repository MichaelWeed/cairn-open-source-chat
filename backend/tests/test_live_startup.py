import io
import sqlite3
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.config import Settings
from app.db import bootstrap
from app.ingest.startup import CorpusIngestSummary, CorpusStartupError, corpus_paths, ingest_corpus
from app.main import create_app
from app.vectorstore import get_chroma_client, get_document_collection


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

    settings = Settings(database_path=tmp_path / "cairn.db", chroma_path=tmp_path / "chroma")
    db = bootstrap(settings.database_path)
    client = get_chroma_client(settings)
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
