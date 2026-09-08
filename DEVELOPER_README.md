# Cairn Technical Guide

This is the full technical guide for setting up, configuring, evaluating,
developing, and operating Cairn. The executive overview is [README.md](README.md).
Cairn was formerly AetherChat; [project.yaml](project.yaml) is the durable identity
record. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/adr/](docs/adr/) are
the durable design authorities, while [docs/PUBLIC-AVAILABILITY.md](docs/PUBLIC-AVAILABILITY.md)
records the delivered public-availability outcome and open tracking decision D4.

---

## 1. Architecture at a Glance

The diagram below is the target architecture, not a statement that every box is built. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) is the fuller map — component-by-component
built-vs-designed status, request flow, and the cost of each deliberate constraint;
[docs/adr/](docs/adr/) records why each decision was made.

```
[Widget (shadow DOM, vanilla TS)]
        | POST /api/v1/chat/message  (JSON in, SSE out)
        v
[FastAPI backend]
   Guardrail pipeline (ordered middleware, toggleable)
   -> Router: RAG answer | tool call | refusal
   -> Provider adapter (Ollama built | hosted adapter planned)
        |
   [SQLite flat index]  vectors
   [SQLite, WAL]        docs metadata, provider registry, config, metric counters
        |
   [Tool registry]  lookup_order_status | escalate_to_human
```

The built developer-preview slice is the FastAPI chat endpoint, a local SQLite flat
vector index and SQLite metadata state, in-process Markdown/PDF ingestion, Ollama
and echo providers, retrieval/refusal/citations, and the `/demo` page. The admin
surface, hosted provider, tools, production widget, and guardrail pipeline are planned.

Design invariants and current limits:

* **Server holds no conversation state.** The API accepts up to five caller-supplied history turns, but the current demo page sends an empty history array. Client-side history persistence belongs to the planned production widget.
* **No admin endpoints exist yet.** The planned admin authentication design is a single account with Argon2id, a SameSite=Strict session cookie, CSRF protection, and optional TOTP.
* **Contracts are frozen Pydantic models** (`extra="forbid"`). The SSE contract and tool schemas in `backend/app/api/contracts.py` are the source of truth.
* **The vector index is local and versioned.** Every ingestion path (upload, scrape,
  future admin reindex) writes through `app.state.document_collection`, which owns a
  SQLite flat index at `CHROMA_PATH/cairn-vectors-v1.sqlite3`. Legacy Chroma files
  are not read, changed, or migrated. Re-ingestion is explicit.

## 2. Developer Preview Quick Start

Prerequisites: `uv`, Ollama running on the host, and the configured chat and
embedding models already present. The default pair is `llama3.1:8b-instruct` and
`nomic-embed-text`, matching `backend/app/config.py`. `make demo` checks the
bundled corpus, Ollama connection, and both models before the server starts. It
prints exact corrective commands and never downloads models.

```sh
ollama pull llama3.1:8b-instruct
ollama pull nomic-embed-text
make demo
# open http://localhost:8080/demo
```

The command uses the existing in-process `ingest_upload()` helper with `app.state.document_collection` and `app.state.db` to ingest `eval/corpus/`, then serves the demo page with real Ollama embeddings and generation. Ask about shipping, returns, or warranties to exercise cited answers. Copy `.env.example` to `.env` only to override `CAIRN_PORT`, `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, or `EMBEDDING_MODEL`. An alternate model pair must already be present in Ollama; `make demo` checks it and never pulls it. A compose-network `OLLAMA_BASE_URL=http://ollama:11434` is rewritten to localhost for the host-run demo helper, while an explicit host URL is used as provided.

**Port:** the preview listens on `CAIRN_PORT` (default 8080). If that port is taken, `make demo` fails with the conflicting port and tells you to set `CAIRN_PORT` in `.env` before retrying. The port is deliberately fixed rather than auto-selected because the demo URL and origin allowlist must agree.

### Grounded container live beta

For the production-like localhost path, provide an explicit corpus and the site
origin that will embed Cairn, then run one command from the repository root:

```sh
CAIRN_CORPUS_PATH=/absolute/path/to/docs \
ORIGIN_ALLOWLIST=http://localhost:4173 \
make live
```

The corpus directory must contain a non-empty Markdown or PDF file. `make live`
honors a non-empty `COMPOSE_CMD` override, otherwise prefers a working
`podman compose` and falls back to `docker compose`. It starts the Compose Ollama
service first, checks the configured `OLLAMA_MODEL` and `EMBEDDING_MODEL` inside
that service, and never pulls a model. If either is missing, the command prints
the exact Compose-scoped `ollama pull` command for the operator to run explicitly.

