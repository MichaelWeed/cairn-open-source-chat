# Cairn Technical Guide

This is the full technical guide for setting up, configuring, evaluating,
developing, and operating Cairn. The executive overview is [README.md](README.md).
Cairn was formerly AetherChat; [project.yaml](project.yaml) is the durable identity
record. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/adr/](docs/adr/) are
the durable design authorities, while [docs/PUBLIC-AVAILABILITY.md](docs/PUBLIC-AVAILABILITY.md)
records the delivered public-availability outcome and open tracking decision D4.
[docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) records the exact packaged capability
manifest, compatibility versions, and pre-1.0 upgrade rules.
[docs/WIDGET.md](docs/WIDGET.md) is the canonical production widget configuration,
event, privacy, CSP, CORS, accessibility, and migration guide.

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
   -> Provider adapter (Ollama built | optional Gemini built)
        |
   [SQLite flat index]  vectors
   [SQLite, WAL]        docs metadata, provider registry, config, metric counters
        |
   [Tool registry]  lookup_order_status | escalate_to_human
```

The built developer-preview slice is the FastAPI chat endpoint, a local SQLite flat
vector index and SQLite metadata state, in-process Markdown/PDF ingestion, Ollama,
optional Gemini, and echo providers, retrieval/refusal/citations, an optional
development-only Firestore retrieval adapter, the `/demo` page, and the generic
`<cairn-chat>` custom element with a production-configurable shadow-DOM chat UI,
first-open capability negotiation, bounded page-lifetime history, safe optional
privacy/handoff links, themes, and content-free host events. The admin surface,
hosted operations, tools, and
guardrail pipeline are planned.

`GET /api/v1/capabilities` returns the static packaged capability manifest. Use it
to discover exact compatibility versions and built, development-only, or planned
states; use `/readyz` for runtime readiness.

Design invariants and current limits:

* **Server holds no conversation state.** The API accepts up to five caller-supplied history turns. The current demo page sends an empty history array; the generic widget keeps a session identifier in browser session storage when available and bounded history in memory.
* **No admin endpoints exist yet.** The planned admin authentication design is a single account with Argon2id, a SameSite=Strict session cookie, CSRF protection, and optional TOTP.
* **Contracts are frozen Pydantic models** (`extra="forbid"`). The SSE contract and tool schemas in `backend/app/api/contracts.py` are the source of truth.
* **Provider stream events are internal and self-identifying.** Text and usage
  variants are frozen, discriminated models. Usage records carry provider, model,
  attempt, optional tier, and bounded token counts; the public SSE contract remains
  unchanged and never emits usage records.
* **The vector index is local and versioned.** Every ingestion path (upload, scrape,
  future admin reindex) writes through `app.state.document_collection`, which owns a
  SQLite flat index at `CHROMA_PATH/cairn-vectors-v1.sqlite3`. Legacy Chroma files
  are not read, changed, or migrated. Re-ingestion is explicit.
* **Candidate planning is offline and pure.** `app.ingest.planner` accepts one
  validated version 1 manifest, the exact same-read document bytes, an exact
  `ExactCorpusReference`, an embedding identity/dimension pair, and an injected
  synchronous embedding function. Contract 1.0 sorts paths by UTF-8 bytes,
  normalizes with `cairn-nfc-lf-v1`, chunks with
  `cairn-boundary-chunks-v1:size=800:overlap=100`, and emits a complete frozen plan
  with stable IDs and SHA-256 digests. Inputs are bounded to 1,024 documents, 8 MiB
  per document, 64 MiB total source text, 16,384 chunks per document, 65,536 chunks
  total, 4,096 embedding dimensions, and 8,388,608 vector scalars. The planner does
  not read files, contact providers or stores, persist data, or change lifecycle.
  The separate candidate persistence service consumes that complete plan through
  injected store and signer/verifier boundaries. The internal lifecycle registry
  can mark fully verified candidates ready, switch an exact active pointer by CAS,
  roll back, and logically remove an inactive version through injected trust and
  store boundaries; it is not wired into startup or public APIs.

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

For the development live-corpus localhost path, provide an explicit corpus and the site
origin that will embed Cairn, then run one command from the repository root:

```sh
CAIRN_CORPUS_PATH=/absolute/path/to/docs \
ORIGIN_ALLOWLIST=http://localhost:4173 \
make live
```

The corpus directory must contain a non-empty Markdown or PDF file and a version 1
`provenance.json` covering every supported document. The manifest verifies the exact
mounted bytes and carries the reviewed public title and canonical HTTP(S) URL into
the existing citation response. Recursive `CORPUS_PATH` startup ingestion is limited
to development and test configurations. Production rejects every non-null
`CORPUS_PATH` before application construction work begins. See
[Startup corpus provenance](docs/CORPUS-PROVENANCE.md) for the strict schema,
hash workflow, symlink rejection, failure behavior, migration from local corpus
compatibility 1, and direct-ingestion fallback. `make live`
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
See [Production widget configuration](docs/WIDGET.md) for the exact six attributes,
event details, nonce policy, 0.2.0 compatibility check, clear behavior, and the
distinction between the packaged configuration boundary and deployment readiness.

### Container plumbing smoke test

`make up` requires Docker or Podman with Compose. The checked-in configuration uses the echo provider and fake embeddings, does not ingest the bundled corpus, and exposes the demo page but no `/admin` route. Use it to check container, health, static-page, and API plumbing. It does not prove a grounded or cited answer. Set `PROVIDER=ollama`, `EMBEDDING_PROVIDER=ollama`, and an Ollama URL reachable from the container only when intentionally testing that alternate configuration; ingestion is still not provided by `make up`. The grounded `make live` workflow remains Ollama-only.

### Optional Gemini generation profile

Gemini is an explicit generation-only option. A source install uses
`cd backend && uv sync --extra gemini`. An opt-in container build uses
`CAIRN_INSTALL_GEMINI=true docker compose build backend`; the checked-in default
is `false`, so the ordinary image does not contain `google-genai`. Never pass
`GEMINI_API_KEY` as a build argument. At runtime, select `PROVIDER=gemini`, set a
server-side `GEMINI_API_KEY`, and use real Ollama embeddings for production.

The fixed settings are `GEMINI_MODEL=gemini-3.8-flash`,
`GEMINI_TIMEOUT_SECONDS=30.0`, and `GEMINI_MAX_RETRIES=1`. Development and test
default to Echo plus fake embeddings. Production accepts only Ollama or Gemini
generation paired with Ollama embeddings; Gemini also requires its key and optional
runtime profile. The adapter and content-free model probe are mocked in tests. No
live Gemini call is part of validation, and `/readyz` does not probe Gemini.

| Mode | Generation | Embeddings | Result |
| --- | --- | --- | --- |
| development/test | missing, Echo, or Ollama | missing, fake, or Ollama | Accepted local configuration |
| development/test | Gemini | fake or Ollama | Requires nonblank `GEMINI_API_KEY` and the optional profile |
| production | Echo, missing, blank, or unknown | any | Rejected |
| production | Ollama or Gemini | fake, missing, blank, or unknown | Rejected |
| production | Ollama | Ollama | Accepted without Gemini inputs |
| production | Gemini | Ollama | Requires nonblank key and the optional profile |

### Optional Firestore retrieval profile

Local SQLite retrieval remains the default. For deterministic development and test
composition only, install `cd backend && uv sync --extra firestore` or build with
`CAIRN_INSTALL_FIRESTORE=true`. Set `RETRIEVAL_BACKEND=firestore` together with
all nine `FIRESTORE_*` values shown in `.env.example`. Production selection is
rejected until immutable corpus promotion and hosted readiness land.

| Setting | Required Firestore value |
| --- | --- |
| `FIRESTORE_PROJECT_ID` | 6-30 character lowercase Google project ID |
| `FIRESTORE_CORPUS_ID` / `FIRESTORE_CORPUS_VERSION` | one stable exact reference, never a moving alias |
| `FIRESTORE_EMBEDDING_IDENTITY` | 1-256 printable ASCII characters with no surrounding whitespace |
| `FIRESTORE_EMBEDDING_DIMENSIONS` | strict integer from 1 through 2,048 |
| `FIRESTORE_DISTANCE_MEASURE` | exact `cosine` or `euclidean` |
| `FIRESTORE_MAX_DISTANCE` | finite nonnegative number |
| `FIRESTORE_QUERY_TIMEOUT_SECONDS` | strict integer from 1 through 30 |
| `FIRESTORE_MAX_RETRIES` | strict integer `0` or `1` |

The database and collection are fixed to `(default)` and
`cairn_corpus_chunks_v1`. Queries require a neutral composite vector index over
`schema_version`, `corpus_id`, `corpus_version`, `embedding_identity`, and
`embedding`. Each attempt has the configured 1 through 30 second timeout; the
application makes zero or one retry after exactly 100 ms for unavailable,
deadline-exceeded, or aborted transport failures only. The adapter fetches K + 1
rows, sorts by `(distance, chunk_id)`, and rejects an unresolved distance tie at
the K boundary.

Authentication is Application Default Credentials at runtime. Cairn never accepts
credential JSON, credential paths, endpoints, or emulator addresses as Firestore
settings. `GOOGLE_SDK_PYTHON_LOGGING_SCOPE` must be blank. Focused tests inject a
fake transport, deny DNS, sockets, providers, ADC, and the production factory, and
make no live or credentialed call. `/readyz` intentionally does not probe
Firestore in this milestone.

Chat consumes one internal retrieval-route resolver per request. The default
resolver preserves `local_active`; development Firestore preserves its configured
exact reference. Explicit development/test composition may inject the read-only
lifecycle resolver, which validates one 45a active snapshot, single-flights full M8
verification for each new content-free pointer/policy fingerprint, and constructs an
M6 adapter bound to the same exact reference. The binding is checked against M6's
authoritative scope, embedding identity, and dimensions immediately before readiness
or retrieval I/O. Cairn ships no production lifecycle factory, trust loader, key, or
new environment setting, and `/readyz` remains unchanged.

### Immutable candidate persistence (development only)

`app.ingest.candidate_persistence` maps one validated plan to an immutable header,
document records, the existing Firestore chunk schema, and a final attestation.
Writes are create-only and header-first. Document and chunk batches are sorted and
bounded to 400 records and an exact 8 MiB encoded commit, while each encoded
document must fit 1 MiB. A complete independent readback uses 200-record pages with
a 201st-row lookahead before inventory hashing or signing.

Failure may leave an immutable header or content prefix. That prefix is not
attested, ready, active, or eligible for retrieval lifecycle promotion. Replaying
the same plan confirms exact records and creates only missing records; any changed
record fails closed and is never repaired, overwritten, or deleted. The service
uses the Firestore timeout and zero-or-one retry policy and resolves ambiguous
creates by read-confirm before retrying.

The module ships no signer, key loader, credential setting, KMS client, or trust
policy. Tests use an injected deterministic fixture that is not cryptography. A
separate signer-free verification service accepts an exact corpus, externally
selected signer identity, and verify-only verifier. It performs the same complete
readback, inventory, payload, envelope, and signature checks without signing or
writing. Its immutable, content-free evidence includes the exact corpus, plan and
semantic-manifest hashes, embedding identity and dimensions, record counts,
inventory and payload hashes, and signer algorithm and key IDs.
`app.corpus_lifecycle` supplies the strict lifecycle rules around a caller-owned,
immutable trust policy. It stores content-free evidence, exact state and immutable
audits in fixed-key transactions. Removal is logical and terminal, never a candidate
delete. The read-only route resolver can consume this seam only through explicit
development/test injection; default startup does not construct it. Cairn still ships no
production trust policy or lifecycle mutation surface. All persistence and lifecycle tests are offline and deny providers,
sockets, credentials, and production factories.

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

`POST /api/v1/chat/message` accepts exactly `{session_id, message, history?, context?}`.
The JSON body is capped at 16,384 actual UTF-8 bytes. Session IDs are 1-96 ASCII
letters, digits, `_`, or `-`; messages and per-turn content are non-blank and at
most 500 characters; history contains at most 5 turns and 2,000 total content
characters. Optional context accepts only `locale` and a non-protocol-relative
`page_path`, each at most 256 characters. Public context is metadata only and is
never included in provider instructions or retrieved context. Response is SSE:

| Event | Payload |
| --- | --- |
| `status` | `{state, label}` |
| `chunk` | `{delta}` |
| `citations` | `{sources: [{id, title, url}]}` |
| `error` | `{code, message, retryable}`; SSE 1.1 adds `invalid_request`, `budget_exhausted`, `concurrency_limited`, `provider_timeout`, `retrieval_unavailable`, and `request_cancelled` |
| `ping` | `{}` heartbeat every 15 s |
| `done` | `{finish_reason: stop\|refused\|limit\|cancelled}` |

Widget behavior on `error`: render message; reconnect once if `retryable`.
Provider adapters receive one frozen `ProviderGenerationRequest`; Ollama receives
the server-owned token cap through `num_predict`, while Gemini receives it through
the SDK generation configuration. Provider deltas are at most 1,000
characters, and the endpoint stops the provider at the configured total character
budget (default 6,000), ending with `done.limit` when truncated.

Ollama and Gemini normalize provider-reported token counts into internal usage
events. Cumulative provider reports are merged by taking the latest non-null,
non-decreasing field rather than summing repeated snapshots. Pure accounting
helpers price a usage event only against an operator-supplied immutable snapshot
identified by canonical JSON and SHA-256. Rates and totals use `Decimal`; unknown
usage is never treated as zero, mixed currencies and empty aggregates fail closed,
and projections are available only with complete priced coverage. No default
price catalog, persistence sink, budget enforcement, or public usage event exists.

Retrieval crosses a separate frozen internal contract (`retrieval_contracts.py`,
version `1.0`). Local chat requests `local_active` corpus compatibility 2 with
squared-L2 distance. The local adapter clamps `RETRIEVAL_TOP_K` to the store
count and the public maximum of 6, validates every returned SQLite row and its
provenance metadata without coercion, and fails closed on unsupported scope,
malformed output, or context larger than 12,000 characters. Exact immutable corpus
references remain unsupported by the local store. The optional development-only
Firestore adapter accepts one configured exact reference and either cosine or
Euclidean distance without changing the public chat or SSE contracts. Internally, chat
resolves one scope-plus-adapter route exactly once and retains that binding for the whole
request, so a later active-pointer change affects only the next request.
`RETRIEVAL_TOP_K` must be an integer from 1 through 6 and
`RETRIEVAL_MAX_DISTANCE` must be finite and non-negative; invalid settings stop
startup rather than changing retrieval behavior silently.

WISMO tool result includes `"mode": "deep_link" | "api"`. The system prompt forbids asserting delivery status when mode is `deep_link`; the model may only present the link.

## 5. Security Model

* **Prompt injection**: fixed prompt template with delimited sections; retrieved chunks wrapped in explicit untrusted-context markers; instruction hierarchy stated; adversarial suite (`backend/tests/adversarial/`) includes poisoned-document retrieval cases and must pass in CI.
* **Endpoint abuse**: Origin allowlist + CORS, per-IP and per-session token buckets, daily budget cap that degrades to a static "high demand" message, optional signed widget token.
* **Output**: PII scrubber on responses; citation-required policy; refusal template on low retrieval confidence.
* **Supply chain**: uv and npm lockfiles with hashes; separate default and optional-Gemini backend CycloneDX SBOMs; Grype gates for both image profiles; digest-pinned images; dependency cooldown window. Release tags ship SBOM + checksums.
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

* Python: `uv sync` in `backend/` for local-only development or `uv sync --extra gemini` for the optional adapter; `ruff`, `mypy`, `pytest` gate CI. Widget: `npm ci && npm run build` in `widget/` (esbuild, size budget check < 100 KB gz).
* Contract-first: change `contracts.py` only via PR that updates widget, tests, and docs together.
* No stubs/TODOs in merged code (CI grep gate). New dependencies require a one-line justification in the PR and land in the lockfile with hashes.
* Agent-assisted contributions follow `CLAUDE.md` / `AGENTS.md`: one component + its tests per task, frozen contracts, PR review with full diffs.

Focused provider-accounting checks run without live provider calls:

```sh
cd backend
uv run --extra gemini pytest -q \
  tests/test_provider_accounting.py tests/test_provider_conformance.py \
  tests/test_provider_gemini.py tests/test_provider_ollama.py \
  tests/test_provider_echo.py tests/test_chat_stream.py \
  tests/test_chat_endpoint.py tests/test_capabilities.py
