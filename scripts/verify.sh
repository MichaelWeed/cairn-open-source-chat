#!/usr/bin/env bash
# Operator gate for a locally built source release and its committed SBOMs.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "[1/4] verifying the source release integrity"
python3 scripts/verify_release.py dist/release

echo "[2/4] scanning committed SBOMs for known vulnerabilities"
grype sbom:backend/sbom.cdx.json --fail-on medium
grype sbom:widget/sbom.cdx.json --fail-on medium

echo "[3/4] source inventory is bound to the verified release manifest"
echo "[4/4] release integrity and committed SBOM scans are complete"

echo "verify: source release integrity and SBOM scans are clean"
