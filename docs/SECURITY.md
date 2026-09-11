# Security

Cairn is pre-1.0 and under active development. This document describes what's actually built and
tested today, not the target end-state — see [DEVELOPER_README.md](../DEVELOPER_README.md) §5 for
the full design and the sections below for work that is not yet implemented. A security doc that oversells
is worse than no security doc; treat the "Not yet built" section below as load-bearing, not
boilerplate.

## Reporting a vulnerability

Open a GitHub issue at [github.com/MichaelWeed/cairn-open-source-chat](https://github.com/MichaelWeed/cairn-open-source-chat)
or, for anything you'd rather not disclose publicly before a fix ships, use GitHub's private
vulnerability reporting on the repo. There is no dedicated security-contact email or bug bounty at
this stage.

## What's built and tested (Phase 1-2)

* **Origin allowlist + CORS.** `ORIGIN_ALLOWLIST` (env-configured) is checked against the request's
  `Origin` header before any chat request is processed; FastAPI's `CORSMiddleware` enforces the
  same list for browser preflight/response headers. Requests with no `Origin` header (non-browser
  clients) aren't blocked by this check — it's a browser-embedding control, not authentication.
* **Production widget boundary.** The packaged widget validates one atomic
  configuration, negotiates exact static capabilities before chat, omits
  credentials and referrers, bounds hostile capability/SSE responses, keeps host
  events content-free, reparses safe links, and supports strict per-response CSP
  nonces without `unsafe-inline`. See [WIDGET.md](WIDGET.md). This does not supply
  authentication, signed widget tokens, deployment-wide budgets, TLS, or provider
  readiness.
* **Rate limiting.** Per-IP and per-session token buckets (`app/ratelimit.py`), checked before any
  provider call. In-memory and single-process — see DEVELOPER_README.md §9 "Concurrency, one
  instance" for the exact scope of that (multiple replicas each enforce their own limit
  independently; this doesn't sum to a global cap).
* **Frozen wire contracts.** Every request/response/SSE-event shape is a Pydantic model with
  `extra="forbid"` (`backend/app/api/contracts.py`) — unexpected fields are rejected outright, not
  silently ignored. `message` is capped at 500 characters and `history` at 5 turns at the contract
  level, before any handler code runs.
* **Fail-closed provider accounting.** Internal usage events and price snapshots are
  frozen strict models with explicit size and numeric bounds. Usage contains no
  prompts, completions, retrieved content, session IDs, or client identifiers.
  Snapshot source URLs reject credentials, query strings, and fragments. Errors do
  not echo raw provider metadata, rates, or content, and this component has no
  authority to enforce budgets or persist records.
* **Structural boundary around retrieved content.** The pure retrieval-evidence
  compiler revalidates exact built-in snapshots, filters each chunk against the
  application request threshold, and serializes eligible `source` and text strings
  into one deterministic JSON object after a fixed untrusted-data instruction.
  Literal tag-shaped characters are escaped and offline adversarial tests prove
  exact parsing, sibling-field resistance, hook safety, content-free failures, and
  no external calls. This structural guarantee does not prove universal semantic
  prompt-injection resistance or model compliance.
* **No message bodies in logs.** Structured JSON logging (`app/logging_config.py`) is a thin
  formatter with no built-in field allowlist/denylist — the "never log message content" rule is
  enforced by code convention and review today, not by a runtime filter. A future contributor who
  passes chat or document content into a log call's `extra={}` won't be stopped by anything
  mechanical yet.
* **Optional Gemini boundary.** The SDK is absent from the default install and is
  imported only after explicit provider selection. `GEMINI_API_KEY` is a trimmed,
  server-side runtime secret and never a build argument. Production configuration
  fails closed unless generation is Ollama or Gemini and embeddings are Ollama.
  Retrieved support context is sent as untrusted JSON data, not as a system
  instruction, and Gemini errors and logs retain only normalized content-free fields.
* **Optional Firestore retrieval boundary.** The SDK is absent from the default
  install and imported only after explicit development/test selection. Configuration
  fixes the database, collection, schema, fields, exact corpus scope, bounds,
  timeout, and retry count. Authentication uses runtime Application Default
  Credentials; credentials and endpoints are not Cairn settings or build arguments.
  Nonblank `GOOGLE_SDK_PYTHON_LOGGING_SCOPE` is rejected, adapter errors are
  content-free, and deterministic tests deny ADC, DNS, sockets, providers, and the
  production factory. Production selection remains fail-closed.
* **Immutable candidate attestation boundary.** The development-only persistence
  component reserves an exact candidate with an immutable header, performs only
  existence-preconditioned creates, independently reads back every expected record,
  rejects extras or changes, and writes the attestation last. Batch count, encoded
  request bytes, document bytes, dimensions, pagination, timeouts, and retries are
  bounded. Errors are fixed and content-free, and SDK logging remains disabled.
  Exact encoded-size agreement is computed linearly and kept operation-local. A
  separate signer-free verifier accepts an exact corpus, externally selected
  identity, and verify-only verifier and returns only identity-bound, content-free
  evidence from a complete durable readback. The component ships no key, credential
  loader, KMS client, production algorithm, trust selection, or trust store.
* **Corpus lifecycle trust and transaction boundary.** The development-only registry
  accepts one immutable caller-owned trust policy, borrows M8's signer-free full
  verifier, and revalidates exact content-free evidence before ready registration or
  active changes. Ready state, active-pointer CAS, rollback, and inactive logical
  removal commit state plus one create-only audit in the same transaction. Store keys
  are domain-separated hashes, SDK retries are disabled, and ambiguous commits are
  read-confirmed before the sole optional service retry. No production trust policy,
  key loader, physical delete, repair, provider call, or application route is added.
* **Exact active retrieval routing boundary.** Chat awaits one route per request and
  keeps its exact scope and M6 adapter together. Lifecycle composition revalidates 45a
  state, single-flights an accepted M8 signer-free refresh on a cache miss, and accepts
  only an exact Firestore adapter whose authoritative private scope, embedding identity,
  and dimensions match the public binding. The same bridge runs before route return,
  readiness, and retrieval, preventing descriptor lies or post-resolution mutation from
  reaching embedding or vector I/O. This path is explicit development/test injection;
  it adds no trust loader, key lookup, production factory, or lifecycle mutation.
* **Supply chain.** `uv.lock`/`package-lock.json` with hashes; CycloneDX SBOM regenerated and
  diffed against the committed one on every `make validate` run; `osv-scanner` (lockfiles) and
  `grype` (built images) gates; container images pinned by digest; a 14-day dependency cooldown
  window. Any documented, narrowly-scoped exception includes a stated re-check condition. The
  retired ChromaDB exception was removed with that dependency, so the backend lockfile has no OSV
  vulnerability ignores; the six remaining image exceptions are limited to findings
  fixed only in pre-release Python and documented with re-check conditions in `.grype.yaml`.

## Not yet built — do not assume these exist

* **No authentication anywhere.** There is currently no admin surface at all (Phase 5), so there's
  nothing to log into and nothing an attacker could authenticate against yet. Once Phase 5 ships,
  the admin account (Argon2id, SameSite=Strict session cookie, CSRF token, optional TOTP) protects
  configuration surfaces only — end-user auth is an explicit non-goal; Cairn
  never gates answers by end-user identity.
* **No always-on guardrail middleware beyond the structural retrieval boundary above.** Tier 1 (input caps,
  injection heuristics, topic/PII input filters — task 4.1) and Tier 2 (Llama Guard 3 toggle — task
  4.2) don't exist yet.
* **No output PII scrubbing.** Responses are not currently filtered for PII before being streamed
  to the client (task 4.3).
* **No daily abuse budget cap or signed widget token** (task 4.5) — only the per-IP/session token
  buckets above.
* **No production candidate trust policy or activation route.** The internal lifecycle
  seam defines strict ready/active transitions, rollback, and logical removal around an
  injected immutable trust policy. Cairn does not ship a production policy, configure
  trusted signer identities, or expose lifecycle mutation through startup, HTTP, or UI.
  Its read-only resolver can be injected programmatically for development/tests, but no
  environment or production composition selects it.
* **No HTTP ingestion endpoint.** `ingest_upload()` (task 2.2) exists as a callable but nothing
  routes an HTTP request to it yet (task 2.7). Today, the only way content enters the vector store
  is a direct function call (tests, or an operator-run script) — there's no attacker-reachable
  upload surface.
* **No TLS termination.** The backend serves plain HTTP; operators must put a TLS-terminating
  reverse proxy or load balancer in front of it for any non-local deployment.

## Operator recommendations (today)

* Terminate TLS in front of the backend; don't expose the published port (`CAIRN_PORT`, default
  8080) directly to the internet.
* Keep `ORIGIN_ALLOWLIST` scoped to the pages that actually embed the widget. Left empty, it
  allows only the app's own origin (`http://localhost:$CAIRN_PORT`).
* Run `make verify` at install time and on the weekly cadence documented in DEVELOPER_README.md §4
  — nothing else will alert you to CVEs disclosed after you installed.
* Keep the local Ollama default unless you intentionally accept the Gemini data
  boundary. Selecting Gemini sends bounded chat inputs and retrieved support
  context to Google; see [docs/PRIVACY.md](PRIVACY.md). `/readyz` performs only the
  selected provider's bounded content-free model probe. It never generates content
  or exposes model/endpoint details, and development-only hosted readiness is not
  production certification.
