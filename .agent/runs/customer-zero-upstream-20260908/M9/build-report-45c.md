# M9 BUILD-45c builder report

## Candidate identity

- Work: `M9/KAN-45c production recursive-corpus guard`
- Branch: `mara/KAN-45c-production-corpus-guard`
- Exact base: `334a20219bb3ac3991f0a17fd1406a5a52bd07ce`
- Head: this implementation commit; its exact object ID is returned with the builder handoff because a commit cannot contain its own Git object ID.
- Commit list over the base: this single normal implementation commit.
- Accepted immutable brief SHA-256: `e5778adb890648a073bdb7a5ef4153af6fbd66ce01fa3aa18773334ffc40890d`
- Implementation diff before adding this report: 11 paths, 1,048 insertions, 24 deletions; binary-patch SHA-256 `2bbd24d448403fce27d2c4c71e597398aaa96b7d1261d34106be7c620cffa933`.

## Full path list

- `backend/app/config.py`
- `backend/app/main.py`
- `backend/app/capabilities.json`
- `backend/tests/test_config.py`
- `backend/tests/test_app.py`
- `backend/tests/test_capabilities.py`
- `backend/tests/test_live_startup.py`
- `backend/tests/test_corpus_lifecycle.py`
- `DEVELOPER_README.md`
- `docs/COMPATIBILITY.md`
- `docs/CORPUS-PROVENANCE.md`
- `.agent/runs/customer-zero-upstream-20260908/M9/build-report-45c.md`

No path outside the final allowlist changed. The brief and control ledger were not edited and are not part of the implementation diff.

## Implementation and ordering

`Settings` now rejects every production deployment with a non-null `CORPUS_PATH` in a `mode="before"` validator. It iterates an exact built-in dictionary with unbound `dict.items`, considers only exact-string `deployment_mode` and `corpus_path` keys, and raises the fixed content-free `CORPUS_PATH is invalid` taxonomy before field parsing, filesystem work, provider selection, or application setup. The existing after-validator repeats the invariant so construct/copy/assignment bypasses cannot survive the runtime boundary.

`validated_settings_snapshot` obtains a supplied or cached settings object once, requires the exact `Settings` type, reads the raw state without invoking instance hooks, and selects only exact declared-string keys. It accepts only exact safe scalar types, clones exact `SecretStr` state, and reconstructs exact concrete `Path` values only from an exact built-in `_raw_paths` list containing exact strings. It ignores untrusted extras and path caches without hashing, equality, representation, conversion, or filesystem calls. It then revalidates the controlled copy through a preallocated `object.__new__(Settings)` target and the existing content-free Pydantic validator facade with `self_instance=target`. This deliberately bypasses `BaseSettings` source construction while preserving all canonical model validators. The returned exact `Settings` has a complete declared-only raw field set and owns fresh Path and secret objects.

`create_app` calls the snapshot function as its literal first action, before logging. The application owns the returned snapshot while the caller continues to own the supplied settings object. Mutation of the caller, nested paths, or secrets after `create_app` returns cannot alter application configuration.

## Adversarial and preservation evidence

The focused tests cover constructor, direct model, JSON, string, and TypeAdapter surfaces under default and all `extra` modes; omitted/null and development/test preservation; production priority over malformed provider, backend, top-k, missing key, unknown field, and Firestore debug-scope inputs; forged construct/copy/update/assignment/raw-state paths; subclasses and facsimiles; hostile raw dictionaries, hash-colliding keys, values, nested Path state, and caches; missing declared fields and hostile extras; fixed content-free error/log/exception retention; exact snapshot ownership and caller-mutation isolation; and direct tripwires for settings sources, environment, logging, filesystem, database, vector, provider, optional import, provenance, lifecycle, candidate, credentials, DNS, and socket work.

The accepted settings-source repair was red-first. An instrumented pre-repair call through `Settings.model_validate` observed two `Path.stat` calls in `DotEnvSettingsSource`. The final regression arms `Settings.model_validate`, `Settings.__init__`, `_settings_init_sources`, `Path.stat`, `Path.open`, `Path.read_text`, and `Path.read_bytes`; the new snapshot path invokes none, while direct negative controls prove every tripwire is live.

Actual startup tests preserve nested Markdown and PDF ingestion in both development and test modes and assert the exact successful `/readyz` body. KAN-86 provenance loading and startup source remain byte-identical. Provider, lifecycle, route, chat, capability endpoint, and public API behavior remain unchanged except for the explicit production recursive-corpus refusal and truthful capability scalar.

