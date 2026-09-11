# Cairn — Architecture

What the system is, how a request moves through it, and which parts are load-bearing
constraints rather than current implementation choices. Accurate as of 2026-09-08.

For the wire contract, configuration knobs, and operational procedures, see
[DEVELOPER_README.md](../DEVELOPER_README.md). For the reasoning behind individual
decisions, see [adr/](adr/). This document is the map; the ADRs are the reasons.

**Read the built-vs-designed distinction carefully.** Roughly half the architecture
below exists and is tested; the rest is designed and not yet written. Each section
marks which.

---

## 1. System context

```
   site visitor
        │  (browser)
        ▼
┌──────────────────┐     operator's own infrastructure
│  page + widget   │     ────────────────────────────────
└────────┬─────────┘
         │ POST /api/v1/chat/message   JSON in, SSE out
         ▼
┌──────────────────────────────────────────────┐
│  Cairn backend  (FastAPI, one process)       │
│                                              │
│   retrieval ──▶ SQLite flat index (local)    │
│   metadata  ──▶ SQLite (WAL, local file)     │
│   inference ──▶ Ollama (default, local)      │
│             └─▶ Gemini (explicit opt-in)     │
└──────────────────────────────────────────────┘
```

Three properties of this diagram carry most of the design weight:

* **Local operation remains the default.** Ollama normally runs in the operator's
  compose stack. Selecting the optional Gemini adapter creates an explicit Google
  data boundary described in the privacy and security documentation.
* **Nothing upstream of the widget is trusted.** The visitor's browser is hostile
  input, and so is the operator's own ingested corpus once it reaches the model —
  retrieved chunks are wrapped as untrusted data, because a poisoned document is a
  real attack path.
* **There is no author-operated service.** Every deployment belongs to someone else.

## 2. Components

| Component | Path | State |
| --- | --- | --- |
| Wire contracts | `backend/app/api/contracts.py` | Built. Frozen — see [ADR-0002](adr/0002-frozen-wire-contracts.md) |
| Chat endpoint (SSE) | `backend/app/api/chat.py` | Built |
| Capability discovery | `backend/app/capabilities.json`, `backend/app/api/capabilities.py` | Built. Static package metadata; see [COMPATIBILITY.md](COMPATIBILITY.md) |
| Provider adapters | `backend/app/providers/` | Built: echo, Ollama, optional Gemini generation |
| Embeddings | `backend/app/embeddings/` | Built: fake, Ollama |
| Vector store | `backend/app/vectorstore.py` | Built. SQLite flat index, version 1, see [ADR-0007](adr/0007-sqlite-flat-vector-index.md) |
| Retrieval protocol + adapters + routing + evidence compiler | `backend/app/retrieval_contracts.py`, `backend/app/retrieval.py`, `backend/app/retrieval_firestore.py`, `backend/app/retrieval_route.py`, `backend/app/retrieval_integrity.py` | Built. Contract 1.0; one route per request, default `local_active` SQLite, configured static Firestore, or explicitly injected development-only attested active routing; eligible support, context, and citations come from one immutable tuple |
| Ingestion pipeline | `backend/app/ingest/` | Built; mounted startup requires a versioned provenance manifest, while direct callable ingestion retains internal citations |
| Immutable candidate planner, persistence, and lifecycle registry | `backend/app/ingest/planner.py`, `backend/app/ingest/candidate_persistence.py`, `backend/app/ingest/candidate_firestore.py`, `backend/app/corpus_lifecycle.py`, `backend/app/corpus_lifecycle_firestore.py` | Built internally for development. Pure plan, create-or-confirm storage, full attestation readback, ready state, exact active-pointer CAS, rollback, logical removal, and immutable audits; no production trust policy or application wiring |
| Rate limiting | `backend/app/ratelimit.py` | Built. In-process, single-instance |
| Metadata store | `backend/app/db/` | Built. SQLite, WAL |
| Config | `backend/app/config.py` | Built |
| Demo page | `backend/app/static/demo/` | Built. Separate static demo UI, not the embeddable widget |
| Evaluation harness | `eval/` | Built. One committed report |
| Embeddable widget | `widget/src/` | Built. Generic `<cairn-chat>` custom element with atomic production configuration, first-open capability negotiation, bounded in-page history, privacy/handoff links, content-free events, CSP nonce support, themes, and responsive accessibility; see [WIDGET.md](WIDGET.md) |
| Tool registry, escalation | — | **Not built** |
| Guardrail middleware | — | **Not built.** Input and output guardrail middleware remains planned |
| Retrieval structural adversarial gate | `backend/tests/adversarial/test_retrieval_integrity_adversarial.py` | Built. Deterministic offline poisoned-content, hook-safety, privacy, and no-external checks; not a live or model-as-judge evaluation |
| Admin surfaces and authentication | — | **Not built.** Nothing to authenticate against |
| Release bundler | `Makefile: release` | **Not built.** Prints a placeholder |

## 3. How a chat request flows

Built and tested today, in order:

