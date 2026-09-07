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
| Conversation history | Not persisted by the current demo, which sends an empty `history` array on every request. The API accepts up to five caller-supplied turns, and the server remains stateless ([DEVELOPER_README.md §1](../DEVELOPER_README.md)). Client-side history persistence is planned for the production widget. | None in the current demo. |
| `session_id` | Client-generated opaque identifier (`crypto.randomUUID()` in the demo page), used only as a rate-limit bucket key. Not linked to any account or identity — none exists to link it to. | Lives as long as the client keeps it (`sessionStorage`). |
| Ingested documents (the operator's knowledge base) | Chunk text + embeddings in the local SQLite vector index (`CHROMA_PATH`/`cairn-vectors-v1.sqlite3`); per-document metadata (`id`, `source`, `content_hash`, `chunk_count`, `ingested_at` — no raw content) in SQLite's `documents` table. Legacy Chroma files are untouched and are not read. | Until re-ingested or (once task 5.3 ships) deleted via the admin content surface. |
| Structured logs | stdout, JSON. By convention (not a mechanical filter — see docs/SECURITY.md), never includes message bodies or document content: latency, guardrail stage outcomes, error codes, query counts only. | Whatever your log aggregation/host retains. |
| Metrics | Not implemented yet. The design intent (DEVELOPER_README.md §5) is counters and topic labels only, never message content — recorded here so the commitment is visible before the code exists. | N/A |
| Escalation tickets | Not implemented yet (task 3.4). Will be off by default when it ships. | N/A |

The knowledge-base corpus is operator-provided content (docs, help articles, product pages) — by
design, Cairn answers from that corpus, not from anything a site visitor tells it. A visitor's chat
message is never added to the retrievable corpus.

## Third-party data flow depends on your provider choice

* **Ollama (default, local).** Chat message text and retrieved context go to the Ollama process
  named in `OLLAMA_BASE_URL`. If that's running in your own compose stack / infrastructure, nothing
  leaves your environment.
* **Any future hosted, OpenAI-compatible provider** would send chat message text and retrieved
  context to that third party. Only `EchoProvider` and `OllamaProvider` are implemented today;
  there is no hosted provider to configure in the current build.

## Backups

Per DEVELOPER_README.md §9, backup = copy the SQLite file and the Chroma persistence directory.
That backup contains your ingested knowledge-base corpus and document metadata — it does **not**
contain chat transcripts, because those were never written to disk in the first place.
