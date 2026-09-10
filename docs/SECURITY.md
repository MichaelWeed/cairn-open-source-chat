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
* **Prompt-injection defense around retrieved content.** The retrieval pipeline (`app/retrieval.py`,
  task 2.4) wraps retrieved document chunks in an explicit `<retrieved-context>` block with a system
  prompt instructing the model to treat that block as untrusted data, not instructions, and to
  ignore any commands embedded inside it. **This is the prompt-level mitigation only.** The
  adversarial test suite that actually proves it holds under attack (poisoned-document retrieval
  cases) is task 4.4, not built yet — treat this defense as unverified against a determined attacker
  until that suite exists and passes.
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
* **No always-on guardrail middleware beyond the prompt-level defense above.** Tier 1 (input caps,
  injection heuristics, topic/PII input filters — task 4.1) and Tier 2 (Llama Guard 3 toggle — task
  4.2) don't exist yet.
* **No output PII scrubbing.** Responses are not currently filtered for PII before being streamed
  to the client (task 4.3).
* **No daily abuse budget cap or signed widget token** (task 4.5) — only the per-IP/session token
  buckets above.
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
  context to Google; see [docs/PRIVACY.md](PRIVACY.md). The provider-local probe is
  not part of `/readyz`, so optional adapter availability is not production readiness.
