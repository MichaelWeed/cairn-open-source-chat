.PHONY: validate verify eval demo release up down \
	live live-down \
	no-stub-check lint-backend test-backend build-widget \
	workflow-policy-check publication-baseline-check lockfile-audit cooldown-check gen-sbom sbom-check digest-pin-lint image-scan

# Docker and Podman are both first-class — compose.yaml
# stays within the vendor-neutral Compose Specification, and this picks
# whichever engine is installed rather than hardcoding one. Override with
# COMPOSE_CMD=<cmd> if both are installed and you want a specific one.
COMPOSE_CMD ?= $(shell command -v docker >/dev/null 2>&1 && echo "docker compose" || echo "podman compose")

# Same command locally (pre-push hook) and in CI (.github/workflows/validate.yml)
# — the same command is the project's local and CI gate.
validate: no-stub-check workflow-policy-check publication-baseline-check lint-backend test-backend build-widget lockfile-audit cooldown-check sbom-check digest-pin-lint image-scan

# Container plumbing smoke test. Checked-in defaults are echo/fake and do
# not ingest a corpus; use `make demo` for the grounded developer preview.
up:
	@COMPOSE_CMD="$(COMPOSE_CMD)" python3 scripts/check_port.py
	$(COMPOSE_CMD) up --build

down:
	$(COMPOSE_CMD) down

# Grounded operator path. The override replaces smoke providers with real
# Ollama services and requires a read-only mounted corpus before readiness.
live:
	@COMPOSE_CMD="$(COMPOSE_CMD)" python3 scripts/check_port.py
	$(COMPOSE_CMD) -f compose.yaml -f compose.live.yaml up --build

live-down:
	$(COMPOSE_CMD) -f compose.yaml -f compose.live.yaml down

# Scoped to source directories, not docs/*.md — the documentation and README
# discuss this policy in prose, which isn't a stub marker.
no-stub-check:
	@! grep -rEn \
		--exclude-dir=node_modules --exclude-dir=.venv --exclude-dir=dist \
		--exclude-dir=.mypy_cache --exclude-dir=.ruff_cache --exclude-dir=.pytest_cache \
		--exclude='package-lock.json' --exclude='uv.lock' --exclude='*.cdx.json' \
		'\b(TODO|FIXME|XXX|NotImplementedError)\b' backend widget eval scripts 2>/dev/null \
		|| (echo "no-stub gate failed: remove the markers above before merging" && exit 1)

workflow-policy-check:
	cd backend && uv run python ../scripts/check_workflow_supply_chain.py

publication-baseline-check:
	cd backend && uv run python ../scripts/check_publication_baseline.py

lint-backend:
	cd backend && uv run ruff check . ../eval && uv run mypy . ../eval/run_eval.py

test-backend:
	cd backend && uv run pytest -q

build-widget:
	cd widget && npm ci && npm test && npm run typecheck && npm run build && npm run check-size

lockfile-audit:
	osv-scanner scan source --config osv-scanner.toml \
		--lockfile=backend/uv.lock --lockfile=widget/package-lock.json

cooldown-check:
	python3 scripts/check_cooldown.py

gen-sbom:
	cd backend && uv sync --locked && cyclonedx-py environment .venv --pyproject pyproject.toml --output-reproducible -o sbom.cdx.json --of JSON
	cd widget && npm ci && node_modules/.bin/cyclonedx-npm --output-reproducible -o sbom.cdx.json

# Regenerates both SBOMs and fails if they drifted from the committed
# version. A clean diff is expected; a dirty one means either commit the
# regenerated SBOM (if the dependency change was intentional — record the
# justification in docs/DEPENDENCIES.md) or investigate why it moved.
sbom-check: gen-sbom
	@git diff --exit-code -- backend/sbom.cdx.json widget/sbom.cdx.json \
		|| (echo "SBOM drifted from the committed version — see comment above this target" && exit 1)

digest-pin-lint:
	python3 scripts/check_digest_pins.py

# Requires a running Docker/Podman engine. Builds the backend image and
# scans it — the one part of `make validate` that isn't just source code.
# Image tag assumes docker compose's <project>-<service> naming; adjust if
# your podman-compose version tags built images differently.
# --only-fixed: gate on vulnerabilities with an available fix (so the gate
# can actually be made green by upgrading) — not on not-yet-fixed/wont-fix
# OS-baseline CVEs no code change here can address. scripts/verify.sh is
# the unfiltered operator-side scan against a live vuln DB. .grype.yaml
# documents the few exceptions where "fixed" means only
# in a Python pre-release.
image-scan:
	$(COMPOSE_CMD) build backend
	grype cairn-backend:latest --fail-on medium --only-fixed

# Operator gate — see scripts/verify.sh.
verify:
	@./scripts/verify.sh

# Eval harness (task 2.6). Self-contained — ingests eval/corpus/ into an
# ephemeral app instance itself (see eval/run_eval.py's docstring for why).
# Requires a reachable Ollama with OLLAMA_MODEL and EMBEDDING_MODEL pulled;
# not part of `make validate` (DEVELOPER_README.md §6).
eval:
	cd backend && uv run python ../eval/run_eval.py

# Manual QA / screen-recording helper — see docs/QA_CHECKLIST.md. Not
# part of `make validate` (scripts/dev_demo.py is for a human at the
# browser, not CI). Requires a reachable Ollama with OLLAMA_MODEL and
# EMBEDDING_MODEL pulled. The helper preflights the corpus, Ollama, and
# both configured models before starting; it never pulls models.
demo:
	cd backend && uv run python ../scripts/dev_demo.py

# Release bundler — Phase 7.2.
release:
	@echo "release: bundler lands in task 7.2"
