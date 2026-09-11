# Privacy and Data Segregation

This document describes what data Cairn actually handles today, where it lives, and who it's
segregated from — grounded in the current codebase (Phase 1-2), not aspirational design. See
[docs/SECURITY.md](SECURITY.md) for the security controls around that data.

## Not a multi-tenant product

**Multi-tenancy is an explicit non-goal.** One Cairn deployment serves one
organization's knowledge base to that organization's site visitors. There's no concept of separate
customer accounts sharing infrastructure, so "data segregation" here doesn't mean tenant-A-can't-see-
tenant-B the way it would in a multi-tenant SaaS — it means: end-user chat activity is segregated
from the operator's knowledge base, from other end-users' sessions, and from any admin surface, all
within a single-tenant deployment. If you need to serve multiple distinct organizations, run
separate deployments; nothing in this codebase is designed to isolate them within one instance.

Similarly, **auth-aware / per-user answers are a non-goal**. Cairn never personalizes an answer
based on who's asking — there's no end-user account system, and (per docs/SECURITY.md) there never
will be one gating chat answers, even once Phase 5's admin auth ships.

## What data exists, and where

| Data | Where it lives | Retention |
| --- | --- | --- |
| Chat message text (the question a visitor types) | Nowhere, server-side. Passed through the rate limiter (keyed by IP/session, not content), the retrieval query, and the provider call, then discarded. | None — never written to disk. |
| Conversation history | The current demo sends an empty `history` array on every request. The generic widget keeps bounded completed non-refusal history in memory and a session identifier in browser session storage when available; the API accepts up to five caller-supplied turns and the server remains stateless ([WIDGET.md](WIDGET.md)). | None on the server. Demo history is empty; generic-widget history lasts only for the page lifetime and `Clear chat` removes it and rotates the session ID. |
| `session_id` | Client-generated opaque identifier (`crypto.randomUUID()` in the demo page), used only as a rate-limit bucket key. Not linked to any account or identity — none exists to link it to. | Lives as long as the client keeps it (`sessionStorage`). |
| Ingested documents (the operator's knowledge base) | Chunk text + embeddings in the local SQLite vector index (`CHROMA_PATH`/`cairn-vectors-v1.sqlite3`); per-document metadata (`id`, `source`, `content_hash`, `chunk_count`, `ingested_at` - no raw content) in SQLite's `documents` table. Legacy Chroma files are untouched and are not read. | Until re-ingested or (once task 5.3 ships) deleted via the admin content surface. |
| Structured logs | stdout, JSON. By convention (not a mechanical filter — see docs/SECURITY.md), never includes message bodies or document content: latency, guardrail stage outcomes, error codes, query counts only. | Whatever your log aggregation/host retains. |
| Provider usage metadata | Bounded token counts, provider/model identifiers, attempt number, and optional service tier are normalized in memory. They are excluded from public SSE and are not persisted or logged by the built path. | Request lifetime only. |
| Readiness evidence | Fixed internal states for database, selected retrieval route, corpus, provider/model, embedding, exact corpus, and budget requirement. Raw responses and exceptions are discarded; public `/readyz` exposes only its existing status and three booleans. | One readiness request only. |
| Immutable candidate and lifecycle records | In development-only injected persistence, reviewed document metadata, chunk text, embeddings, and a signature envelope are stored under one exact corpus ID/version. Separate lifecycle records retain only content-free hashes, counts, embedding/signer identity, exact state, pointer, and immutable audits. | Candidate records have no deletion policy. Lifecycle removal is terminal and logical only; it does not delete candidate content or audits. |
| Metrics | Not implemented yet. The design intent (DEVELOPER_README.md §5) is counters and topic labels only, never message content — recorded here so the commitment is visible before the code exists. | N/A |
| Escalation tickets | Not implemented yet (task 3.4). Will be off by default when it ships. | N/A |

The widget's optional privacy and operator-owned handoff links never receive a
message, history, session, citation, source page, referrer, error body, or widget
state. Handoff navigation requires a user click. Widget host events contain only
fixed version, enum, retryability, and citation-count fields. See
[WIDGET.md](WIDGET.md) for the exact browser data flow.

The knowledge-base corpus is operator-provided content (docs, help articles, product pages) — by
design, Cairn answers from that corpus, not from anything a site visitor tells it. A visitor's chat
message is never added to the retrievable corpus.

## Third-party data flow depends on your provider choice

* **Ollama (default, local).** Chat message text and retrieved context go to the Ollama process
  named in `OLLAMA_BASE_URL`. If that's running in your own compose stack / infrastructure, nothing
  leaves your environment.
* **Gemini (optional, hosted).** When explicitly selected, the current chat message,
  up to five bounded history turns, the server instruction, and retrieved support
  context leave the operator's infrastructure for Google. Public widget context,
  session IDs, client IPs, and database paths are not included in that provider
  request. Retrieved support contains only server-derived ordinals plus the validated
  opaque `source` and text for chunks at or below the request threshold. It excludes
  citation title/URL, chunk/document/corpus IDs, distance, active pointer, public
  widget context, session/IP data, storage paths derived by Cairn, and credentials.
  Gemini may return token counts and a service tier, which Cairn normalizes
  in memory without retaining raw provider metadata. Cairn does not persist chat
  transcripts or normalized usage metadata.
* **Firestore retrieval (optional, development/test only).** The current question is
  embedded by the selected embedding provider and the query vector is sent to the
  configured Google Cloud project through Application Default Credentials. The
  bounded result can contain corpus chunk text and citation metadata. Cairn does
  not send conversation history, session IDs, client IPs, or credentials to the
  Firestore query, and the adapter neither writes nor logs query/result content.
  No live Firestore call is part of validation.
* **Firestore candidate persistence (optional, development/test only).** An
  explicitly injected service can send reviewed candidate metadata, chunk text,
  vectors, and an attestation envelope to the fixed exact-scope collections. It
  never logs record values, hashes, signer key IDs, signatures, credentials, or SDK
  responses, and encoded-size agreement is operation-local rather than retained in
  a content-derived cache. The signer-free verification service rereads durable
  records and returns only immutable content-free evidence. Cairn ships no signing
  key or production signing implementation, and validation makes no credential
  lookup or live write.
* **Corpus lifecycle registry (optional, development/test only).** An explicitly
  injected immutable trust policy gates ready state and exact active-pointer changes.
  Lifecycle documents and audits contain no content, provenance, vector, signature,
  actor, time, request, path, URL, project, database, credential, or key material.
  Logical removal retains evidence and all audits; there is no TTL, garbage collector,
  physical delete, or automatic rollback.
* **Active retrieval routing (optional, development/test only).** Each request resolves
  one exact route and retains it only for that request. The lifecycle resolver keeps at
  most one content-free verification fingerprint and its policy version/generation; it
  retains no query, content, vector, signature, adapter, verifier, policy object, path,
  URL, credential, SDK result, or provider data. Binding and lifecycle failures take the
  existing content-free refusal path before embedding, Firestore vector-query, citation,
  or provider work.

## Backups

Per DEVELOPER_README.md §9, backup = copy the SQLite metadata database and
`CHROMA_PATH/cairn-vectors-v1.sqlite3`.
That backup contains your ingested knowledge-base corpus and document metadata — it does **not**
contain chat transcripts, because those were never written to disk in the first place.