After the grounded backend reaches `/readyz`, the command prints the selected
engine, resolved corpus, origin allowlist, Cairn URL, exact two-line generic embed,
and `make live-down`. The stack runs detached; `make live-down` stops it without
deleting the named application or model volumes. A page served from any other
origin must be added to `ORIGIN_ALLOWLIST` before its widget can call Cairn.

### Container plumbing smoke test

`make up` requires Docker or Podman with Compose. The checked-in configuration uses the echo provider and fake embeddings, does not ingest the bundled corpus, and exposes the demo page but no `/admin` route. Use it to check container, health, static-page, and API plumbing. It does not prove a grounded or cited answer. Set `PROVIDER=ollama`, `EMBEDDING_PROVIDER=ollama`, and an Ollama URL reachable from the container only when intentionally testing that alternate configuration; ingestion is still not provided by `make up`.

**Config plumbing:** compose only interpolates `.env` into `compose.yaml` — it never passes `.env` to the container by itself. Every knob in `.env.example` is therefore forwarded explicitly in the `environment:` block of `compose.yaml`, with defaults mirroring `backend/app/config.py`. Add new settings in all three places.

`compose.yaml` stays within the vendor-neutral Compose Specification (no Docker-specific extensions), so it runs unmodified under either engine. `make up`/`make down` use the available `COMPOSE_CMD`; set it explicitly (`COMPOSE_CMD="podman compose" make up`) if both are installed and you want a specific one. The grounded `make live` path uses the Podman-first validated selection described above.

## 3. Planned Configuration Surfaces

These operator surfaces are roadmap design, not current routes or UI.

| Surface | Where | Notes |
| --- | --- | --- |
| Providers | Admin UI -> Models | Add/edit/archive; exactly one active; change applies next request, no restart |
| Content | Admin UI -> Content | Scrape mode (robots.txt-respecting, allowlist) or upload (md/pdf); per-document reindex; incremental by content hash |
| Guardrails | Admin UI -> Safety | Tier 1 always on; Tier 2 (Llama Guard 3 via Ollama) toggle with measured latency cost shown |
| WISMO | Admin UI -> Tools | Deep-link mode default; carrier API adapters config-gated with credentials refs (never stored in plaintext) |
| Limits | `.env` / Admin | Rate buckets, 500-char message cap, origin allowlist, daily budget cap |

## 4. Chat API Contract

`POST /api/v1/chat/message` with `{session_id, message, history[<=5], context?}`. Response is SSE:

| Event | Payload |
| --- | --- |
| `status` | `{state, label}` |
| `chunk` | `{delta}` |
| `citations` | `{sources: [{id, title, url}]}` |
| `error` | `{code: rate_limited\|provider_unavailable\|guardrail_block\|internal, message, retryable}` |
| `ping` | `{}` heartbeat every 15 s |
| `done` | `{finish_reason}` |

Widget behavior on `error`: render message; reconnect once if `retryable`.

WISMO tool result includes `"mode": "deep_link" | "api"`. The system prompt forbids asserting delivery status when mode is `deep_link`; the model may only present the link.

## 5. Security Model

* **Prompt injection**: fixed prompt template with delimited sections; retrieved chunks wrapped in explicit untrusted-context markers; instruction hierarchy stated; adversarial suite (`backend/tests/adversarial/`) includes poisoned-document retrieval cases and must pass in CI.
* **Endpoint abuse**: Origin allowlist + CORS, per-IP and per-session token buckets, daily budget cap that degrades to a static "high demand" message, optional signed widget token.
* **Output**: PII scrubber on responses; citation-required policy; refusal template on low retrieval confidence.
* **Supply chain**: uv and npm lockfiles with hashes; CycloneDX SBOM per build; Grype gate; digest-pinned images; dependency cooldown window. Release tags ship SBOM + checksums.
* **Data**: raw messages never persisted server-side; metrics are counters and topic labels only. Optional ticket persistence (Phase 3) is off by default and documented in [docs/PRIVACY.md](docs/PRIVACY.md).

Report vulnerabilities per [docs/SECURITY.md](docs/SECURITY.md).

## 6. Evaluation Harness

```
make eval          # requires OLLAMA_MODEL and EMBEDDING_MODEL pulled in Ollama
```

