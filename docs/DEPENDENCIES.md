# Dependency Justifications

Per `CLAUDE.md` / `MASTER_PLAN.md` §3: every new dependency gets a one-line justification here at the moment it lands in a lockfile.

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
* **chromadb** — embedded vector store (task 2.1), `PersistentClient` mode (no server process). Pulls a large transitive tree (numpy, onnxruntime, opentelemetry, grpcio, etc. — chromadb's own dependencies, not ours to trim); ~10 transitive deps needed cooldown pins, recorded via `[tool.uv] constraint-dependencies`. Bumped `requires-python` to `>=3.12` because `numpy` (a chromadb dependency) dropped 3.11 support — harmless since local dev and the Docker image already run 3.13. `osv-scanner.toml` documents one exception: a Critical pre-auth code-injection CVE (GHSA-f4j7-r4q5-qw2c) in chromadb's HTTP server API, which this project never runs (embedded mode only, no fixed chromadb version exists yet).
* **pypdf** — PDF text extraction for upload ingestion (task 2.2).

## Widget (`widget/package-lock.json`)

* **esbuild** — bundler + size-budget enforcement for the widget (<100KB gz). Pinned to 0.28.1 to clear a moderate dev-server-only advisory in <=0.24.2 (not applicable to our one-shot build usage, but no reason to carry it).
* **typescript** — type-checking for widget source.
* **@cyclonedx/cyclonedx-npm** — generates the widget SBOM for `make validate`'s SBOM-diff gate. Pinned to 5.0.0 (6.0.0 was inside the cooldown window). Installed with `libxmljs2`/`ajv` (its optional XML/JSON-validation backends) omitted via `widget/.npmrc` (`omit=optional`) — we only need JSON SBOM output, and that dependency subtree pulled in several packages that were themselves inside the cooldown window.
* **lru-cache** (transitive, via cyclonedx-npm → hosted-git-info) — pinned to 11.5.1 via `overrides` in `widget/package.json`; the resolver's default pick was inside the cooldown window.

## Container image (`backend/Dockerfile`, task 1.8)

Not a lockfile, but pinned the same way for the same reason — recorded here after `grype` found real, fixable CVEs in the base image during task 1.8:

* **Base image**: `python:3.13-slim`, not 3.11 (the project only requires `>=3.11`). The 3.11-slim base had several CVEs in the system CPython interpreter itself with fixes available only on the 3.13+ branch.
* **`apt-get upgrade`** at build time, layered on top of the pinned base digest — picks up Debian's current security patches (fixed several `perl-base`/`libc`-family CVEs that were stale in the pinned base layer).
* **`pip`, `wheel`, `setuptools==82.0.1`** upgraded before installing `uv` — the base image's bundled versions had known CVEs, including a `jaraco-context`/`wheel` copy vendored *inside* `setuptools` that a plain `pip install --upgrade wheel` doesn't touch.
* **`uv==0.11.25`** — pinned well past 0.9.13 (the local dev toolchain's version, independent of this) specifically to clear GHSA-4gg8-gxpx-9rph (Medium).
* **`.grype.yaml`** documents four remaining exceptions: CVEs fixed only in Python 3.15 alpha/beta/unreleased builds — not appropriate to chase by running pre-release Python in production. Re-check on 3.15 GA.

## Tooling (not in a lockfile — installed via Homebrew locally, via CI steps in `.github/workflows/validate.yml`)

* **osv-scanner** — lockfile vulnerability audit (`make validate`).
* **grype** — SBOM/image vulnerability scan (`make validate`, `scripts/verify.sh`).
* **cyclonedx-python** (provides `cyclonedx-py`) — generates the backend SBOM.
