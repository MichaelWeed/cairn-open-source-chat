# M6 / KAN-47b implementation report

## Result

READY_FOR_REVIEW

- Base: `d65d86c46d8c0e0096c61893a28afb62673ade27`
- Implementation commit: `90ac01e7e304111d3dfae9a61f7ce273cef3fd59`
- Mainline integrated: `0cfe87fc10997f6f28cf009b7101ede58f6eeb22`
- Normal merge commit: `c3a5671940a5a3bb4d0de2313a720754da828517`
- Readiness repair commit: `fc2a6661a695fdf3529a6d7d8a93a165a9fe1c8e`
- Privacy/offline-guard repair commit: `fa170863e6330d04dd85edea3de524a7fb6de239`
- Full-gate test-typing repair commit: `051946e`
- Branch: `mara/KAN-47b-firestore-retrieval`
- Push: not performed

## Delivered boundary

Added one optional, development/test-only Firestore exact-corpus retrieval adapter
behind the accepted M5 retrieval contract. The implementation provides fixed
database/collection/schema/query mapping, K + 1 deterministic cutoff handling,
full atomic row validation, content-free error translation, bounded timeout/retry,
scope-aware non-claiming readiness, and idempotent owned-client cleanup. Local
retrieval remains the default and production Firestore selection remains rejected.

The application-owned request-policy seam now supplies the configured exact scope,
distance measure, and threshold without changing the public chat or SSE contracts.
The optional image switch, four-profile SBOM generation, capability state,
configuration parity, dependency rationales, and affected architecture/privacy/
security/operator documentation are included.

## Dependency stop-gate evidence

The lock retains `google-auth==2.57.0`, adds only the approved direct optional
`google-cloud-firestore==2.29.0`, and constrains exactly the approved cooldown set:

- `google-api-core==2.34.0`
- `googleapis-common-protos==1.75.1`
- `grpcio==1.83.0`
- `grpcio-status==1.83.0`
- `protobuf==7.35.1`

Default, Firestore-only, Gemini-only, and combined Gemini plus Firestore locked
resolutions each completed with status 0. The network-enabled cooldown preflight
reported `cooldown check passed (81 packages checked)`. No additional inside-window
package was selected. Each constraint has a one-line rationale in
`docs/DEPENDENCIES.md`.

## Focused verification

All commands used the locked dependency graph and exited 0.

- Firestore profile focused suite: `240 passed`, one pre-existing Starlette
  deprecation warning.
- Firestore profile Ruff: `All checks passed!`
- Firestore profile mypy: `Success: no issues found in 30 source files`
- Firestore profile `uv lock --check`: resolved 58 packages without drift.
- Combined Gemini plus Firestore focused suite: `278 passed`, the same warning.
- Combined profile Ruff: `All checks passed!`
- Combined profile mypy: `Success: no issues found in 30 source files`
- Combined profile `uv lock --check`: resolved 58 packages without drift.
- Default-profile focused regression suite: `193 passed`, the same warning.
- Toolchain/workflow/capability/config focused suite: `86 passed`, the same warning.
- `make lockfile-audit`: no issues found.
- `make cooldown-check`: status 0; the earlier network-enabled stop gate resolved
  all publish dates and reported zero cooldown findings.
- `make digest-pin-lint`: `digest-pin lint passed`.
- `make sbom-check`: status 0; reproducibly regenerated default, Gemini,
  Firestore-only, combined-hosted, and widget SBOMs and restored the default backend
  environment.
- `git diff --cached --check`: status 0 before the implementation commit.

`uv` crashes inside the restricted macOS sandbox while initializing its system
configuration backend, so profile sync and SBOM commands used the isolated
`/private/tmp/cairn-m6-uv-cache` outside that restriction. The dependency content
remained locked and offline during final focused profile runs.

## Determinism, privacy, and lifecycle evidence

- The shared no-external fixture denies DNS resolution, socket connection paths,
  provider streams, ADC when installed, and the application Firestore factory.
- Adapter tests inject transport, embeddings, sleeper, and ownership. They verify
  exact query/projection mapping, no threshold/offset/page/order controls, K + 1
  cutoff behavior, stable document keys, row bounds/types, duplicate and repeated
  document conflicts, retry/cancellation, readiness, and cleanup.
