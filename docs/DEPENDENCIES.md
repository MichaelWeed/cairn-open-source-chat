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

## Widget (`widget/package-lock.json`)

* **esbuild** — bundler + size-budget enforcement for the widget (<100KB gz). Pinned to 0.28.1 to clear a moderate dev-server-only advisory in <=0.24.2 (not applicable to our one-shot build usage, but no reason to carry it).
* **typescript** — type-checking for widget source.
* **@cyclonedx/cyclonedx-npm** — generates the widget SBOM for `make validate`'s SBOM-diff gate. Pinned to 5.0.0 (6.0.0 was inside the cooldown window). Installed with `libxmljs2`/`ajv` (its optional XML/JSON-validation backends) omitted via `widget/.npmrc` (`omit=optional`) — we only need JSON SBOM output, and that dependency subtree pulled in several packages that were themselves inside the cooldown window.
* **lru-cache** (transitive, via cyclonedx-npm → hosted-git-info) — pinned to 11.5.1 via `overrides` in `widget/package.json`; the resolver's default pick was inside the cooldown window.

## Tooling (not in a lockfile — installed via Homebrew locally, via CI steps in `.github/workflows/validate.yml`)

* **osv-scanner** — lockfile vulnerability audit (`make validate`).
* **grype** — SBOM/image vulnerability scan (`make validate`, `scripts/verify.sh`).
* **cyclonedx-python** (provides `cyclonedx-py`) — generates the backend SBOM.