uv run --extra gemini ruff check . ../eval
uv run --extra gemini mypy . ../eval/run_eval.py
```

## 8. Extending: Adding a Tool

1. Define a Pydantic call schema and result schema in `backend/app/tools/<name>/schema.py`.
2. Implement the executor (async, timeout-bounded, no network unless declared).
3. Register in the tool registry with `enabled: false` default.
4. Add unit tests, an integration test with a mocked executor, and at least one adversarial case.
5. Document the admin toggle.

No core changes required; the registry injects enabled tool schemas into the provider call.

## 9. Operations

* `GET /healthz` provides liveness. `GET /readyz` checks the database, local vector store, and corpus state; hosted-provider readiness remains planned. Neither response contains sensitive data.
* Structured JSON logs: latency, guardrail stage outcomes, error codes, query counts. No message bodies.
* Backup = copy the SQLite metadata database and
  `CHROMA_PATH/cairn-vectors-v1.sqlite3` (volume-mounted).

### Concurrency, one instance

FastAPI/uvicorn runs a single async event loop. The retrieval protocol is async and
yields once for cancellation, but the local adapter then performs embedding and its
flat-vector query synchronously on that loop. The query scans the bounded corpus in
O(N) time. Do not assume concurrent retrieval throughput. The client lock protects
the SQLite connection inside this one process;
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
