"""Upload ingestion: parse -> chunk -> embed -> store, with incremental
reindex by content hash.
"""

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Literal

from app.ingest.chunking import chunk_text
from app.ingest.parsers import extract_text
from app.vectorstore import DocumentCollection

IngestStatus = Literal["created", "updated", "unchanged"]


@dataclass(frozen=True)
class IngestResult:
    document_id: str
    status: IngestStatus
    chunk_count: int


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _chunk_ids(document_id: str, count: int) -> list[str]:
    return [f"{document_id}::chunk::{i}" for i in range(count)]


def ingest_upload(
    *,
    db: sqlite3.Connection,
    collection: DocumentCollection,
    document_id: str,
    filename: str,
    content: bytes,
) -> IngestResult:
    """Ingest one uploaded file. Re-ingesting the same document_id with
    unchanged bytes is a no-op; changed bytes replace the old chunks."""
    new_hash = content_hash(content)

    row = db.execute(
        "SELECT content_hash, chunk_count FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    previous_hash = row[0] if row else None

    if previous_hash == new_hash:
        expected_ids = _chunk_ids(document_id, row[1])
        if collection.get(ids=expected_ids)["ids"] == expected_ids:
            return IngestResult(document_id=document_id, status="unchanged", chunk_count=row[1])

    text = extract_text(filename, content)
    chunks = chunk_text(text)

    collection.replace(
        ids_to_delete=_chunk_ids(document_id, row[1]) if row is not None else [],
        ids=_chunk_ids(document_id, len(chunks)),
        documents=chunks,
        metadatas=[
            {"document_id": document_id, "source": filename, "chunk_index": i}
            for i in range(len(chunks))
        ],
    )

    with db:
        db.execute(
            "INSERT INTO documents (id, source, content_hash, chunk_count) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "source = excluded.source, content_hash = excluded.content_hash, "
            "chunk_count = excluded.chunk_count, "
            "ingested_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
            (document_id, filename, new_hash, len(chunks)),
        )

    status: IngestStatus = "updated" if row is not None else "created"
    return IngestResult(document_id=document_id, status=status, chunk_count=len(chunks))
