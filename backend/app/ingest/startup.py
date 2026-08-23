"""Corpus ingestion owned by the application's lifespan.

The live container path must never open a second Chroma client. Callers pass
the lifespan-owned SQLite connection and document collection into this module.
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from chromadb.api.models.Collection import Collection

from app.ingest.parsers import SUPPORTED_EXTENSIONS
from app.ingest.pipeline import ingest_upload


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

    paths = sorted(
        path
        for path in corpus_path.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    if not paths:
        raise CorpusStartupError(
            f"configured corpus directory {corpus_path} contains no Markdown or PDF files"
        )
    return paths


def ingest_corpus(
    *, db: sqlite3.Connection, collection: Collection, corpus_path: Path
) -> CorpusIngestSummary:
    """Ingest a non-empty mounted corpus through the application's own handles."""
    results = []
    for path in corpus_paths(corpus_path):
        content = path.read_bytes()
        if not content.strip():
            continue
        relative_path = path.relative_to(corpus_path).as_posix()
        results.append(
            ingest_upload(
                db=db,
                collection=collection,
                document_id=f"corpus:{relative_path}",
                filename=relative_path,
                content=content,
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
    return CorpusIngestSummary(document_count=len(results), chunk_count=chunk_count)
