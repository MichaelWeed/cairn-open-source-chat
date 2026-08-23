"""Validate Cairn's durable public-source classifications and first-publication evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


REPOSITORY_ROOT = Path(__file__).parents[1]
FIRST_PUBLICATION_SHA = "04a1db5d72884eb8fbf803ab46686ccc58d3a9b6"
FIRST_SUCCESSFUL_WORKFLOW_RUN = "32605367945"
FIRST_SUCCESSFUL_RUN_HEAD_SHA = "ab1668696acc60ca7a696f6f738bb9425d1eb3ea"


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
    if "public_main" in source:
        errors.append("systems.source must use initial_publication, not public_main")

    initial_publication = source.get("initial_publication")
    if not isinstance(initial_publication, dict):
        errors.append("systems.source.initial_publication must be a mapping")
    else:
        expected_initial_publication: dict[str, Any] = {
            "ref": "refs/heads/main",
            "head_sha": FIRST_PUBLICATION_SHA,
            "published_on": "2026-08-22",
        }
        for field, expected in expected_initial_publication.items():
            if initial_publication.get(field) != expected:
                errors.append(
                    f"systems.source.initial_publication.{field} must be {expected!r}"
                )

    if ci.get("state") != "active-green":
        errors.append("systems.ci.state must be 'active-green'")
    if "latest_successful_run" in ci:
        errors.append("systems.ci must use first_successful_run, not latest_successful_run")
    first_successful_run = ci.get("first_successful_run")
    if not isinstance(first_successful_run, dict):
        errors.append("systems.ci.first_successful_run must be a mapping")
    else:
        expected_first_successful_run: dict[str, Any] = {
            "id": FIRST_SUCCESSFUL_WORKFLOW_RUN,
            "head_sha": FIRST_SUCCESSFUL_RUN_HEAD_SHA,
            "completed_on": "2026-08-22",
        }
        for field, expected in expected_first_successful_run.items():
            if first_successful_run.get(field) != expected:
                errors.append(f"systems.ci.first_successful_run.{field} must be {expected!r}")

    issue_tracking = systems.get("issue_tracking")
    if not isinstance(issue_tracking, dict):
        errors.append("systems.issue_tracking must be a mapping")
    elif issue_tracking.get("state") != "enabled-empty":
        errors.append("systems.issue_tracking.state must be 'enabled-empty'")

    project_board = systems.get("project_board")
    if not isinstance(project_board, dict):
        errors.append("systems.project_board must be a mapping")
    elif project_board.get("state") != "not-yet-created":
        errors.append("systems.project_board.state must be 'not-yet-created'")

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
