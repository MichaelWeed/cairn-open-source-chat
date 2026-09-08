# ADR-0007: SQLite flat-vector index

**Decided:** 2026-09-07 · **Status:** Accepted

## Context

The embedded ChromaDB dependency provided a narrow synchronous collection interface,
but also pulled a large runtime dependency tree and carried an HTTP-server vulnerability
exception that did not apply to this application's embedded mode. Cairn stores a bounded,
operator-managed corpus and needs only local persistence, explicit replacement ingestion,
and deterministic nearest-neighbor retrieval.

## Decision

Use the Python standard library's SQLite module for a versioned flat-vector index. The
index stores collection-scoped chunk IDs, document text, JSON metadata, and JSON numeric
embeddings in `cairn-vectors-v1.sqlite3` below the configured `CHROMA_PATH`. It exposes
the existing synchronous add, delete, count, get, and query shapes. Querying uses squared
Euclidean distance and orders ties by stable chunk ID.

Existing Chroma persistence files are neither read nor altered. Operators re-ingest a
configured corpus explicitly. `CHROMA_PATH` remains the configuration name for this
milestone, even though it now supplies the SQLite vector-index directory.

## Consequences

The application has no vector database runtime dependency and no Chroma-specific OSV
exception. Writes are transactional, and malformed persisted embeddings or dimension
mismatches fail explicitly. The index is appropriate for Cairn's bounded local corpus,
not for distributed or high-scale approximate-neighbor workloads.