1. **Contract validation.** The actual request body is capped at 16,384 bytes, then
   parsed by a frozen Pydantic model with `extra="forbid"`. Session, message,
   history, and optional context fields have explicit per-field and aggregate
   bounds before any handler code runs.
2. **Origin check.** The `Origin` header is checked against the allowlist. This is
   a browser-embedding control, not authentication — a non-browser client sending no
   `Origin` is not blocked by it, and the security documentation says so.
3. **Rate limiting.** Per-IP and per-session token buckets, checked before any
   provider call, so an abusive client cannot burn inference capacity.
4. **Retrieval.** Chat resolves one internal scope-plus-adapter route, then builds a
   bounded version 1.0 request for that exact scope. The default route is
   `local_active`. The async local adapter yields once for cancellation and then the
   local SQLite implementation embeds the exact query and returns the closest
   squared-L2 chunks. The adapter validates result shape, stable chunk identity,
   ordering, bounds, and provenance before returning any row. Exact immutable
   corpus references are not implemented by this local adapter. A static or explicitly
   injected lifecycle Firestore route carries a separately constructed exact M6 adapter;
   its descriptor and M6 private binding authority are rechecked immediately before I/O,
   so an in-flight request cannot mix active versions.
5. **Evidence compilation and refusal.** The pure compiler strictly reconstructs
   the request and untrusted adapter result, verifies echoed authority, and retains
   each chunk whose distance is at or below the request threshold without reranking.
   No eligible chunk means a mechanical refusal and no provider call.
6. **Context and citations.** One immutable eligible tuple produces both the
   bounded deterministic JSON support object and first-seen supporting citations.
   A fixed server instruction labels JSON strings as untrusted data. Oversized
   context refuses before citations or provider work; chunks are never silently
   truncated or dropped.
7. **Streaming.** The endpoint builds one validated, server-owned provider request.
   Public context metadata does not enter its instructions or retrieved context.
   Provider chunks and total output are bounded before the endpoint emits `status`,
   `citations`, `chunk`, `ping`, and `done`/`error` events per the contract.

Mounted startup ingestion validates the complete versioned provenance manifest before
writing either SQLite store. Verified public titles and canonical HTTP(S) URLs travel
in existing chunk metadata and emerge through the frozen citation response. Direct
programmatic ingestion has no public-source attestation and keeps an internal
`document://` reference. See [CORPUS-PROVENANCE.md](CORPUS-PROVENANCE.md).

Separately, the internal ingestion planner can receive the already validated
manifest bytes and exact same-read document byte snapshots with an exact corpus ID
and version. It performs deterministic extraction, LF/NFC normalization, existing
boundary-aware chunking, fixed-size positional embedding batches, and canonical
identity/digest construction without filesystem, network, database, vector-store,
or provider access. It returns one complete frozen candidate plan or fails closed;
it does not alter the current mounted startup pipeline. The separate persistence
component creates or confirms an immutable candidate header, then sorted document
and chunk batches, reads the exact scope back in stable key order, and signs a
canonical inventory only after full comparison. Its fixed collections are
`cairn_corpus_candidates_v1`, `cairn_corpus_documents_v1`,
`cairn_corpus_chunks_v1`, and `cairn_corpus_attestations_v1`. Candidate and
attestation keys are derived from the exact corpus reference, document keys are M7
document IDs, and chunk keys use the M6 bounded chunk-key helper. A distinct
signer-free verification service accepts an exact corpus, externally selected
identity, and verify-only verifier, then returns immutable content-free evidence
from a fresh complete durable readback. It does not select trust or lifecycle
state. The separate lifecycle registry borrows that verifier, captures one injected
immutable trust-policy snapshot, and records only strict content-free evidence. Ready
registration, promotion, rollback, and inactive terminal logical removal use exact
state-plus-audit transactions. An explicitly injected read-only resolver may consume
the active-state seam for chat or a content-free exact-version probe. It single-flights
full M8 refresh for one content-free fingerprint and retains no adapter, verifier, or
policy object. No production lifecycle or trust factory is selected by configuration,
and mounted startup ingestion remains separate.

The Gemini adapter imports its SDK only after explicit selection. It maps history
roles, keeps retrieved context and the current visitor question as separate JSON
data in the final user turn, applies bounded token and wall-clock limits, and
propagates task cancellation so disconnects stop work. It emits only current text
chunks to the public stream. Provider adapters also emit bounded, self-identifying
usage records internally. The chat endpoint consumes and filters those records so
the frozen SSE payload is unchanged; usage is not persisted. Pure cost helpers can
apply an operator-supplied, hash-identified price snapshot without owning a price
catalog, budget policy, or sink.

Errors ride the SSE stream rather than the HTTP status code — a rate-limited
request returns HTTP 200 with a `rate_limited` error event. This surprises people
and is verified explicitly in the QA checklist.

## 4. Invariants

These hold across every part of the system. Breaking one is an architecture change,
not a bug fix.

* **The server holds no conversation state.** History travels with the request from
  the client. Consequence: the backend is horizontally scalable in principle, and
  chat text never needs to touch disk. See [ADR-0004](adr/0004-stateless-conversation.md).
