.PHONY: validate verify eval release \
	no-stub-check lint-backend test-backend build-widget \
	lockfile-audit cooldown-check gen-sbom sbom-check

# Same command locally (pre-push hook) and in CI (.github/workflows/validate.yml)
# — see MASTER_PLAN.md §3. Image-scan (grype) and digest-pin lint land in task
# 1.8 once Dockerfiles/compose.yaml exist to scan.
validate: no-stub-check lint-backend test-backend build-widget lockfile-audit cooldown-check sbom-check

# Scoped to source directories, not docs/*.md — the plan and README
# discuss this policy in prose, which isn't a stub marker.
no-stub-check:
	@! grep -rEn \
		--exclude-dir=node_modules --exclude-dir=.venv --exclude-dir=dist \
		--exclude-dir=.mypy_cache --exclude-dir=.ruff_cache --exclude-dir=.pytest_cache \
		--exclude='package-lock.json' --exclude='uv.lock' --exclude='*.cdx.json' \
		'\b(TODO|FIXME|XXX|NotImplementedError)\b' backend widget eval scripts 2>/dev/null \
		|| (echo "no-stub gate failed: remove the markers above before merging" && exit 1)

lint-backend:
	cd backend && uv run ruff check . && uv run mypy .

test-backend:
	cd backend && uv run pytest -q

build-widget:
	cd widget && npm ci && npm run typecheck && npm run build && npm run check-size

lockfile-audit:
	osv-scanner scan source --lockfile=backend/uv.lock --lockfile=widget/package-lock.json

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

# Operator gate — see MASTER_PLAN.md §4 and scripts/verify.sh.
verify:
	@./scripts/verify.sh

# Eval harness — Phase 2.6.
eval:
	@echo "eval: harness lands in task 2.6"

# Release bundler — Phase 7.2.
release:
	@echo "release: bundler lands in task 7.2"
