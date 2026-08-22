#!/usr/bin/env bash
# Operator gate. First cut: only the SBOM
# vulnerability scan is real so far, since it's the one step that doesn't
# depend on a release manifest or a running compose stack. Steps 1, 3, and 4
# land in Phase 7 once `make release` and the compose stack exist.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "[1/4] checksum verification: skipped — no release manifest yet (Phase 7.2)"

echo "[2/4] scanning committed SBOMs for known vulnerabilities"
grype sbom:backend/sbom.cdx.json --fail-on medium
grype sbom:widget/sbom.cdx.json --fail-on medium

echo "[3/4] running-image digest check: skipped — no compose stack yet (task 1.8)"
echo "[4/4] smoke test (/healthz, /readyz, SSE round trip): skipped — no app yet (task 1.5+)"

echo "verify: SBOM scan clean; remaining steps land in tasks 1.5, 1.8, and Phase 7"
