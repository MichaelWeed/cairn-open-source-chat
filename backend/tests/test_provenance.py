import hashlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import bootstrap
from app.ingest.pipeline import ingest_upload
from app.ingest.startup import CorpusStartupError, ingest_corpus
from app.main import create_app
from app.providers.echo import EchoProvider
from app.retrieval import build_citations, retrieve_chunks
from app.vectorstore import DocumentCollection, get_document_collection, get_vector_client

Env = tuple[sqlite3.Connection, DocumentCollection]


@pytest.fixture(autouse=True)
def fake_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    settings = Settings(database_path=tmp_path / "cairn.db", chroma_path=tmp_path / "vectors")
    db = bootstrap(settings.database_path)
    client = get_vector_client(settings)
    try:
        yield db, get_document_collection(client, settings)
    finally:
        client.close()
        db.close()


def _entry(content: bytes, **overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "title": "Returns policy",
        "url": "https://docs.example.com/policies/returns",
        "sha256": hashlib.sha256(content).hexdigest(),
        "owner": "Documentation team",
        "reviewed_at": "2026-09-08",
        "public": True,
    }
    entry.update(overrides)
    return entry


def _write_manifest(corpus: Path, documents: dict[str, dict[str, object]]) -> None:
    (corpus / "provenance.json").write_text(
        json.dumps({"version": 1, "documents": documents}), encoding="utf-8"
    )


def _invalid_manifest(kind: str, content: bytes) -> str:
    entry = _entry(content)
    if kind == "malformed":
        return "{"
    if kind == "duplicate":
        encoded = json.dumps(entry)
        return f'{{"version":1,"documents":{{"returns.md":{encoded},"returns.md":{encoded}}}}}'
    if kind == "version":
        return json.dumps({"version": 2, "documents": {"returns.md": entry}})
    if kind == "missing":
        documents: dict[str, object] = {}
    elif kind == "unlisted":
        documents = {"returns.md": entry, "ghost.md": entry}
    elif kind == "non-public":
        documents = {"returns.md": _entry(content, public=False)}
    elif kind == "hash-mismatch":
        documents = {"returns.md": _entry(content, sha256="0" * 64)}
    elif kind == "incomplete":
        incomplete = _entry(content)
        del incomplete["owner"]
        documents = {"returns.md": incomplete}
    elif kind == "non-http-url":
        documents = {"returns.md": _entry(content, url="document://returns")}
    elif kind == "malformed-http-url":
        documents = {"returns.md": _entry(content, url="https://[invalid")}
    elif kind == "unstable-path":
        documents = {"../returns.md": entry}
    elif kind == "invalid-review-date":
        documents = {"returns.md": _entry(content, reviewed_at="09/08/2026")}
    else:  # pragma: no cover - the parameter list below is exhaustive
        raise AssertionError(f"unknown test manifest kind: {kind}")
    return json.dumps({"version": 1, "documents": documents})


def _parse_sse(body: str) -> list[tuple[str, dict[str, object]]]:
    events = []
    for block in body.strip().split("\n\n"):
        lines = block.splitlines()
        event_type = next(
            line.removeprefix("event: ") for line in lines if line.startswith("event: ")
        )
        data = next(line.removeprefix("data: ") for line in lines if line.startswith("data: "))
        events.append((event_type, json.loads(data)))
    return events


def test_startup_requires_a_provenance_manifest(tmp_path: Path, env: Env) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "returns.md").write_text("Returns are accepted within 30 days.")
    db, collection = env

    with pytest.raises(CorpusStartupError, match="provenance.json"):
        ingest_corpus(db=db, collection=collection, corpus_path=corpus)

    assert collection.count() == 0


