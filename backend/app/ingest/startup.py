"""Corpus ingestion owned by the application's lifespan.

The live container path must never open a second vector-store client. Callers pass
the lifespan-owned SQLite connection and document collection into this module.
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.ingest.parsers import SUPPORTED_EXTENSIONS
from app.ingest.pipeline import _chunk_ids, ingest_upload
from app.ingest.provenance import CorpusProvenanceError, load_provenance_manifest
from app.vectorstore import DocumentCollection


class CorpusStartupError(RuntimeError):
    """Raised when a configured startup corpus cannot establish readiness."""


@dataclass(frozen=True)
class CorpusIngestSummary:
    document_count: int
    chunk_count: int


def corpus_paths(corpus_path: Path) -> list[Path]:
    """Return supported corpus files in deterministic relative-path order."""
    if not corpus_path.is_dir():
        raise CorpusStartupError(f"configured corpus directory {corpus_path} does not exist")

    candidates = sorted(
        path
        for path in corpus_path.rglob("*")
        if path.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    for path in candidates:
        if path.is_symlink():
            relative_path = path.relative_to(corpus_path).as_posix()
            raise CorpusStartupError(
                f"configured corpus document must not be a symbolic link: {relative_path}"
            )
    paths = [path for path in candidates if path.is_file()]
    if not paths:
        raise CorpusStartupError(
            f"configured corpus directory {corpus_path} contains no Markdown or PDF files"
        )
    return paths


def ingest_corpus(
    *, db: sqlite3.Connection, collection: DocumentCollection, corpus_path: Path
) -> CorpusIngestSummary:
    """Ingest a non-empty mounted corpus through the application's own handles."""
    paths = corpus_paths(corpus_path)
    documents = {
        path.relative_to(corpus_path).as_posix(): path.read_bytes() for path in paths
    }
    try:
        provenance = load_provenance_manifest(corpus_path, documents)
    except CorpusProvenanceError as error:
        raise CorpusStartupError(str(error)) from error

    current_paths: set[str] = set()
    results = []
    for relative_path, content in documents.items():
        if not content.strip():
            continue
        current_paths.add(relative_path)
        source = provenance[relative_path]
        results.append(
            ingest_upload(
                db=db,
                collection=collection,
                document_id=f"corpus:{relative_path}",
                filename=relative_path,
                content=content,
                citation_title=source.title,
                citation_url=source.url,
            )
        )

    if not results:
        raise CorpusStartupError(
            f"configured corpus directory {corpus_path} contains no non-empty Markdown or PDF files"
        )

    chunk_count = sum(result.chunk_count for result in results)
    if chunk_count == 0:
        raise CorpusStartupError(
            f"configured corpus directory {corpus_path} contains no extractable document text"
        )

    stale_documents = db.execute(
        "SELECT id, chunk_count FROM documents WHERE id LIKE 'corpus:%' ORDER BY id"
    ).fetchall()
    for document_id, stale_chunk_count in stale_documents:
        if document_id.removeprefix("corpus:") in current_paths:
            continue
        collection.delete(ids=_chunk_ids(document_id, stale_chunk_count))
        with db:
            db.execute("DELETE FROM documents WHERE id = ?", (document_id,))

    return CorpusIngestSummary(document_count=len(results), chunk_count=chunk_count)
