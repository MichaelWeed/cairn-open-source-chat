# Compatibility and capability discovery

Cairn publishes static, validated package metadata at `GET /api/v1/capabilities`.
The response is identical to `backend/app/capabilities.json`; it does not inspect
the environment, test credentials, probe providers, or expose corpus content and
paths. Readiness remains available separately at `GET /readyz`.

The current developer-preview values are recorded below. This JSON block is checked
against the packaged manifest in the backend test suite so documentation drift fails
validation.

<!-- capabilities-manifest:start -->
```json
{
  "schema_version": "1.1",
  "release": {
    "stage": "developer-preview",
    "version": "0.0.0"
  },
  "compatibility": {
    "chat_api": "1.0",
    "sse_events": "1.1",
    "widget": "0.2.0",
    "local_retrieval_store": "1",
    "local_corpus": "2",
    "provider_accounting": "1.0"
  },
  "capabilities": {
    "providers": {
      "ollama": "available",
      "echo": "development_only",
      "gemini": "available"
    },
    "embeddings": {
      "ollama": "available",
      "fake": "development_only",
      "hosted": "planned"
    },
    "retrieval": {
      "local_sqlite_flat": "available",
      "hosted_durable": "development_only"
    },
    "corpus": {
      "local_directory": "development_only",
      "reviewed_manifest": "available",
      "immutable_versions": "development_only"
    },
    "widget": {
      "custom_element": "available",
      "production_configuration": "available"
    },
    "safety": {
      "origin_allowlist": "available",
      "single_instance_rate_limits": "available",
      "deployment_wide_controls": "planned"
    },
    "operations": {
      "liveness": "available",
      "local_readiness": "available",
      "capability_discovery": "available",
      "hosted_readiness": "development_only",
      "provider_usage_cost": "available"
    }
  }
}
```
<!-- capabilities-manifest:end -->

## Local corpus compatibility 2

Local corpus compatibility 2 requires every `CORPUS_PATH` directory to include a
version 1 `provenance.json` that covers each mounted Markdown and PDF document.
Version 1 accepted document directories without this manifest. Before upgrading,
operators must add the reviewed public title, canonical HTTP(S) URL, exact-byte
SHA-256, owner, review date, and `public: true` attestation described in
[CORPUS-PROVENANCE.md](CORPUS-PROVENANCE.md). Symlinked documents and manifests are
rejected; mount regular files read-only.

Recursive mounted-directory startup ingestion is available only in development and
test configurations. Production rejects every non-null `CORPUS_PATH` before
application construction work begins. The reviewed manifest parser and deterministic
candidate planner remain usable at their documented offline boundaries.

## Upgrade rules before 1.0

- A patch release repairs documentation or implementation without changing a
  reported contract.
- A minor release may add a capability, field, provider, or adapter that existing
  consumers can ignore.
- A major release is required for removal, semantic reinterpretation, or an
  incompatible request, event, or schema change.
- Pre-1.0 compatibility is explicit: a breaking change updates the affected
  compatibility value and includes migration notes. Moving aliases are not versions.

Capability state is descriptive, not a runtime health signal. `available` means the
feature is built for its documented path, `development_only` identifies deterministic
development/test implementations, and `planned` means callers must not depend on it.
Gemini availability requires the optional dependency or image profile, an explicit
provider selection, and a server-side key. `operations.hosted_readiness` is
`development_only`: `/readyz` evaluates the selected provider/model, embedding,
and selected local or hosted retrieval route through bounded content-free probes.
This does not make hosted retrieval production-selectable. `hosted_durable` is
`development_only`: the optional Firestore adapter can perform bounded exact-scope
reads in development and tests. Chat consumes one internal scope-plus-adapter route
per request. Explicit development/test injection may bind validated active lifecycle
state to a separately exact-bound M6 adapter; no production trust/factory or lifecycle
mutation surface is supplied. The chat and SSE compatibility versions are unchanged.

Widget compatibility 0.2.0 adds the fail-closed configuration, capability probe,
privacy, event, CSP, and accessibility contract documented in [WIDGET.md](WIDGET.md).
It also excludes refusals from later browser history and tightens URLs, names,
themes, and nonces as described in that guide's migration section.
`widget.production_configuration = "available"` identifies only the packaged
widget configuration boundary. It is not deployment certification, provider or
hosted readiness, or deployment-wide abuse control.