Produces `eval/reports/<date>.md` with: groundedness (LLM-judged support of answers by retrieved chunks), citation precision/recall, correct-refusal rate on unanswerable questions, TTFT and P95 latency, adversarial pass/fail. Question sets are YAML in `eval/questions/`; write one for your corpus before trusting production traffic. v0 (task 2.6) is self-contained rather than pointed at an already-running deployment — it ingests `eval/corpus/` into its own ephemeral app instance via `ingest_upload()` directly, since there's no HTTP upload endpoint yet (task 2.7). The committed report is v0's proof the harness works end to end against a real model, not yet the statistically meaningful eval that will back numbers in the executive README — that's task 7.4.

### Manual QA

```
make demo          # requires OLLAMA_MODEL and EMBEDDING_MODEL pulled in Ollama
```

Boots the real app (not the `echo`/`fake` defaults `make up` uses out of the
box) with `eval/corpus/` ingested into it, and serves the demo page for
interactive browser testing. Before binding the port it checks for a non-empty
Markdown corpus, a reachable Ollama service, and both configured models. It never
pulls models. See [docs/QA_CHECKLIST.md](docs/QA_CHECKLIST.md)
for the scenarios to run and their last-verified status — the qualitative
counterpart to `make eval`'s quantitative metrics.

## 7. Development

* Python: `uv sync` in `backend/`; `ruff`, `mypy`, `pytest` gate CI. Widget: `npm ci && npm run build` in `widget/` (esbuild, size budget check < 100 KB gz).
* Contract-first: change `contracts.py` only via PR that updates widget, tests, and docs together.
* No stubs/TODOs in merged code (CI grep gate). New dependencies require a one-line justification in the PR and land in the lockfile with hashes.
* Agent-assisted contributions follow `CLAUDE.md` / `AGENTS.md`: one component + its tests per task, frozen contracts, PR review with full diffs.

## 8. Extending: Adding a Tool

1. Define a Pydantic call schema and result schema in `backend/app/tools/<name>/schema.py`.
2. Implement the executor (async, timeout-bounded, no network unless declared).
3. Register in the tool registry with `enabled: false` default.
4. Add unit tests, an integration test with a mocked executor, and at least one adversarial case.
5. Document the admin toggle.

No core changes required; the registry injects enabled tool schemas into the provider call.

## 9. Operations

* `GET /healthz` (liveness) and `GET /readyz` (provider + vector store checks); no sensitive data in either.
* Structured JSON logs: latency, guardrail stage outcomes, error codes, query counts. No message bodies.
* Backup = copy the SQLite metadata database and
  `CHROMA_PATH/cairn-vectors-v1.sqlite3` (volume-mounted).

### Concurrency, one instance

FastAPI/uvicorn runs a single async event loop, but retrieval is deliberately
synchronous: embedding and the local flat-vector query run on that loop, and the
query scans the bounded corpus in O(N) time. Do not assume concurrent retrieval
throughput. The client lock protects the SQLite connection inside this one process;
it does not make multiple application instances a supported configuration. Two
other pieces are also scoped to one instance rather than made distributed, by design:

* **Rate limiting** (`app/ratelimit.py`) is an in-process, in-memory token-bucket dict keyed by IP/session. Fine for one instance; behind multiple replicas each one enforces its own limits independently, so the effective cap for a client is `limit x replica_count`, not `limit`.
* **SQLite (WAL)** supports many concurrent readers and one writer *on one host*. It is not safe for multiple hosts writing over a shared network filesystem (NFS/EFS) — don't reach for that as a scaling shortcut.

Within those bounds, a single instance's practical ceiling for concurrent users is set by Ollama's own inference concurrency (typically low — see `OLLAMA_NUM_PARALLEL`) more than by the FastAPI layer, since a local model serves one or a few generations in parallel per GPU/CPU budget regardless of how many requests are queued.

### Scaling beyond one instance

Single instance is the v1 self-hosted design point. Horizontal scaling is not a
supported path. Do not mount either SQLite file on replicas, whether read-only or
read-write, and do not use a shared network filesystem for the index or metadata.
The local flat index and in-memory rate limiter have no cross-instance coherence
contract. A future scaling architecture requires a separate approved design; this
repository does not select a vendor or migration path for it.

```
[one operator-managed host]
  FastAPI process -> local SQLite flat index + local SQLite metadata database
                   -> operator-managed Ollama endpoint
```

## 10. Roadmap and Non-Goals

Out-of-scope feature requests (CRM sync, auth-aware answers, multi-tenant, voice) are not supported by this project. See [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) for the contribution boundary.
