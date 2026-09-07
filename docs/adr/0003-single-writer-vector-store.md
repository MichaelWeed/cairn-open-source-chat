# ADR-0003 — The vector store has exactly one in-process writer

**Decided:** 2026-07-12, from an empirical failure · **Status:** Superseded by ADR-0007

## Context

This decision was forced by observed behaviour, not chosen from a design menu.

While stress-testing the retrieval pipeline, opening a second
`chromadb.PersistentClient` against the same `CHROMA_PATH` — from another process or
subprocess — desynchronized the long-lived server handle's view of the on-disk HNSW
segments. Every subsequent retrieval query on the affected collection then failed
with `chromadb.errors.InternalError: ... Nothing found on disk`, and stayed broken
until the process restarted.

Reproduced live against chromadb 1.5.9 in embedded mode. That version offers no
lighter-weight reload or reconnect call — the only recovery API is `reset()`, which
wipes the entire store. Notably, a `TestClient`-only test cannot reproduce this at
all, because it never creates the second handle.

## Decision

Every ingestion path — upload, future scrape ingestion, any future admin reindex —
writes through the same `Collection` handle the chat endpoint queries, held on the
application state. No code opens a second `PersistentClient` against the live
`CHROMA_PATH`.

Any regression test for this must exercise two separate `Collection` handles against
one path, since that is the only construction that reproduces the failure.

## Consequences

**This is a permanent design constraint, not a stopgap.** Because no refresh API
exists in this chromadb version, in-process single-writer is the only correct shape
available. Treating it as temporary would be wrong.

**It constrains the pending upload endpoint.** The HTTP ingestion route must reuse
`request.app.state.document_collection`, never construct its own client. This is the
single most likely way for a future contributor to reintroduce the bug, which is why
it is written down here rather than left as a comment.

**It rules out an entire scaling approach.** An early planning assumption — that a
read replica could mount the Chroma persistence directory read-only — is invalid. A
read replica's handle goes stale in exactly the same way, and would eventually throw
the same error rather than merely serving outdated results.

**The escape route has a security precondition.** Horizontal scaling requires Chroma
in client/server mode, with one server process owning on-disk state and app replicas
as stateless HTTP clients. But `osv-scanner.toml` currently ignores a Critical
pre-auth code-injection CVE in chromadb's HTTP server API *specifically because this
deployment runs no such listener*. Adopting client/server mode voids that exception.
Whoever takes on scaling must first verify a fixed chromadb version exists, or fully
network-isolate the Chroma service — not merely swap in `HttpClient` and move on.