Provider accounting compatibility 1.0 adds an internal discriminated provider
event stream and pure cost helpers. Usage records identify their provider, model,
attempt, and optional service tier; token fields remain nullable when a provider
does not report them. Cost calculation requires an operator-supplied immutable
price snapshot with a canonical SHA-256 identity. Cairn does not ship a live price
catalog, persist accounting records, enforce budgets, or expose usage over SSE.
The request-scoped handoff composes those existing 1.0 records without changing any
public compatibility version. Built-in application-owned providers have exact attempt
limits of Echo 0, Ollama 1, and Gemini 1 or 2; caller-injected providers receive no
authoritative accounting observer.

## Internal retrieval contract 1.0

The provider-independent retrieval request and result models are versioned `1.0`.
The local adapter accepts only `local_active` with local corpus compatibility 2 and
squared-L2 distance. Exact corpus ID and version references remain unsupported by
the local store. The optional Firestore adapter accepts only one configured exact
reference in development/test. The route resolver keeps the exact reference and its M6
adapter together for one request and rechecks M6 binding authority before I/O; a pointer
change is visible to the next request only. This does not alter the public request or
event shape. This protocol layer
requires no data migration for local retrieval store schema 1 or local corpus
compatibility 2. It does not rename or reinterpret the existing SQLite index, ingestion
metadata, public chat request, SSE events, or capability manifest.

Retrieval settings fail fast: `RETRIEVAL_TOP_K` is an integer from 1 through 6 and
`RETRIEVAL_MAX_DISTANCE` is finite and non-negative. Store output, chunk text,
identifiers, provenance URLs, result count, and assembled context are bounded.
Malformed or oversized results take the existing content-free refusal path without
partial citations or a provider call.

The internal retrieval-evidence compiler enforces the existing threshold per chunk,
preserving adapter order and threshold equality. It serializes the exact eligible
support as deterministic JSON and derives every citation from the same tuple in
first-seen document order. Reviewed title/HTTP(S) URL pairs remain exact; direct
ingestion retains the title-truncated-to-160 and `document://<document_id>` fallback.
The public chat API remains `1.0`, SSE remains `1.1`, and no capability, retrieval,
corpus, or store compatibility value changes.

## Internal ingestion-plan contract 1.0

The pure ingestion planner consumes in-memory version 1 manifest bytes, exact
same-read document bytes, an exact corpus reference, and an injected offline
embedding function. Its `1.0` algorithm identities and bounded canonical SHA-256
material make a complete candidate plan deterministic across retries and process
hash seeds. Planning does not persist or activate a candidate.

The internal candidate persistence contract adds create-or-confirm storage, exact
readback, canonical inventory hashing, and injected attestation verification. Its
separate signer-free verifier accepts an exact corpus, externally selected identity,
and verify-only verifier and returns immutable content-free evidence. Therefore
`immutable_versions` is `development_only`. The internal lifecycle contract can copy
verified evidence into ready state, apply exact active-pointer promotion or rollback,
and logically remove an inactive version with immutable audits. It accepts an injected
immutable trust policy but ships no real signer or production trust selection and is
not selected by default startup or exposed through an endpoint. Its read-only active
seam can be composed explicitly with the internal route resolver for development/tests.
That resolver's exact-version probe can be aggregated by `/readyz` when the resolver
is explicitly injected in development/test. The public response retains only the
existing database, vector-store, and corpus booleans; no scope, version, model,
reason, or endpoint is exposed. Production lifecycle injection fails readiness as
misconfigured.

The public chat API remains `1.0`, SSE remains `1.1`, local corpus remains `2`, and
local retrieval store remains `1`. Existing deployments require no migration or
reindex for this internal addition.

## Chat compatibility 1.0 and SSE compatibility 1.1

The public chat request keeps the same four keys and does not require a version
field. KAN-46a adds validation bounds without changing successful envelopes. SSE
1.1 adds error codes for invalid requests, budgets, concurrency, timeouts,
retrieval, and cancellation, plus `limit` and `cancelled` done reasons. Existing
event names and payload fields remain compatible.
