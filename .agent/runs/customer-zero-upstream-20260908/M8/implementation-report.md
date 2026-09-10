# M8 Implementation Report: KAN-49b Candidate Persistence and Attestation

## Provenance

- Work ID: `customer-zero-upstream-20260908:M8:BUILD:1`
- Repair work ID: `customer-zero-upstream-20260908:M8:REPAIR:1`
- Branch: `mara/KAN-49-candidate-attestation`
- Base: `7e09b23e82ca3ae302efaab9e5a5cbf6cf326c82`
- Accepted ancestors: M5 `2b0d0d5`, M6 `e59f2a9`, M7 `35b3677`
- Rejected implementation commit: `f4eeb4087f54f99f74b542eccb29f7b48476b03d`
- Rejected first repair commit: `8a0445b4fdbc437ad11e1578d40f7ec9a3dd2ec3`
- Rejected second repair commit: `969e85c005b69c83aa6a44a9cb04a5e793cc2211`
- Rejected privacy repair commit: `cf25bb1a120bd07d3b429f53e8fdc3944f78f05f`
- Rejected public-model repair commit: `c58efec3601efcc2d7f729d505115b36844b757d`
- Final model-lifecycle repair commit: the follow-up repair commit containing this report; its exact
  object ID is returned to the control lane after handoff.

The accepted M5 source snapshot, M6 bounded chunk-key and embedding contracts, and
M7 candidate-plan contract matched the preflight assumptions. No material contract
drift required re-preflight.

## Delivered Boundary

`backend/app/ingest/candidate_persistence.py` adds the frozen, strict persistence
models, fixed content-free failures, deterministic record mappings, candidate key,
public canonical-JSON bytes helper, framed inventory digest, attestation
payload/envelope, injected store and signer/verifier protocols, the
create-or-confirm service, and a separate signer-free verification service. The
persistence service reserves
the immutable header first, writes sorted document and chunk records with bounded
batches, independently reads back the complete exact scope, recomputes M7 content
and embedding hashes, verifies every expected record and the signature, and creates
the attestation last.

Replay is create-or-confirm only. Conflicts are read-confirmed, ambiguous transient
creates are read-confirmed before a bounded retry, and changed or extra records fail
closed. Cancellation is preserved. Owned cleanup is idempotent. No update, delete,
promotion, ready-state, active-pointer, repair, or partial-success contract is
introduced.

`CandidateAttestationVerificationService.verify_attested_candidate` accepts an
exact corpus, a strict expected `AttestationIdentity`, and an externally selected
verify-only verifier. It constructs no signer and performs no signing or writes.
It independently rereads the durable header, exact bounded document and chunk
scope, and attestation; validates every record field and relationship; reuses the
same canonical inventory, payload, and envelope rules; and verifies identity both
before store access and after the verifier call. It returns a strict immutable
`VerifiedCandidateEvidence` containing the corpus, plan and semantic-manifest
hashes, embedding identity and dimensions, record counts, inventory and payload
hashes, and signer algorithm and key IDs. It contains no document or chunk content.
M9 remains responsible for trusted identity selection and lifecycle CAS.

`backend/app/ingest/candidate_firestore.py` adds the optional Firestore adapter and
construction-only factory. It uses create preconditions, exact protobuf document and
commit sizing, fixed timeouts, no SDK retries, exact corpus filters, name ordering,
cursor pagination, and a 201-row lookahead for 200-record pages. Only the known
chunk vector is converted to the SDK vector type. SDK failures normalize to the
fixed store failure vocabulary. The public `CandidateStore` protocol and
`CandidateStoreFailure` vocabulary introduced by the M8 boundary retain their
frozen signatures; a private checked-store extension carries exact operation-local
encoded sizes and per-write byte digests for adapters that support linear
agreement verification.

The fixed collections are:

- `cairn_corpus_candidates_v1`
- `cairn_corpus_documents_v1`
- `cairn_corpus_chunks_v1`
- `cairn_corpus_attestations_v1`

Batches contain at most 400 creates and at most 8 MiB of exactly encoded commit
material; each encoded document is bounded to 1 MiB. Each record is encoded once
during linear planning, its exact create contribution is accumulated, and the
final commit is re-encoded once and required to match the operation-local expected
size and per-write SHA-256 digests. Missing-only retries reuse the original bounded
agreement instead of replanning. No content-derived encoder cache is retained.
Candidate plans retain the M7
`MAX_TOTAL_CHUNKS=65,536` bound and existing document, chunk, vector-scalar, and
text bounds; the Firestore embedding limit is at most 2,048 dimensions. Tests
exercise a 401-record split into 400 plus 1 with exactly 401 planning encodes, a
compact 65,536-record maximum with exactly 65,536 planning encodes, exact encoded
size rejection and drift, the 1 MiB document limit, 2,048 embedding dimensions,
and the complete 0/1/199/200/201/65,536 pagination matrix.

