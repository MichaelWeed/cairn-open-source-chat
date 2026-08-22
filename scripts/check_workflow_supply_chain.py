#!/usr/bin/env python3
"""Reject mutable or unverified CI tool provisioning in committed workflows."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml


IMMUTABLE_ACTION_REF = re.compile(r"^[0-9a-f]{40}$")
FETCH_PIPE_TO_SHELL = re.compile(
    r"\b(?:curl|wget)\b[^\n]*\|\s*(?:sudo\s+)?(?:ba)?sh\b", re.IGNORECASE
)
FETCHED_SHELL_SCRIPT = re.compile(
    r"\b(?:curl|wget)\b[^\n]*(?:--output|-o)\s+([^\s'\"]+\.s?h)\b",
    re.IGNORECASE,
)

OSV_SCANNER_VERSION = "2.4.0"
OSV_SCANNER_SHA256 = "15314940c10d26af9c6649f150b8a47c1262e8fc7e17b1d1029b0e479e8ed8a0"
GRYPE_VERSION = "0.115.0"
GRYPE_SHA256 = "3fad92940650e514c0aa2dad83526942a055e210cec09a8a59d9c024adc2b90e"
UV_VERSION = "0.11.30"
CYCLONEDX_BOM_VERSION = "7.3.0"


def _workflow_steps(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    jobs = value.get("jobs")
    if not isinstance(jobs, dict):
        return []
    steps: list[dict[str, Any]] = []
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        job_steps = job.get("steps")
        if isinstance(job_steps, list):
            steps.extend(step for step in job_steps if isinstance(step, dict))
    return steps


def _errors_for_workflow(path: Path) -> list[str]:
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    steps = _workflow_steps(parsed)
    errors: list[str] = []
    run_commands: list[str] = []
    setup_uv_version: str | None = None

    for step in steps:
        uses = step.get("uses")
        if isinstance(uses, str):
            action, separator, reference = uses.partition("@")
            if not separator or not IMMUTABLE_ACTION_REF.fullmatch(reference):
                errors.append(f"{path}: uses reference must be a full 40-character commit SHA: {uses}")
            if action == "astral-sh/setup-uv":
                with_values = step.get("with")
                if isinstance(with_values, dict):
                    value = with_values.get("version")
                    if isinstance(value, (str, int, float)):
                        setup_uv_version = str(value)

        run = step.get("run")
        if not isinstance(run, str):
            continue
        run_commands.append(run)
        if FETCH_PIPE_TO_SHELL.search(run):
            errors.append(f"{path}: fetched content must not be piped to sh or bash")
        for shell_script in FETCHED_SHELL_SCRIPT.findall(run):
            invocation = re.compile(rf"\b(?:ba)?sh\s+['\"]?{re.escape(shell_script)}\b")
            if invocation.search(run):
                errors.append(f"{path}: fetched installer script must not be executed directly: {shell_script}")

    provision_text = "\n".join(run_commands)
    required_fragments = {
        "osv-scanner version": f"OSV_SCANNER_VERSION={OSV_SCANNER_VERSION}",
        "osv-scanner checksum": f"OSV_SCANNER_SHA256={OSV_SCANNER_SHA256}",
        "grype version": f"GRYPE_VERSION={GRYPE_VERSION}",
        "grype checksum": f"GRYPE_SHA256={GRYPE_SHA256}",
        "cyclonedx-bom version": f"cyclonedx-bom=={CYCLONEDX_BOM_VERSION}",
    }
    for description, fragment in required_fragments.items():
        if fragment not in provision_text:
            errors.append(f"{path}: required CI tooling is unpinned or changed: {description}")
    if provision_text.count("sha256sum --check --strict") < 2:
        errors.append(f"{path}: each downloaded release artifact must be checksum-verified")
    if setup_uv_version != UV_VERSION:
        errors.append(f"{path}: setup-uv must request uv {UV_VERSION}, found {setup_uv_version!r}")
    if re.search(r"\b(?:osv-scanner|grype)\b[^\n]*(?:/main\b|/latest\b)", provision_text):
        errors.append(f"{path}: CI tool download must not use mutable main or latest URLs")
    if re.search(r"uv\s+tool\s+install\s+cyclonedx-bom(?:\s|$)(?!==)", provision_text):
        errors.append(f"{path}: cyclonedx-bom must use an exact version")
    return errors


def validate_workflows(repository_root: Path) -> list[str]:
    workflow_dir = repository_root / ".github" / "workflows"
    paths = sorted((*workflow_dir.glob("*.yaml"), *workflow_dir.glob("*.yml")))
    if not paths:
        return [f"{workflow_dir}: no committed workflow files found"]
    return [error for path in paths for error in _errors_for_workflow(path)]


def main() -> int:
    repository_root = Path(__file__).resolve().parents[1]
    errors = validate_workflows(repository_root)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("workflow supply-chain policy passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
