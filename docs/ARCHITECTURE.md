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
│   inference ──▶ Ollama  ◀── the only         │
│                            outbound call     │
└──────────────────────────────────────────────┘
```

Three properties of this diagram carry most of the design weight:

* **There is no boundary the operator does not own.** The only outbound network
  call in the current build is to Ollama, which normally runs in the same compose
  stack. No hosted provider is implemented; a future hosted adapter would create
  a new data boundary that must be documented explicitly.
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
| Provider adapters | `backend/app/providers/` | Built: echo, Ollama |
| Embeddings | `backend/app/embeddings/` | Built: fake, Ollama |
| Vector store | `backend/app/vectorstore.py` | Built. SQLite flat index, version 1, see [ADR-0007](adr/0007-sqlite-flat-vector-index.md) |
| Retrieval + refusal | `backend/app/retrieval.py` | Built |
| Ingestion pipeline | `backend/app/ingest/` | Built as a callable; no HTTP route reaches it |
| Rate limiting | `backend/app/ratelimit.py` | Built. In-process, single-instance |
| Metadata store | `backend/app/db/` | Built. SQLite, WAL |
| Config | `backend/app/config.py` | Built |
| Demo page | `backend/app/static/demo/` | Built. Separate static demo UI, not the embeddable widget |
| Evaluation harness | `eval/` | Built. One committed report |
| Embeddable widget | `widget/src/` | Built. Generic `<cairn-chat>` custom element with a shadow-DOM chat UI; `api-url` and `assistant-name` are its supported attributes |
| Tool registry, escalation | — | **Not built** |
| Guardrail middleware, adversarial suite | `backend/tests/adversarial/` | **Not built.** Directory holds `.gitkeep` |
| Admin surfaces and authentication | — | **Not built.** Nothing to authenticate against |
| Release bundler | `Makefile: release` | **Not built.** Prints a placeholder |

## 3. How a chat request flows

Built and tested today, in order:

1. **Contract validation.** The request body is a Pydantic model with
   `extra="forbid"`. Unknown fields are rejected outright; the 500-character message
   cap and 5-turn history limit are enforced here, before any handler code runs.
2. **Origin check.** The `Origin` header is checked against the allowlist. This is
   a browser-embedding control, not authentication — a non-browser client sending no
   `Origin` is not blocked by it, and the security documentation says so.
3. **Rate limiting.** Per-IP and per-session token buckets, checked before any
   provider call, so an abusive client cannot burn inference capacity.
4. **Retrieval.** The query is embedded and the vector store returns the closest
   chunks with distances.
5. **The refusal gate.** If the closest chunk exceeds the distance threshold, the
   request is refused *mechanically* and **the provider is never called**. This
   ordering is the substance of the no-hallucination claim: the cheapest and most
   reliable way to not fabricate an answer is to not ask the model.
6. **Prompt assembly.** Surviving chunks are wrapped in a delimited
   `<retrieved-context>` block with instructions that its contents are untrusted
   data, not instructions.
7. **Streaming.** The provider streams tokens; the endpoint emits `status`,
   `citations`, `chunk`, `ping`, and `done`/`error` events per the contract.

Errors ride the SSE stream rather than the HTTP status code — a rate-limited
request returns HTTP 200 with a `rate_limited` error event. This surprises people
and is verified explicitly in the QA checklist.

## 4. Invariants

These hold across every part of the system. Breaking one is an architecture change,
not a bug fix.

* **The server holds no conversation state.** History travels with the request from
  the client. Consequence: the backend is horizontally scalable in principle, and
  chat text never needs to touch disk. See [ADR-0004](adr/0004-stateless-conversation.md).
* **The vector index is local and versioned.** Every ingestion path writes through
  the lifespan-owned collection handle. The SQLite flat index is stored at
  `CHROMA_PATH/cairn-vectors-v1.sqlite3`; legacy Chroma files are not read or
  changed, and corpus re-ingestion is explicit. See
  [ADR-0007](adr/0007-sqlite-flat-vector-index.md).
* **Contracts change only with their consumers.** The wire format is a frozen
  Pydantic model; changing it requires updating widget, tests, and documentation in
  the same change. See [ADR-0002](adr/0002-frozen-wire-contracts.md).
* **Retrieved content is untrusted.** Operator documents are data, never
  instructions. Note honestly: this is currently a prompt-level mitigation whose
  adversarial test suite does not exist yet.
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

**Runtime:** Ollama for inference and embeddings; Python's standard-library SQLite
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
