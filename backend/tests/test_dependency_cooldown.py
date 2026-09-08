import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest


def _load_cooldown_module() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "check_cooldown.py"
    spec = importlib.util.spec_from_file_location("check_cooldown", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cooldown() -> ModuleType:
    return _load_cooldown_module()


def _configure_backend_release(
    monkeypatch: pytest.MonkeyPatch,
    cooldown: ModuleType,
    name: str,
    version: str,
    released: datetime,
) -> None:
    monkeypatch.setattr(cooldown, "backend_packages", lambda: [(name, version)])
    monkeypatch.setattr(cooldown, "widget_packages", lambda: [])
    monkeypatch.setattr(cooldown, "pypi_release_date", lambda _name, _version: released)


def test_early_release_approval_matches_only_the_exact_pypdf_pair(cooldown: ModuleType) -> None:
    assert cooldown.is_early_release_approved("pypdf", "6.18.0")
    assert not cooldown.is_early_release_approved("pypdf", "6.18.1")
    assert not cooldown.is_early_release_approved("another-package", "6.18.0")


def test_approved_pypdf_release_passes_inside_cooldown_window(
    monkeypatch: pytest.MonkeyPatch, cooldown: ModuleType
) -> None:
    _configure_backend_release(
        monkeypatch, cooldown, "pypdf", "6.18.0", datetime.now(UTC) - timedelta(days=1)
    )

    assert cooldown.main() == 0


def test_unapproved_release_fails_inside_cooldown_window(
    monkeypatch: pytest.MonkeyPatch, cooldown: ModuleType
) -> None:
    _configure_backend_release(
        monkeypatch, cooldown, "pypdf", "6.18.1", datetime.now(UTC) - timedelta(days=1)
    )

    assert cooldown.main() == 1


def test_another_package_fails_inside_cooldown_window(
    monkeypatch: pytest.MonkeyPatch, cooldown: ModuleType
) -> None:
    _configure_backend_release(
        monkeypatch, cooldown, "another-package", "6.18.0", datetime.now(UTC) - timedelta(days=1)
    )

    assert cooldown.main() == 1


def test_ordinary_release_age_passes_at_or_after_cooldown_window(
    monkeypatch: pytest.MonkeyPatch, cooldown: ModuleType
) -> None:
    def fail_if_approval_is_checked(_name: str, _version: str) -> bool:
        raise AssertionError("ordinary release eligibility must bypass early approval")

    monkeypatch.setattr(cooldown, "is_early_release_approved", fail_if_approval_is_checked)
    _configure_backend_release(
        monkeypatch, cooldown, "pypdf", "6.18.0", datetime.now(UTC) - timedelta(days=14, seconds=1)
    )

    assert cooldown.main() == 0
