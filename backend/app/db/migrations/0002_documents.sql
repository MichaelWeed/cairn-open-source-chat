-- Per-document ingestion metadata (task 2.2). content_hash drives
-- incremental reindex: unchanged hash on re-ingest is a no-op.
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    chunk_count INTEGER NOT NULL,
    ingested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
