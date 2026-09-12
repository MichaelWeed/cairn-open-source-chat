#!/usr/bin/env bash
# Operator gate for a locally built source release and its committed SBOMs.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -d dist/release && ! -L dist/release ]]; then
  echo "[1/4] verifying the source release integrity"
  python3 scripts/verify_release.py dist/release
  release_integrity="verified"
else
  echo "[1/4] source archive integrity not rechecked: dist/release is absent"
  release_integrity="not rechecked"
fi

echo "[2/4] scanning committed SBOMs for known vulnerabilities"
grype sbom:backend/sbom.cdx.json --fail-on medium
grype sbom:widget/sbom.cdx.json --fail-on medium

echo "[3/4] checking shipped source identity and version"
python3 -c 'import json, tomllib; from pathlib import Path; root = Path("."); project = tomllib.loads((root / "backend/pyproject.toml").read_text()); manifest = json.loads((root / "backend/app/capabilities.json").read_text()); assert project["project"]["name"] == "cairn-backend"; assert project["project"]["version"] == "0.1.0" == manifest["release"]["version"]; print("source identity: cairn-backend 0.1.0")'
echo "[4/4] committed SBOM scans and source identity check are complete"

echo "verify: release integrity ${release_integrity}; committed SBOM scans and source identity are clean"
