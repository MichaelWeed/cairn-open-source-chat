# M9 BUILD-45b Implementation Report

## Result

- Work ID: `customer-zero-upstream-20260908:M9:BUILD-45b:1`
- Segment: `M9 / KAN-45b active corpus retrieval routing`
- Status: complete; all focused and control-owned repository gates are green
- Branch: `mara/KAN-45b-active-corpus-routing`
- Worktree: `/Users/johndoe/Projects/Cairn/.claude/worktrees/kan45b-active-corpus-routing`
- Exact base: `dbc7d77d17bea366cce41e1726a469229a491d39`
- Implementation commit: `a8cb07eed23601d9d662284893236316ab17dda0`
- Final commit: the normal post-review repair commit; exact object ID is returned to
  control after commit
- Model/context telemetry: unavailable

## Changed files

- `backend/app/retrieval_route.py`
- `backend/app/api/chat.py`
- `backend/app/main.py`
- `backend/tests/test_retrieval_route.py`
- `backend/tests/test_chat_stream.py`
- `backend/tests/test_app.py`
- `DEVELOPER_README.md`
- `docs/ARCHITECTURE.md`
- `docs/COMPATIBILITY.md`
- `docs/PRIVACY.md`
- `docs/SECURITY.md`
- `.agent/runs/customer-zero-upstream-20260908/M9/build-report-45b.md`

The staged implementation diff before adding this report is 11 files, 4,055 insertions,
and 48 deletions. Its `git diff --cached --binary` SHA-256 is
`3fc7e490dc329a919b09eee3928f896867fe3694273905ca82f7de1e88069a6e`.
The report is intentionally excluded from that implementation fingerprint so the
fingerprint is not self-referential.

No frozen M5, M6, M7, M8, 45a lifecycle, configuration, capability, startup,
dependency, lockfile, provider, public contract, local vector-store, workflow, image,
or SBOM file changed.

## Preconditions and ancestry

The final brief SHA-256 was confirmed as
`3b759a6deaf3b46c4d8bdfaf912139746fc8ec068f067ae015d801d9285ad9ff`.
The worktree began clean from exact `origin/main` at the stated base. Independent
`git merge-base --is-ancestor <commit> HEAD` checks exited `0` for:

- M5: `2b0d0d54bd60fcc51f68ccc3fa5ecccd68c9fa15`
- M6: `e59f2a96cdb4daab321a5055f8bc4dcc9b43497f`
- M7: `35b367732baab9c3c529dba92e615333c3987322`
- M8 source: `f2a579163f72778b65ab2087ecdcdab9e66bc125`
- integrated M8: `c99dd4bc3c1eb98a4992bb32d3b91343cd908af3`
- BUILD-45a source: `81a0c9f97cf135e66097dd36a994bd65ddeeadd8`
- BUILD-45a parity repair: `58a7122a10578c6c4a9e07528bb7ff76b304160f`
- BUILD-45a merge and this build base: `dbc7d77d17bea366cce41e1726a469229a491d39`

The accepted 45a and M8 public interfaces matched the brief. No re-preflight was
required.

## Implementation evidence

### Exact route and M6 binding authority

`ResolvedRetrievalRoute` carries one scope and adapter together. Exact routes also
carry an `ExactRetrievalAdapterBinding`. The narrow M6 bridge accepts only the exact
`FirestoreRetrievalAdapter` class and reads only its authoritative `_scope`,
`_embedding_identity`, and `_embedding_dimensions` fields. It rejects descriptor
lies, missing or malformed private authority, subclasses, lookalikes, hidden model
state, moving aliases, and post-construction mutation before adapter readiness or
retrieval I/O.

Chat awaits one resolver call before constructing `RetrievalRequest`, uses only the
returned scope and adapter, and revalidates the route immediately before
`adapter.retrieve`. A direct regression mutates M6 embedding authority after the
first validation and proves the second validation refuses with zero embedding,
vector, citation, or provider work.

### Lifecycle verification and bounded cache

The lifecycle resolver calls the accepted 45a `resolve_active_state` seam once per
operation and validates exact embedding identity and dimensions before M8 or factory
work. On a new content-free fingerprint it acquires one lock, rechecks policy
rotation, selects the exact trusted identity and verify-only verifier, and calls the
accepted bound M8 verification method. The exact strict M8 evidence is projected
across all 11 public fields and must equal 45a evidence.