The capability asset changes exactly `/capabilities/corpus/local_directory` from `available` to `development_only`. Its formatted file SHA-256 is `0c1f398299025543a55e13798479cfade8710aec255f27399d3308f0f796a422`. The compact endpoint remains 976 bytes with SHA-256 `fad756f2c3bbda2a89d74665ff8c18b5d46503d5e2e65df58216847d1d5b6d16`. Documentation mirrors the same development/test-only recursive-ingestion boundary and preserves the offline manifest/parser/planner boundary.

## Frozen production hashes

- `backend/app/ingest/startup.py`: `380a02c156d5dc887dd6ca84304b4573d42aea46039909dc8e4f3fbccc6d93da`
- `backend/app/ingest/provenance.py`: `24fb9031d05ed5c1b0862bef446b7ea3188e9f3bb57ff27675ad6d0bb9e7f094`
- `backend/app/api/contracts.py`: `1895eaf57db82f12eac3855de603e3e686c1e75c010430addca537ed260a2cb1`
- `backend/app/api/chat.py`: `582e5885408d00b547a869fcbe82087f039cde90f54b170be13cc551212cac30`
- `backend/app/retrieval_route.py`: `4928d2991a1427cc36d703f40d931b1a1e8577f9f68bab05842595c8b078afe9`
- `backend/app/retrieval_contracts.py`: `fae47b51302018c364a5423b800f754c39f7beba138505399e49b728ca9831af`
- `backend/app/capabilities.py`: `c53c439bbb18a7be4dc6031cde1892072a3c7248e4a8f6cd3b37effe0f4e0c7e`
- `backend/app/api/capabilities.py`: `a53ee240361d25234d8dfe103d60ea675cea3f1bbaa1add8247b6de714b954fb`

## Verification evidence

All final focused commands ran from `backend` without live providers, credentials, or stores:

- `uv run --locked pytest -q tests/test_config.py tests/test_app.py tests/test_capabilities.py tests/test_live_startup.py tests/test_provenance.py tests/test_provider_selection.py`: exit 0, 282 passed, one upstream Starlette deprecation warning.
- `uv run --locked pytest -q tests/test_retrieval_route.py tests/test_corpus_lifecycle.py tests/test_chat_stream.py tests/test_chat_endpoint.py`: exit 0, 293 passed, one upstream Starlette deprecation warning.
- `uv run --locked --extra firestore pytest -q tests/test_config.py tests/test_app.py tests/test_provider_selection.py tests/test_retrieval_firestore.py`: exit 0, 291 passed, one upstream Starlette deprecation warning.
- `uv run --locked ruff check app/config.py app/main.py tests/test_config.py tests/test_app.py tests/test_capabilities.py tests/test_live_startup.py`: exit 0.
- `uv run --locked mypy . ../eval/run_eval.py`: exit 0, 78 source files clean.
- `uv lock --check`: exit 0, 58 packages resolved from the existing lock in 4 ms.
- Additional provider profile, `uv run --locked --extra gemini pytest -q tests/test_config.py tests/test_app.py tests/test_provider_selection.py`: exit 0, 239 passed, one upstream Starlette deprecation warning.
- Additional combined profile, `uv run --locked --extra firestore --extra gemini pytest -q tests/test_config.py tests/test_app.py tests/test_provider_selection.py tests/test_retrieval_firestore.py`: exit 0, 291 passed, one upstream Starlette deprecation warning.
- Final `git diff --check`: exit 0.

An earlier literal, incomplete typecheck target omitted the unchanged sibling eval source and failed only on its known `run_eval` import. Control repaired the immutable brief to the repository-canonical command above; that exact canonical command is green. The stale lifecycle capability hash test was likewise updated only after the final independently accepted allowlist repair, and its final profile is green.

The serialized `make lockfile-audit`, `make cooldown-check`, `make sbom-check`, `make digest-pin-lint`, `make firestore-image-check`, `make gemini-image-check`, `make image-scan`, and `make validate` gates were explicitly withheld from this build lane for control-owned execution. Their status, including scanner disposition, is pending control and is not represented as green here.

## Delivery assertions

The builder read the complete implementation diff and verified the exact allowlist, first-action `main.py` delta, one-leaf capability delta, frozen hashes, and clean patch formatting. No dependency, lockfile, schema, public API, retrieval, lifecycle, ingestion, provenance, provider, widget, Compose, workflow, Dockerfile, deployment, credential, live service, network, production, Jira, VoiceVerdict, push, PR, or merge mutation occurred.
