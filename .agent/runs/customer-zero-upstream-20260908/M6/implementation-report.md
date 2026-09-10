# M6 / KAN-47b implementation report

## Result

READY_FOR_REVIEW

- Base: `d65d86c46d8c0e0096c61893a28afb62673ade27`
- Implementation commit: `90ac01e7e304111d3dfae9a61f7ce273cef3fd59`
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

## Self-review

Read the complete source/config/docs diff and both new source/test files before
commit. The committed change is limited to the M6 allowlist. A staged secret/stub/
live-test/VoiceVerdict scan found no matches. Public contracts, local ingestion,
retrieval contract models, widget code, deployment state, and cloud state are
unchanged.