The retained state is bounded to one last-good tuple and one policy
version/generation pair. It retains no adapter, verifier, policy object, evidence
model, content, signature, query, vector, credential, or SDK response. Concurrent
waiters prove exactly one refresh winner. Failures and cancellation leave the prior
cache unchanged and never fall back to an older route.

The actual bound `CandidateAttestationVerificationService.verify_attested_candidate`
test seeds a fake durable store, switches it to read-only before resolution, and
proves one verification with zero sign or write calls.

### A-to-B concurrency and readiness

A barrier-controlled chat test uses the actual `LifecycleRetrievalRouteResolver`.
Request A blocks inside its A-bound vector query while a second request observes a
promoted B state, independently revalidates B, constructs the B-bound M6 adapter, and
finishes through B. A then resumes through A. Exact Firestore filters prove no mixed
version and the provider remains uncalled for empty retrieval results.

Lifecycle readiness resolves and validates the route, then validates it again
immediately before the single M6 readiness call. A mutation inserted between those
checks fails before readiness I/O. Local readiness always reports
`exact_version_ready=false`; lifecycle exact readiness is true only when reachability,
store readiness, active state, trust, M8 evidence, and binding authority all pass.

### Failure, privacy, cancellation, ownership, and external-denial evidence

Fixed content-free `RetrievalError` results are raised outside caught exception
frames. Tests recursively inspect `args`, `vars`, `__cause__`, and `__context__`, logs,
and public output for active-state, M8, policy, and factory canaries. No canary is
retained or emitted.

Cancellation is re-raised unchanged during active-state resolution, policy supply,
refresh-lock wait, verifier selection, M8 verification, factory creation, readiness,
route resolution, and adapter retrieval. Tests prove no later refresh, factory,
readiness, embed, vector, citation, provider, fallback, or close operation as
applicable.

Injected resolver, 45a resolver dependency, bound M8 verification service, M6
adapters, and vector clients remain caller-owned. Normal and failed application
startup close neither injected 45a nor M8 resources. Existing app-owned vector-client
tests now assert exactly one close on both normal and failed startup.

An autouse route guard denies DNS, socket creation/connect, environment/key lookup,
ADC, providers, and M6/M8/45a production factories. Direct negative controls exercise
every guard. All tests use fakes and no live network, provider, credential, key,
Firestore, deploy, or promotion operation occurred.

### Frozen public behavior

Raw-byte SHA-256 assertions against the accepted base prove configuration,
`capabilities.json`, and recursive startup are unchanged. Exact response-byte tests
prove `/readyz` retains its 200 response and both database-failure and vector-store
503 responses. Public chat and SSE contracts are unchanged. Default startup creates
only the static local/configured route; production lifecycle trust/factory selection
remains absent and BUILD-45c remains separate.

## Red-first and repair evidence

The first resolver test collection exited `2` with
`ModuleNotFoundError: app.retrieval_route`, before the module existed. The initial
eight route tests then passed after the first implementation. Independent adversarial
review found and red-tested two source defects: hidden exact-model `__dict__` extras
could bypass strict copying, and a malicious local readiness result could claim exact
version readiness. Both were fixed before the broad matrix.

The first final default-profile rerun after the coverage expansion exited `1` with
235 tests passed and 131 setup errors because the test guard called
`find_spec("google.auth")` when the optional parent `google` package was absent. The
guard now handles that missing parent, includes a direct regression, and preserves
the ADC denial when installed. The corrected default profile is green below. This was
a test-only portability repair; runtime source did not change.

After the implementation commit, independent full-diff review found that
`_InjectedAdapter.check_readiness` had been indented after an unconditional helper
return, making it unreachable. The method was moved onto `_InjectedAdapter` and the
dead block was removed without changing runtime source. Post-repair evidence is:

- `uv run --locked --extra gemini --extra firestore pytest -q tests/test_app.py`:
  exit `0`; 13 passed, 1 upstream warning, 0.89 s pytest time.
- `uv run --locked pytest -q tests/test_retrieval_route.py tests/test_retrieval_conformance.py tests/test_chat_stream.py tests/test_chat_endpoint.py tests/test_app.py tests/test_config.py tests/test_capabilities.py tests/test_live_startup.py`:
  exit `0`; 367 passed, 1 upstream warning, 0.83 s pytest time.
- `make lint-backend`: exit `0`; Ruff passed and mypy reported success across 78
  source files, 0.90 s observed command time.

