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
