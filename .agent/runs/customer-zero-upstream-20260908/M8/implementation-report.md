# M8 Implementation Report: KAN-49b Candidate Persistence and Attestation

## Provenance

- Work ID: `customer-zero-upstream-20260908:M8:BUILD:1`
- Repair work ID: `customer-zero-upstream-20260908:M8:REPAIR:1`
- Branch: `mara/KAN-49-candidate-attestation`
- Base: `7e09b23e82ca3ae302efaab9e5a5cbf6cf326c82`
- Accepted ancestors: M5 `2b0d0d5`, M6 `e59f2a9`, M7 `35b3677`
- Rejected implementation commit: `f4eeb4087f54f99f74b542eccb29f7b48476b03d`
- Final repair commit: the follow-up repair commit containing this report; its exact
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

Focused evidence:

- M8 persistence and Firestore component tests: 73 passed, exit 0.
- Firestore-profile focused matrix: 310 passed, one upstream warning, exit 0.
- Combined Firestore/Gemini focused matrix: 478 passed, one upstream warning,
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
- Final `make validate`: exit 0; 893 backend tests passed plus all widget protocol,
  history, layout, browser, type, build, size, distribution, supply-chain, and image
  gates. One upstream Starlette deprecation warning was reported.

The first repair full-validation attempt stopped at repository-wide mypy with 39
errors in the expanded adversarial test file because the focused type-check commands
covered application sources but omitted the changed test. This recurrence of
`BUG-20260910-0850` was recorded with exact evidence. Test annotations were repaired
without exclusions; repository-wide Ruff and mypy then passed, and the final full
validation above is the post-repair result.

The image scanner reported only the repository-accepted low-severity Python
`CVE-2026-15310`, whose listed fix is the prerelease `3.15.0rc2`; the gate exited 0.

## Scope Confirmation

No dependency or lockfile changed. No pre-M8 public API contract changed, and the
repair preserves M8's original public store signatures. No files outside the
milestone allowlist changed in this candidate branch. The primary
worktree's untracked `bugs/` was preserved except for the explicitly requested
append-only `BUG-20260910-0850` recurrence record; `/private/tmp/cairn-KAN-71`, all
other worktrees, and VoiceVerdict were untouched.
This build lane did not push, merge, deploy, or mutate any external system.