## Focused verification

All commands ran from `backend/` unless noted.

- `uv sync --locked`: exit `0`; 58 packages resolved; optional feature packages
  removed for a true default-profile run.
- `uv run --locked pytest -q tests/test_retrieval_route.py tests/test_retrieval_conformance.py tests/test_chat_stream.py tests/test_chat_endpoint.py tests/test_app.py tests/test_config.py tests/test_capabilities.py tests/test_live_startup.py`:
  exit `0`; 367 passed, 1 upstream Starlette deprecation warning, 0.88 s pytest time.
- `uv sync --locked --extra firestore`: exit `0`; 58 packages resolved.
- `uv run --locked --extra firestore pytest -q tests/test_retrieval_route.py tests/test_corpus_lifecycle.py tests/test_corpus_lifecycle_firestore.py tests/test_candidate_persistence.py tests/test_candidate_firestore.py tests/test_retrieval_firestore.py tests/test_retrieval_conformance.py tests/test_chat_stream.py tests/test_chat_endpoint.py tests/test_app.py tests/test_config.py tests/test_capabilities.py tests/test_live_startup.py`:
  exit `0`; 751 passed, 1 upstream Starlette deprecation warning, 11.38 s pytest time.
- `uv sync --locked --extra gemini --extra firestore`: exit `0`; 58 packages
  resolved.
- `uv run --locked --extra gemini --extra firestore pytest -q tests/test_retrieval_route.py tests/test_corpus_lifecycle.py tests/test_corpus_lifecycle_firestore.py tests/test_candidate_persistence.py tests/test_candidate_firestore.py tests/test_retrieval_firestore.py tests/test_retrieval_conformance.py tests/test_chat_stream.py tests/test_chat_endpoint.py tests/test_app.py tests/test_config.py tests/test_capabilities.py tests/test_live_startup.py tests/test_provider_conformance.py tests/test_provider_gemini.py`:
  exit `0`; 801 passed, 1 upstream Starlette deprecation warning, 9.52 s pytest time.
- From repository root, `make lint-backend`: exit `0`; Ruff passed and mypy reported
  success across 78 source files, 0.86 s observed command time.
- `uv lock --check`: exit `0`; 58 packages resolved in 6 ms.
- `git diff --cached --check`: exit `0` before adding this report.

## Mandatory repository gates

The build lane did not run shared container, image, OSV, or full validation gates.
Control serialized and observed every required repository gate on the frozen tree:

- `make lockfile-audit`: exit `0`.
- `make cooldown-check`: exit `0`; cooldown result `81`.
- `make sbom-check`: exit `0`; SBOM parity passed.
- `make digest-pin-lint`: exit `0`.
- `make firestore-image-check`: exit `0`; the target covered Firestore and the
  combined Gemini plus Firestore image.
- `make gemini-image-check`: exit `0`.
- `make image-scan`: exit `0`; fixed-medium policy passed.
- `make validate`: exit `0`; 1,295 backend tests passed, Ruff and mypy passed across
  78 files, and the complete widget, browser, build, distribution, supply-chain, and
  feature-image checks passed. Control did not provide precise wall times, so none are
  fabricated here.

## Audit and deviations

- Every changed path is in the brief allowlist.
- Full source, integration, test, documentation, and staged diff review found no
  TODO, stub, detached task, fallback route, second active-state read, mutable alias,
  sensitive log, trust/key loader, production lifecycle factory, or live endpoint.
- No dependency, lockfile, configuration, capability, startup, public schema,
  lifecycle, candidate-persistence, M6 adapter, provider, workflow, image, or SBOM file
  changed.
- No dependency was added and no install outside the locked repository environment
  occurred.
- The only deviations were adversarial test-matrix expansion requested by independent
  review, the corrected optional-package availability probe, and the post-review
  unreachable test-helper repair described above. All stayed inside the accepted
  component and file allowlist.

## Reviewer focus and next action

Review the private-authority bridge at route return and immediately before readiness
or retrieval, lock-before-verifier ordering, exact 11-field M8 projection, one-entry
cache rotation invariants, actual A-to-B in-flight separation, content-free exception
construction, and caller ownership. All control-owned gates are exit `0`. The
implementation commit is followed by one normal narrow repair commit, after which the
clean candidate returns for independent full-diff acceptance. Do not push or merge from
this lane.
