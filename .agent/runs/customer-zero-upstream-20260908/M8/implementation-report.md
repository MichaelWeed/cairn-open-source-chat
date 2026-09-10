# M8 Implementation Report: KAN-49b Candidate Persistence and Attestation

## Provenance

- Work ID: `customer-zero-upstream-20260908:M8:BUILD:1`
- Branch: `mara/KAN-49-candidate-attestation`
- Base: `7e09b23e82ca3ae302efaab9e5a5cbf6cf326c82`
- Accepted ancestors: M5 `2b0d0d5`, M6 `e59f2a9`, M7 `35b3677`
- Final commit: the single implementation commit containing this report; its exact
  object ID is recorded by the control lane after handoff.

The accepted M5 source snapshot, M6 bounded chunk-key and embedding contracts, and
M7 candidate-plan contract matched the preflight assumptions. No material contract
drift required re-preflight.

## Delivered Boundary

`backend/app/ingest/candidate_persistence.py` adds the frozen, strict persistence
models, fixed content-free failures, deterministic record mappings, candidate key,
public canonical-JSON bytes helper, framed inventory digest, attestation
payload/envelope, injected store and
signer/verifier protocols, and the create-or-confirm service. The service reserves
the immutable header first, writes sorted document and chunk records with bounded
batches, independently reads back the complete exact scope, recomputes M7 content
and embedding hashes, verifies every expected record and the signature, and creates
the attestation last.

Replay is create-or-confirm only. Conflicts are read-confirmed, ambiguous transient
creates are read-confirmed before a bounded retry, and changed or extra records fail
closed. Cancellation is preserved. Owned cleanup is idempotent. No update, delete,
promotion, ready-state, active-pointer, repair, or partial-success contract is
introduced.

The service also exposes a read-only complete-candidate verification seam accepting
an externally selected verifier. It performs no signing or writes and returns only
the existing immutable content-free receipt evidence. This supplies the clarified
M9 trust-policy handoff without treating the M8 signer instance as a trust root or
duplicating inventory, payload, or readback rules.

`backend/app/ingest/candidate_firestore.py` adds the optional Firestore adapter and
construction-only factory. It uses create preconditions, exact protobuf document and
commit sizing, fixed timeouts, no SDK retries, exact corpus filters, name ordering,
cursor pagination, and a 201-row lookahead for 200-record pages. Only the known
chunk vector is converted to the SDK vector type. SDK failures normalize to the
fixed store failure vocabulary.

The fixed collections are:

- `cairn_corpus_candidates_v1`
- `cairn_corpus_documents_v1`
- `cairn_corpus_chunks_v1`
- `cairn_corpus_attestations_v1`

Batches contain at most 400 creates and at most 8 MiB of exactly encoded commit
material; each encoded document is bounded to 1 MiB. Candidate plans retain the M7
2,048-record maximum and existing document, chunk, vector-scalar, text, and
dimension bounds. Tests exercise a 401-record split into 400 plus 1, exact encoded
size rejection and encoder drift, the 1 MiB document limit, exact 2,048-record
acceptance, and 201-row pagination lookahead.

## Public Surface and Privacy

The capability manifest now reports `corpus.immutable_versions` as
`development_only`. Public chat API, SSE, provenance manifest, local corpus, and
local retrieval-store versions are unchanged. The component is not wired to
startup or HTTP routes and ships no signer, signing key, trust store, credential
loader, KMS client, production algorithm, lifecycle policy, or new dependency.

Errors and receipts are content-free. The implementation adds no logging. Tests
deny live providers, sockets, credentials, production factories, and external I/O.
No emulator, deployment, promotion, credential, key, or live Firestore/provider
operation was run.

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

Focused evidence:

- Final affected canonical-helper persistence/capability tests: 22 passed, exit 0.
- Candidate persistence, Firestore, and capability tests before the final helper
  clarification: 26 passed, exit 0.
- Firestore-profile focused matrix: 254 passed, one upstream warning, exit 0.
- Combined Firestore/Gemini focused matrix: 422 passed, one upstream warning,
  exit 0.
- Final Firestore-profile Ruff: exit 0.
- Final Firestore-profile mypy: 35 source files, exit 0.
- Final locked resolution check: 58 packages, exit 0.

Mandatory repository evidence:

- `make lockfile-audit`: exit 0; 58 backend and 191 widget packages.
- `make cooldown-check`: exit 0; 81 packages.
- `make sbom-check`: exit 0 across all profiles with default restored.
- `make digest-pin-lint`: exit 0.
- `make image-scan`: exit 0.
- `make gemini-image-check`: exit 0.
- `make firestore-image-check`: exit 0, including default, Firestore, Gemini, and
  combined profiles with default restoration.
- Final `make validate`: exit 0; 838 backend tests passed plus all widget protocol,
  history, layout, browser, type, build, size, distribution, supply-chain, and image
  gates. One upstream Starlette deprecation warning was reported.

The first full validation attempt exposed test-only optional-SDK imports under the
default dependency profile. The tests were repaired to use deterministic fakes in
the default profile and the real SDK only under the Firestore profile; the final
full validation above is the post-repair result.

The image scanner reported only the repository-accepted low-severity Python
`CVE-2026-15310`, whose listed fix is the prerelease `3.15.0rc2`; the gate exited 0.

## Scope Confirmation

No dependency or lockfile changed. No existing public contract changed. No files
outside the milestone allowlist changed. The primary worktree's untracked `bugs/`,
`/private/tmp/cairn-KAN-71`, all other worktrees, and VoiceVerdict were untouched.
This build lane did not push, merge, deploy, or mutate any external system.
