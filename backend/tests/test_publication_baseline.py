import importlib.util
from pathlib import Path
from types import ModuleType

import yaml


def _baseline_module() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "check_publication_baseline.py"
    spec = importlib.util.spec_from_file_location("publication_baseline_for_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_project_record_matches_the_published_baseline() -> None:
    baseline = _baseline_module()

    errors = baseline.publication_baseline_errors(Path(__file__).parents[2] / "project.yaml")

    assert errors == []


def test_baseline_rejects_a_public_repository_marked_empty(tmp_path: Path) -> None:
    baseline = _baseline_module()
    project = yaml.safe_load((Path(__file__).parents[2] / "project.yaml").read_text())
    project["systems"]["source"]["state"] = "created-but-empty"
    project_file = tmp_path / "project.yaml"
    project_file.write_text(yaml.safe_dump(project), encoding="utf-8")

    errors = baseline.publication_baseline_errors(project_file)

    assert "systems.source.state must be 'published'" in errors


def test_baseline_requires_immutable_initial_publication_evidence(tmp_path: Path) -> None:
    baseline = _baseline_module()
    project = yaml.safe_load((Path(__file__).parents[2] / "project.yaml").read_text())
    project["systems"]["source"].pop("initial_publication")
    project_file = tmp_path / "project.yaml"
    project_file.write_text(yaml.safe_dump(project), encoding="utf-8")

    errors = baseline.publication_baseline_errors(project_file)

    assert "systems.source.initial_publication must be a mapping" in errors


def test_baseline_rejects_swapped_first_publication_and_first_successful_shas(
    tmp_path: Path,
) -> None:
    baseline = _baseline_module()
    project = yaml.safe_load((Path(__file__).parents[2] / "project.yaml").read_text())
    project["systems"]["source"]["initial_publication"]["head_sha"] = (
        "ab1668696acc60ca7a696f6f738bb9425d1eb3ea"
    )
    project["systems"]["ci"]["first_successful_run"]["head_sha"] = (
        "04a1db5d72884eb8fbf803ab46686ccc58d3a9b6"
    )
    project_file = tmp_path / "project.yaml"
    project_file.write_text(yaml.safe_dump(project), encoding="utf-8")

    errors = baseline.publication_baseline_errors(project_file)

    assert (
        "systems.source.initial_publication.head_sha must be "
        "'04a1db5d72884eb8fbf803ab46686ccc58d3a9b6'"
    ) in errors
    assert (
        "systems.ci.first_successful_run.head_sha must be "
        "'ab1668696acc60ca7a696f6f738bb9425d1eb3ea'"
    ) in errors


def test_baseline_requires_the_first_successful_run_evidence(tmp_path: Path) -> None:
    baseline = _baseline_module()
    project = yaml.safe_load((Path(__file__).parents[2] / "project.yaml").read_text())
    project["systems"]["ci"]["first_successful_run"]["id"] = "unexpected"
    project_file = tmp_path / "project.yaml"
    project_file.write_text(yaml.safe_dump(project), encoding="utf-8")

    errors = baseline.publication_baseline_errors(project_file)

    assert "systems.ci.first_successful_run.id must be '32605367945'" in errors


def test_baseline_rejects_moving_current_evidence_field_names(tmp_path: Path) -> None:
    baseline = _baseline_module()
    project = yaml.safe_load((Path(__file__).parents[2] / "project.yaml").read_text())
    project["systems"]["source"]["public_main"] = {"sha": "not-a-current-pointer"}
    project["systems"]["ci"]["latest_successful_run"] = {"id": "not-a-current-run"}
    project_file = tmp_path / "project.yaml"
    project_file.write_text(yaml.safe_dump(project), encoding="utf-8")

    errors = baseline.publication_baseline_errors(project_file)

    assert "systems.source must use initial_publication, not public_main" in errors
    assert "systems.ci must use first_successful_run, not latest_successful_run" in errors


def test_baseline_requires_enabled_empty_tracking_features(tmp_path: Path) -> None:
    baseline = _baseline_module()
    project = yaml.safe_load((Path(__file__).parents[2] / "project.yaml").read_text())
    project["systems"]["issue_tracking"]["state"] = "not-yet-created"
    project["systems"]["project_board"]["state"] = "created"
    project_file = tmp_path / "project.yaml"
    project_file.write_text(yaml.safe_dump(project), encoding="utf-8")

    errors = baseline.publication_baseline_errors(project_file)

    assert "systems.issue_tracking.state must be 'enabled-empty'" in errors
    assert "systems.project_board.state must be 'not-yet-created'" in errors