def test_startup_rejects_symlinked_document_without_mutating_existing_state(
    tmp_path: Path, env: Env
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    outside_content = b"Content outside the configured corpus root."
    outside_document = tmp_path / "outside.md"
    outside_document.write_bytes(outside_content)
    (corpus / "returns.md").symlink_to(outside_document)
    _write_manifest(corpus, {"returns.md": _entry(outside_content)})
    db, collection = env
    ingest_upload(
        db=db,
        collection=collection,
        document_id="corpus:existing.md",
        filename="existing.md",
        content=b"Existing corpus content",
    )

    with pytest.raises(CorpusStartupError, match="symbolic link"):
        ingest_corpus(db=db, collection=collection, corpus_path=corpus)

    assert collection.get(ids=["corpus:existing.md::chunk::0"])["documents"] == [
        "Existing corpus content"
    ]
    assert collection.get(ids=["corpus:returns.md::chunk::0"])["ids"] == []
    assert db.execute("SELECT id FROM documents ORDER BY id").fetchall() == [
        ("corpus:existing.md",)
    ]


def test_startup_rejects_symlinked_manifest_without_mutating_existing_state(
    tmp_path: Path, env: Env
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    content = b"Returns are accepted within 30 days."
    (corpus / "returns.md").write_bytes(content)
    outside_manifest = tmp_path / "outside-provenance.json"
    outside_manifest.write_text(
        json.dumps({"version": 1, "documents": {"returns.md": _entry(content)}}),
        encoding="utf-8",
    )
    (corpus / "provenance.json").symlink_to(outside_manifest)
    db, collection = env
    ingest_upload(
        db=db,
        collection=collection,
        document_id="corpus:existing.md",
        filename="existing.md",
        content=b"Existing corpus content",
    )

    with pytest.raises(CorpusStartupError, match="symbolic link"):
        ingest_corpus(db=db, collection=collection, corpus_path=corpus)

    assert collection.get(ids=["corpus:existing.md::chunk::0"])["documents"] == [
        "Existing corpus content"
    ]
    assert collection.get(ids=["corpus:returns.md::chunk::0"])["ids"] == []
    assert db.execute("SELECT id FROM documents ORDER BY id").fetchall() == [
        ("corpus:existing.md",)
    ]


def test_manifest_accepts_a_160_unicode_character_title(tmp_path: Path, env: Env) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    content = b"Returns are accepted within 30 days."
    title = "é" * 160
    (corpus / "returns.md").write_bytes(content)
    _write_manifest(corpus, {"returns.md": _entry(content, title=title)})
    db, collection = env

    ingest_corpus(db=db, collection=collection, corpus_path=corpus)

    stored = collection.get(ids=["corpus:returns.md::chunk::0"])
    assert stored["metadatas"][0]["citation_title"] == title


def test_manifest_rejects_a_161_unicode_character_title_before_writes(
    tmp_path: Path, env: Env
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    content = b"Returns are accepted within 30 days."
    (corpus / "returns.md").write_bytes(content)
    _write_manifest(corpus, {"returns.md": _entry(content, title="é" * 161)})
    db, collection = env
    ingest_upload(
        db=db,
        collection=collection,
        document_id="corpus:existing.md",
        filename="existing.md",
        content=b"Existing corpus content",
    )

    with pytest.raises(CorpusStartupError, match="at most 160 Unicode characters"):
        ingest_corpus(db=db, collection=collection, corpus_path=corpus)

    assert collection.get(ids=["corpus:existing.md::chunk::0"])["documents"] == [
        "Existing corpus content"
    ]
    assert collection.get(ids=["corpus:returns.md::chunk::0"])["ids"] == []
    assert db.execute("SELECT id FROM documents ORDER BY id").fetchall() == [
        ("corpus:existing.md",)
    ]


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("malformed", "valid JSON"),
        ("duplicate", "duplicate"),
        ("version", "version must be 1"),
        ("missing", "missing provenance"),
        ("unlisted", "unlisted provenance"),
        ("non-public", "not public"),
        ("hash-mismatch", "hash mismatch"),
        ("incomplete", "required fields"),
        ("non-http-url", "HTTP"),
        ("malformed-http-url", "HTTP"),
        ("unstable-path", "stable relative path"),
        ("invalid-review-date", "YYYY-MM-DD"),
    ],
)
def test_invalid_provenance_fails_before_ingestion(
    kind: str, message: str, tmp_path: Path, env: Env
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    content = b"Returns are accepted within 30 days."
    (corpus / "returns.md").write_bytes(content)
    (corpus / "provenance.json").write_text(_invalid_manifest(kind, content), encoding="utf-8")
    db, collection = env

    with pytest.raises(CorpusStartupError, match=message):
        ingest_corpus(db=db, collection=collection, corpus_path=corpus)

    assert collection.count() == 0
    assert db.execute("SELECT COUNT(*) FROM documents").fetchone() == (0,)


def test_manifested_startup_preserves_public_citation_metadata(
    tmp_path: Path, env: Env
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    content = b"Returns are accepted within 30 days."
    (corpus / "returns.md").write_bytes(content)
    _write_manifest(corpus, {"returns.md": _entry(content)})
    db, collection = env

    ingest_corpus(db=db, collection=collection, corpus_path=corpus)
    chunks = retrieve_chunks(collection, "return window")
    citations = build_citations(chunks)

    assert citations[0].title == "Returns policy"
    assert citations[0].url == "https://docs.example.com/policies/returns"


def test_provenance_change_refreshes_metadata_for_unchanged_content(
    tmp_path: Path, env: Env
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    content = b"Returns are accepted within 30 days."
    (corpus / "returns.md").write_bytes(content)
    _write_manifest(corpus, {"returns.md": _entry(content)})
    db, collection = env
    ingest_corpus(db=db, collection=collection, corpus_path=corpus)

    _write_manifest(
        corpus,
        {
            "returns.md": _entry(
                content,
                title="Returns and refunds",
                url="https://docs.example.com/policies/returns-and-refunds",
            )
        },
    )
    ingest_corpus(db=db, collection=collection, corpus_path=corpus)

    citation = build_citations(retrieve_chunks(collection, "return window"))[0]
    assert citation.title == "Returns and refunds"
    assert citation.url == "https://docs.example.com/policies/returns-and-refunds"


def test_manifested_startup_emits_public_citation_through_existing_sse_contract(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    content = b"Returns are accepted within 30 days."
    (corpus / "returns.md").write_bytes(content)
    _write_manifest(corpus, {"returns.md": _entry(content)})
    settings = Settings(
        database_path=tmp_path / "cairn.db",
        chroma_path=tmp_path / "vectors",
        corpus_path=corpus,
        retrieval_max_distance=1000.0,
    )

    with TestClient(create_app(settings, provider=EchoProvider())) as client:
        response = client.post(
            "/api/v1/chat/message",
            json={"session_id": "provenance-test", "message": "What is the return window?"},
        )

    assert response.status_code == 200
    citations = next(data for event, data in _parse_sse(response.text) if event == "citations")
    assert citations["sources"] == [
        {
            "id": "corpus:returns.md",
            "title": "Returns policy",
            "url": "https://docs.example.com/policies/returns",
        }
    ]
