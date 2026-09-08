# Dependency Justifications

Per `CLAUDE.md`: every new dependency gets a one-line justification here at the moment it lands in a lockfile.

## Backend (`backend/uv.lock`)

* **ruff** — lint gate in `make validate`; pinned to 0.15.20 (cooldown-compliant as of 2026-07-11).
* **mypy** — type-check gate in `make validate`; pinned to 1.18.2 to avoid the new `librt`/`ast-serialize` Rust-core deps introduced in mypy 2.x, which weren't past the 14-day cooldown window.
* **pytest** — test runner for `make validate`.
* **typing-extensions** (transitive, via mypy) — constrained to 4.15.0 via `[tool.uv] constraint-dependencies` because the resolver's default pick was inside the cooldown window.
* **pydantic** — the frozen contract models in `backend/app/api/contracts.py` (task 1.4): request/response validation, `extra="forbid"`, and JSON round-tripping for the chat API and SSE events.
* **fastapi** — the app framework (task 1.5): app factory, `/healthz`/`/readyz`, and (from 1.7) the chat endpoint. Pinned to 0.138.1 (cooldown-compliant as of 2026-07-11).
* **uvicorn[standard]** — ASGI server to run the FastAPI app. Pinned to 0.49.0; `websockets` (a transitive dep) constrained to 16.0 via `[tool.uv] constraint-dependencies` for the same reason.
* **pydantic-settings** — env-var config loader (`app/config.py`), consistent with the frozen-Pydantic-models approach already used for the API contracts.
* **httpx** — runtime dependency as of task 1.6: `OllamaProvider` uses it to stream `/api/chat`. Also (still) used by FastAPI's `TestClient` in tests.
* **pytest-asyncio** (dev) — runs the provider adapters' `async def test_*` functions (task 1.6); `asyncio_mode = "auto"` in `pyproject.toml` so tests don't need per-function markers.
* **SQLite** (Python standard library): embedded flat-vector index (KAN-76). It persists versioned vector rows locally without a vector database dependency; existing Chroma files are left untouched and operators explicitly re-ingest the corpus into `cairn-vectors-v1.sqlite3`.
* **pypdf**: PDF text extraction for upload ingestion (task 2.2); pinned to 6.18.0, the fixed floor for the indirect-object-header advisory. Released 2026-09-07, it has an owner-approved KAN-72 exact-pair early-release approval that applies only until the normal 14-day cooldown expires.
* **pyyaml**: declared directly because `eval/run_eval.py` imports it to load `eval/questions/*.yaml`.
* **types-pyyaml** (dev) — type stubs so `mypy --strict` can check `eval/run_eval.py`'s `yaml.safe_load` usage.

## Widget (`widget/package-lock.json`)

* **esbuild** — bundler + size-budget enforcement for the widget (<100KB gz). Pinned to 0.28.1 to clear a moderate dev-server-only advisory in <=0.24.2 (not applicable to our one-shot build usage, but no reason to carry it).
* **typescript** — type-checking for widget source.
* **@cyclonedx/cyclonedx-npm** — generates the widget SBOM for `make validate`'s SBOM-diff gate. Pinned to 5.0.0 (6.0.0 was inside the cooldown window). Installed with `libxmljs2`/`ajv` (its optional XML/JSON-validation backends) omitted via `widget/.npmrc` (`omit=optional`) — we only need JSON SBOM output, and that dependency subtree pulled in several packages that were themselves inside the cooldown window.
* **lru-cache** (transitive, via cyclonedx-npm → hosted-git-info) — pinned to 11.5.1 via `overrides` in `widget/package.json`; the resolver's default pick was inside the cooldown window.
* **brace-expansion** (transitive, optional tooling path) — pinned to 2.1.4 via `overrides` to clear two uncontrolled-resource-consumption advisories; released 2026-07-30 and outside the cooldown.
* **fast-uri** (transitive, via cyclonedx-npm → optional ajv): pinned to 3.1.6 via `overrides` to clear four URI-parser advisories (GHSA-5jgf-p345-68v8, GHSA-f65p-4m7j-42xc, GHSA-fph4-wmhf-6fwf, and GHSA-jqff-g426-hqxp); released 2026-08-23 and outside the cooldown.
* **ip-address** (transitive, optional tooling path) — pinned to 10.3.1 via `overrides` to clear three address-parser advisories; released 2026-07-25 and outside the cooldown.
* **js-yaml** (transitive, via cyclonedx-npm → xmlbuilder2) — pinned to 4.3.1 via `overrides` to clear a prototype-pollution advisory; released 2026-07-31 and outside the cooldown.
* **tar** (transitive, optional tooling path) — pinned to 7.5.21 via `overrides` to clear a path-traversal advisory; released 2026-07-21 and outside the cooldown.

## Container image (`backend/Dockerfile`, task 1.8)

Not a lockfile, but pinned the same way for the same reason — recorded here after `grype` found real, fixable CVEs in the base image during task 1.8:

* **Base image**: `python:3.13.15-slim`, not 3.11 (the project only requires `>=3.11`); pinned to the fixed 3.13.15 multi-platform digest after Grype found system-CPython CVEs in 3.13.14 (released 2026-08-05).
* **`apt-get upgrade`** at build time, layered on top of the pinned base digest — picks up Debian's current security patches (fixed several `perl-base`/`libc`-family CVEs that were stale in the pinned base layer).
* **`pip`, `wheel`, `setuptools==83.0.0`** upgraded before installing `uv` — the base image's bundled versions had known CVEs; setuptools 83.0.0 also clears GHSA-h35f-9h28-mq5c (released 2026-07-04).
* **`uv==0.11.30`** — pinned past 0.9.13 (the local dev toolchain's version, independent of this) to retain the GHSA-4gg8-gxpx-9rph fix and bundle fixed `quinn-proto` 0.11.15 (released 2026-07-20).
* **`.grype.yaml`** documents four remaining exceptions: CVEs fixed only in Python 3.15 alpha/beta/unreleased builds — not appropriate to chase by running pre-release Python in production. Re-check on 3.15 GA.

## Tooling (not in a lockfile — installed via Homebrew locally, via CI steps in `.github/workflows/validate.yml`)

* **osv-scanner** — lockfile vulnerability audit (`make validate`).
* **grype** — SBOM/image vulnerability scan (`make validate`, `scripts/verify.sh`).
* **cyclonedx-python** (provides `cyclonedx-py`) — generates the backend SBOM.