- Transport and Pydantic validation failures are detached from raw exception causes
  and contexts before the fixed M5 error crosses the adapter boundary.
- Debug log capture proves canary query, document, URL, and transport-error content
  is absent.
- Default selection cannot construct the Firestore factory; injected adapters remain
  caller-owned, while application-selected adapters close exactly once.
- No credential, live provider call, cloud write, emulator, deployment, or
  VoiceVerdict material was used.

## Deferred shared gates

Per control-lane instruction, `make firestore-image-check`, `make
gemini-image-check`, `make image-scan`, and full `make validate` were not run in this
lane. Their Makefile definitions and image/SBOM inputs are prepared for the shared
review gate.

## Current-main integration and readiness repair

The M4 provider usage/accounting mainline was integrated through a normal two-parent
merge. The one privacy-document conflict was resolved cumulatively. Automatic code
merges were reviewed semantically: retrieval refusal returns before provider stream
construction and accounting; grounded generation filters internal usage records;
the absolute public ping deadline, cancellation cleanup, and frozen SSE payloads are
preserved; and capability schema 1.1 contains both development-only hosted durable
retrieval and available provider usage/cost accounting.

Independent review then identified that `FirestoreSdkVectorClient.readiness_get`
fell through after a successful SDK read. The added negative control produced three
failures at the success fallthrough assertion for empty, nonempty, and deliberately
malformed completed results. The repair returns immediately after any completed
bounded limit-1 read and continues to normalize transport failures content-free.
Wrapper tests also prove exact projection, limit, timeout, `retry=None`, cleanup,
and absence of raw error cause/context.

Post-repair integration evidence, all status 0:

- focused config/retrieval/chat/provider/accounting/capability suite with Gemini and
  Firestore extras: `466 passed`, one pre-existing Starlette warning;
- Ruff: `All checks passed!`;
- mypy: `Success: no issues found in 32 source files`;
- default, Firestore-only, Gemini-only, and combined locked profile syncs;
- `uv lock --check`, followed by restoration of the default environment.

The final privacy repair replaces raw Settings validation failures with a
`ValidationError`-compatible, content-free representation. Direct construction,
all three `model_validate` entry points, and all three equivalent `TypeAdapter`
entry points now detach rejected input, context, cause, and provider identifiers.
Per-call `extra=allow`, `ignore`, and `forbid` cannot retain unknown content and
preserve the Settings contract's ignore behavior. The adversarial matrix covers 24
surface/override combinations plus direct construction; all 49 privacy and valid
path cases pass.

The shared and Firestore-specific no-external fixtures now have direct proof that
their Gemini, Ollama, production Firestore factory, socket, DNS, and optional ADC
denials execute. The cumulative final focused suite reports `569 passed` with one
pre-existing Starlette warning. Ruff reports `All checks passed!`, mypy reports
`Success: no issues found in 32 source files`, all four locked profiles sync, and
`uv lock --check` passes before the default environment is restored.

## Full-gate test-typing repair

The control lane's first full `make validate` run reached backend mypy and exposed
four test-only annotation errors in `test_retrieval_firestore.py`: one private
module-export access and three writes through the public row field's read-only
`Mapping` annotation. Commit `051946e` patches the public `importlib` module
directly and narrows the known dictionary fixtures to typed `MutableMapping`
views at the mutation sites. Production code and runtime test behavior are
unchanged.

Post-repair verification, all status 0:

- full backend Ruff scope, including eval: `All checks passed!`;
- full backend mypy scope, including eval: `Success: no issues found in 66 source files`;
- M6 Firestore focused matrix: `300 passed`, with the pre-existing Starlette warning;
- `git diff --check`: clean.

Per control-lane instruction, the shared full and image gates were not rerun in
this repair lane. The control lane retains ownership of that serialized rerun.

## Self-review

Read the complete source/config/docs diff and both new source/test files before
commit. The committed change is limited to the M6 allowlist. A staged secret/stub/
live-test/VoiceVerdict scan found no matches. Public contracts, local ingestion,
retrieval contract models, widget code, deployment state, and cloud state are
unchanged.
