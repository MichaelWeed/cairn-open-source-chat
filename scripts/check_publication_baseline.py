"""Validate the structured durable record of Cairn's public-source baseline."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


REPOSITORY_ROOT = Path(__file__).parents[1]
PUBLIC_MAIN_SHA = "ab1668696acc60ca7a696f6f738bb9425d1eb3ea"
SUCCESSFUL_WORKFLOW_RUN = "32605367945"


def publication_baseline_errors(project_file: Path) -> list[str]:
    """Return structured-record violations without depending on narrative prose."""
    parsed = yaml.safe_load(project_file.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        return ["project.yaml must contain a mapping"]

    systems = parsed.get("systems")
    if not isinstance(systems, dict):
        return ["project.yaml must define systems"]
    source = systems.get("source")
    ci = systems.get("ci")
    if not isinstance(source, dict) or not isinstance(ci, dict):
        return ["project.yaml must define systems.source and systems.ci mappings"]

    errors: list[str] = []
    expected_source: dict[str, Any] = {
        "visibility": "public",
        "state": "published",
    }
    for field, expected in expected_source.items():
        if source.get(field) != expected:
            errors.append(f"systems.source.{field} must be {expected!r}")

    public_main = source.get("public_main")
    if not isinstance(public_main, dict):
        errors.append("systems.source.public_main must be a mapping")
    else:
        expected_main: dict[str, Any] = {
            "ref": "refs/heads/main",
            "sha": PUBLIC_MAIN_SHA,
            "published_on": "2026-08-22",
        }
        for field, expected in expected_main.items():
            if public_main.get(field) != expected:
                errors.append(f"systems.source.public_main.{field} must be {expected!r}")

    if ci.get("state") != "active-green":
        errors.append("systems.ci.state must be 'active-green'")
    successful_run = ci.get("latest_successful_run")
    if not isinstance(successful_run, dict):
        errors.append("systems.ci.latest_successful_run must be a mapping")
    else:
        expected_run: dict[str, Any] = {
            "id": SUCCESSFUL_WORKFLOW_RUN,
            "head_sha": PUBLIC_MAIN_SHA,
            "completed_on": "2026-08-22",
        }
        for field, expected in expected_run.items():
            if successful_run.get(field) != expected:
                errors.append(f"systems.ci.latest_successful_run.{field} must be {expected!r}")

    return errors


def main() -> int:
    errors = publication_baseline_errors(REPOSITORY_ROOT / "project.yaml")
    if errors:
        for error in errors:
            print(f"publication baseline check failed: {error}")
        return 1
    print("publication baseline check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
