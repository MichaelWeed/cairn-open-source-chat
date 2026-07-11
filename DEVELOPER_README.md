# Cairn Developer Guide

Technical documentation for deploying, operating, and extending Cairn (formerly AetherChat; see [MASTER_PLAN.md](MASTER_PLAN.md) §2 for the naming record). Executive overview: [README.md](README.md). Full design: [docs/SOLUTION_DESIGN.md](docs/SOLUTION_DESIGN.md) and [MASTER_PLAN.md](MASTER_PLAN.md).

---

## 1. Architecture at a Glance

```
[Widget (shadow DOM, vanilla TS)]
        | POST /api/v1/chat/message  (JSON in, SSE out)
        v
[FastAPI backend]
   Guardrail pipeline (ordered middleware, toggleable)
   -> Router: RAG answer | tool call | refusal
   -> Provider adapter (Ollama default | OpenAI-compatible hosted)
        |
   [ChromaDB embedded]  vectors
   [SQLite, WAL]        docs metadata, provider registry, config, metric counters
        |
   [Tool registry]  lookup_order_status | escalate_to_human
```

Design invariants:

* **Server holds no conversation state.** The widget carries history (last 5 turns, sessionStorage) in each request.
* **Widget endpoints are anonymous; admin endpoints are authenticated.** Single admin account, Argon2id, SameSite=Strict session cookie, CSRF token, optional TOTP.
* **Contracts are frozen Pydantic models** (`extra="forbid"`). The SSE contract and tool schemas in `backend/app/api/contracts.py` are the source of truth.

## 2. Quick Start (Local)

Prerequisites: Docker or Podman, with Compose (`docker compose` or `podman compose`/`podman-compose`); for the local model path, Ollama with an 8B-class instruct model pulled.

```
git clone <repo> && cd cairn
cp .env.example .env            # set OLLAMA_BASE_URL, ADMIN_BOOTSTRAP_PASSWORD
make up                         # digest-pinned images; uses whichever engine is installed
# open http://localhost:8080/admin  -> ingest the bundled sample corpus
# open http://localhost:8080/demo   -> widget test page
```

`compose.yaml` stays within the vendor-neutral Compose Specification (no Docker-specific extensions), so it runs unmodified under either engine. `make up`/`make down` detect the available `COMPOSE_CMD`; set it explicitly (`COMPOSE_CMD=podman compose make up`) if both are installed and you want a specific one.

Embed on any page:

```html
<script src="https://your-host/widget.js"
        data-endpoint="https://your-host/api/v1"
        data-title="Support"></script>
```

## 3. Configuration Surfaces

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
make eval          # runs against the currently ingested corpus
```

Produces `eval/reports/<date>.md` with: groundedness (LLM-judged support of answers by retrieved chunks), citation precision/recall, correct-refusal rate on unanswerable questions, TTFT and P95 latency, adversarial pass/fail. The committed report for the sample corpus backs every number in the executive README. Question sets are YAML in `eval/questions/`; write one for your corpus before trusting production traffic.

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
* Backup = copy the SQLite file and the Chroma persistence directory (volume-mounted).
* Scaling: single instance is the design point; replicas behind a load balancer are possible since the server is stateless, with the vector store mounted read-only on replicas and writes routed to the ingest instance.

## 10. Roadmap and Non-Goals

See [MASTER_PLAN.md](MASTER_PLAN.md) §7. Out-of-scope feature requests (CRM sync, auth-aware answers, multi-tenant, voice) are closed with a pointer to the consulting page by policy; see [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md).