The repair also makes malformed SDK response shapes and encoder disagreement map
to fixed `malformed_store`, rejects false lookahead cursors without accepting an
extra terminal empty page, and revalidates or reconstructs every nested public M8
model across constructors, copies, replacement, adapters, and JSON validation.
Hostile bypass-mutated nested models cannot cross an outer public boundary.
Durable verification reconstructs the actual M7 `PlannedChunk`, `PlannedDocument`,
and `CandidateIngestionPlan` models, so their validators independently recompute
document IDs, chunk IDs, document plan hashes, the semantic-manifest hash, and the
plan hash. A forged path/source candidate remains rejected even if an attacker
re-signs its internally self-consistent M8 envelope.

The second repair requires durable provenance dates to parse and round-trip to the
exact canonical `YYYY-MM-DD` representation before M7 reconstruction. Basic and ISO
week-date spellings cannot be normalized into accepted lifecycle evidence, even
after a valid re-sign. Firestore snapshot existence is likewise strict: only exact
`bool` values are interpreted, literal `False` is absence, literal `True` is
presence, and every other completed SDK value is fixed `malformed_store` before
retry, signing, verification, or write work.

The privacy repair ensures parsing and validation failures cross the public M8
boundary only as the fixed `CandidatePersistenceError` taxonomy with no reachable
raw exception. It clears cause, context, and prior traceback retention after the
caught frame has exited, and applies the same content-free conversion to canonical
JSON encoding, strict model construction/copy/revalidation, candidate corpus keys,
durable header/page/record/date/URL/path reconstruction, store encoding, retry
delay, and create classification. Public persistence and signer-free verification
entry points provide a final cancellation-preserving sanitization boundary. Direct
recursive probes inspect the complete reachable exception graph, arguments,
instance state, structured errors, JSON, string, and representation for raw record,
provenance, hash, key, and canary retention.

The public-model repair seals Pydantic's pre-schema JSON parser as well as the
existing model schema. Each M8 model installs the same content-free validator
facade during Pydantic subclass initialization, so constructors and direct
`model_validate`, `model_validate_json`, and `model_validate_strings` calls share
the exact validator used by `TypeAdapter` for its Python, JSON, and strings routes.
Malformed JSON syntax is therefore normalized before either public caller receives
Pydantic's input-retaining `ValidationError`; structurally invalid inputs and
hostile `extra` overrides remain fixed `invalid_plan`. Valid direct-model and
`TypeAdapter` JSON round trips remain exact for all eleven public models.

The model-lifecycle repair keeps that facade effective across Pydantic schema
rebuilds. A model-local `model_rebuild` override serializes rebuild calls, lets
Pydantic prepare its replacement schema, and reinstalls the facade before releasing
the lock or returning. It does not patch Pydantic `BaseModel` or M7 models globally.
The override preserves `None` for a default completed-model no-op and `True` for
forced rebuilds. Repeated and concurrent serialized rebuild calls, concurrent
validation after return, adapters created before and after rebuild, derived
subclasses, valid Python/JSON round trips, and model/adapter JSON schema generation
remain deterministic and content-free. Validation concurrent with an in-progress
rebuild is not claimed because Pydantic explicitly documents that lifecycle as not
thread-safe.

## Public Surface and Privacy

The capability manifest now reports `corpus.immutable_versions` as
`development_only`. Public chat API, SSE, provenance manifest, local corpus, and
local retrieval-store versions are unchanged. The component is not wired to
startup or HTTP routes and ships no signer, signing key, trust store, credential
loader, KMS client, production algorithm, lifecycle policy, or new dependency.

Errors, receipts, and verified evidence are content-free. The implementation adds
no logging. Persistence and Firestore instances retain no candidate-derived sizing
cache; sizing agreement exists only in bounded operation-local batch objects whose
representation contains count and size only. Tests directly probe internal state,
representations, logs, exception chains, and cleanup. They deny live providers,
sockets, credentials, production factories, and external I/O. No emulator,
deployment, promotion, credential, key, or live Firestore/provider operation was
run.

## Changed Files

- `backend/app/ingest/candidate_persistence.py`
- `backend/app/ingest/candidate_firestore.py`
- `backend/tests/test_candidate_persistence.py`
- `backend/tests/test_candidate_firestore.py`
- `backend/app/capabilities.json`
- `backend/tests/test_capabilities.py`
- `DEVELOPER_README.md`
- `docs/ARCHITECTURE.md`
- `docs/COMPATIBILITY.md`
- `docs/PRIVACY.md`
- `docs/SECURITY.md`
- `.agent/runs/customer-zero-upstream-20260908/M8/implementation-report.md`

## Verification Evidence

Red-first evidence:

- `uv run --locked --extra firestore pytest -q tests/test_candidate_persistence.py`
  exited 2 with the expected missing-module collection failure before the source
  module existed.