* **The default vector index is local and versioned.** Every ingestion path writes through
  the lifespan-owned collection handle. The SQLite flat index is stored at
  `CHROMA_PATH/cairn-vectors-v1.sqlite3`; legacy Chroma files are not read or
  changed, and corpus re-ingestion is explicit. See
  [ADR-0007](adr/0007-sqlite-flat-vector-index.md). The optional Firestore adapter
  is a read-only development/test retrieval boundary: it applies four exact equality filters,
  a fixed projection and vector field, a K + 1 query, and deterministic
  `(distance, chunk_id)` ordering. Candidate persistence is a separate injected
  development boundary. It performs header-first create-only writes, exact
  read-confirm replay, full document/chunk readback, framed inventory hashing, and
  immutable attestation last. A failed operation may leave an unattested prefix;
  it never cleans up or promotes that prefix. Its signer-free verifier repeats the
  exact durable readback and returns identity-bound evidence without signing or
  writing. The development-only route resolver binds one validated active state to one
  exact M6 adapter and repeats the authority check before retrieval or readiness.
  Aggregate hosted readiness remains planned.
* **Contracts change only with their consumers.** The wire format is a frozen
  Pydantic model; changing it requires updating widget, tests, and documentation in
  the same change. See [ADR-0002](adr/0002-frozen-wire-contracts.md).
* **Retrieved content is untrusted.** Operator documents are data, never
  instructions. JSON structural separation and deterministic poisoned-document
  tests prove the application boundary, but do not prove that every model will
  comply semantically with the instruction.
* **Chat content never reaches disk or logs.** Enforced by convention and review,
  not by a runtime filter — a real gap, recorded in `docs/SECURITY.md`.

## 5. Deliberate constraints and what they cost

Each of these is a decision with a known price, accepted knowingly.

**Single instance.** Rate limiting is an in-memory dictionary, the SQLite index is
a bounded local corpus, and retrieval runs as a synchronous O(N) flat-vector query.
Behind N replicas, a client's effective rate limit becomes N times the configured
value, and SQLite over a shared network filesystem is unsafe. The practical
concurrency ceiling is set by synchronous retrieval and Ollama inference parallelism,
not by the web layer. Horizontal scaling is not a supported path.

**Local models by default.** Slower and lower-quality than frontier hosted models,
in exchange for the privacy claim being structural rather than contractual. See
[ADR-0001](adr/0001-self-hosted-local-first.md).

**A fixed, operator-chosen port.** The server refuses to hunt for a free port and
fails a preflight check instead, because the embed snippet, CORS allowlist, and any
reverse proxy in front all reference one specific port. See
[ADR-0006](adr/0006-fixed-operator-chosen-port.md).

**Single-tenant.** One deployment serves one organization. Multi-tenancy is a
non-goal, so no code isolates tenants — running two organizations on one instance
is unsupported, not merely discouraged.

## 6. External dependencies

**Runtime:** Ollama for default inference and embeddings, with exact-pinned
`google-genai` available only through the optional Gemini generation profile;
Python's standard-library SQLite
for the local flat-vector index and metadata; FastAPI and uvicorn; pypdf for PDF
extraction. Every dependency has a written justification
and pin rationale in [DEPENDENCIES.md](DEPENDENCIES.md).

**Supply-chain posture,** which is an architectural property here rather than a
process detail: both lockfiles carry hashes; container images are referenced by
digest, never by floating tag; no package version younger than 14 days may enter a
lockfile; a CycloneDX SBOM is regenerated on every gate run and must not drift from
the committed copy. Any active CVE exception carries a stated reason and an explicit
re-check condition. The backend lockfile currently has no OSV exceptions.

**The validation gate is one command in two places** — the local pre-push hook and
GitHub Actions both run `make validate`, so "green locally" and "green in CI" cannot
diverge. See [ADR-0005](adr/0005-one-validation-command.md). The gate requires
osv-scanner, grype, cyclonedx-py, a container engine, and network access; as of
2026-08-21 it has only ever run on the author's machine, because nothing has been
pushed.

## 7. Deployment and operating model

The design point is one host running `docker compose` or `podman compose`, on an
operator-chosen fixed port, behind the operator's own TLS-terminating reverse proxy.
The compose file stays inside the vendor-neutral Compose Specification so Docker and
Podman are both first-class, and the Makefile detects which engine is present.

The backend serves plain HTTP. There is no TLS termination in the stack, by design —
operators are told to put a proxy in front and not to expose the published port
directly.

Backup is a file copy: the SQLite metadata database and
`CHROMA_PATH/cairn-vectors-v1.sqlite3`.
That backup contains the operator's corpus and document metadata. It contains no
chat transcripts, because none were ever written.

The operating obligation that outlives installation: no external service will notify
a self-hosted operator of CVEs disclosed after they installed, so `make verify`
re-scans the shipped SBOM against a current vulnerability database on a recommended
weekly cadence. The release bundle that would carry it is not built yet.