- Three initial repair adversarial tests failed against the rejected implementation
  for content-retaining caches, false terminal pagination, and nested-model bypass;
  all passed after the repairs.
- The signer exception-chain canary probe failed before exception handling moved
  outside the caught context and passed after the content-free repair.
- The second-repair hostile matrix initially produced 9 failures and 3 passes:
  three accepted noncanonical reviewed-date representations, five non-boolean
  snapshot-existence values, and the service-level false-absence path failed as
  expected; canonical-date, invalid-date, and literal-false controls passed. The
  same matrix passed 12 tests after the two narrow source edits.
- The privacy-repair slice initially produced 10 failures and 1 pass. Raw parser,
  Pydantic, M7 reconstruction, URL, corpus, receipt, and Unicode encoder exceptions
  were reachable through `__context__`. After moving error construction outside
  caught frames and sealing both public service boundaries, all 11 direct recursive
  exception-graph probes passed.
- The public-model validation matrix initially produced 22 malformed-JSON failures
  and 66 passes across all eleven public models and eight direct-model/`TypeAdapter`
  Python, JSON, and strings routes. Both malformed-JSON routes leaked raw Pydantic
  errors for every model. After the class-wide validator-boundary repair, all 88
  route/model combinations passed with recursive retention checks and valid JSON
  round trips.
- The model-lifecycle slice initially failed all 22 all-model rebuild and concurrent
  rebuild/validation cases because a forced rebuild replaced each facade with a raw
  `SchemaValidator`. After preserving the facade as the model-local lifecycle seam,
  all 22 all-model cases plus the derived-subclass rebuild probe passed.

Focused evidence:

- M8 candidate persistence component tests after model-lifecycle repair: 188 passed,
  exit 0.
- M8 persistence plus Firestore component tests: 203 passed, exit 0.
- Firestore-profile focused matrix: 440 passed, one upstream warning, exit 0.
- Combined Firestore/Gemini focused matrix: 608 passed, one upstream warning,
  exit 0.
- Linear-count probes: 401 records required exactly 401 record encodes and split
  into 400 plus 1; 65,536 records required exactly 65,536 record encodes and split
  into 164 bounded batches.
- Pagination probes passed for 0, 1, 199, 200, 201, and 65,536 records with bounded
  query counts and strict cursor behavior.
- Final Firestore-profile Ruff: exit 0.
- Final Firestore-profile mypy: 35 application source files, exit 0.
- Final combined-profile Ruff: exit 0.
- Final combined-profile mypy: 35 application source files, exit 0.
- Final repository-wide Ruff: exit 0.
- Final repository-wide mypy: 72 application and test source files, exit 0.
- Final locked resolution checks: exit 0; no dependency or lockfile changed.

Final mandatory repository evidence:

- `make lockfile-audit`: exit 0; 58 backend and 191 widget packages.
- `make cooldown-check`: exit 0; 81 packages.
- `make sbom-check`: exit 0 across all profiles with default restored.
- `make digest-pin-lint`: exit 0.
- `make image-scan`: exit 0.
- `make gemini-image-check`: exit 0.
- `make firestore-image-check`: exit 0, including default, Firestore, Gemini, and
  combined profiles with default restoration.
- Final `make validate`: exit 0; 1,023 backend tests passed plus all widget protocol,
  history, layout, browser, type, build, size, distribution, supply-chain, and image
  gates. One upstream Starlette deprecation warning was reported.

The first repair full-validation attempt stopped at repository-wide mypy with 39
errors in the expanded adversarial test file because the focused type-check commands
covered application sources but omitted the changed test. This recurrence of
`BUG-20260910-0850` was recorded with exact evidence. Test annotations were repaired
without exclusions; repository-wide Ruff and mypy then passed, and the final full
validation above is the post-repair result.

Independent review process incident `BUG-20260910-1311` records that an acceptance
lane's `uv run` synchronized cached packages despite a no-install instruction. This
second repair used the existing `.venv/bin` executables for tests, Ruff, and mypy;
only the read-only `uv lock --check` command used uv, and it did not synchronize or
install packages. The subsequently required `make validate` gate performed only its
repository-configured reproducibility syncs and container builds; no dependency was
added, upgraded, or written to a lockfile. The bug record and index remain
primary-worktree review evidence, not candidate files.

The image scanner reported only the repository-accepted low-severity Python
`CVE-2026-15310`, whose listed fix is the prerelease `3.15.0rc2`; the gate exited 0.

## Scope Confirmation

No dependency or lockfile changed. No pre-M8 public API contract changed, and the
repair preserves M8's original public store signatures. No files outside the
milestone allowlist changed in this candidate branch. The primary
worktree's untracked `bugs/` was preserved as process evidence and is not included
in this candidate; `/private/tmp/cairn-KAN-71`, all
other worktrees, and VoiceVerdict were untouched.
This build lane did not push, merge, deploy, or mutate any external system.
