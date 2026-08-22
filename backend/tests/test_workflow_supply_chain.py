import importlib.util
from pathlib import Path
from types import ModuleType


def _policy_module() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "check_workflow_supply_chain.py"
    spec = importlib.util.spec_from_file_location("workflow_supply_chain_for_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_workflow(tmp_path: Path, content: str) -> Path:
    workflow = tmp_path / ".github" / "workflows" / "validate.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(content)
    return tmp_path


def test_committed_workflow_satisfies_supply_chain_policy() -> None:
    policy = _policy_module()
    repository_root = Path(__file__).parents[2]

    errors = policy.validate_workflows(repository_root)

    assert errors == []


def test_policy_rejects_fetched_installer_piped_to_shell(tmp_path: Path) -> None:
    policy = _policy_module()
    repository_root = _write_workflow(
        tmp_path,
        """name: validate
jobs:
  validate:
    steps:
      - uses: actions/checkout@v4
      - run: curl https://example.test/install.sh | bash
""",
    )

    errors = policy.validate_workflows(repository_root)

    assert any("piped to sh or bash" in error for error in errors)
    assert any("full 40-character commit SHA" in error for error in errors)


def test_policy_rejects_direct_execution_of_fetched_shell_installer(tmp_path: Path) -> None:
    policy = _policy_module()
    repository_root = _write_workflow(
        tmp_path,
        """name: validate
jobs:
  validate:
    steps:
      - uses: actions/checkout@1111111111111111111111111111111111111111
      - run: |
          curl --output installer.sh https://example.test/installer.sh
          bash installer.sh
""",
    )

    errors = policy.validate_workflows(repository_root)

    assert any(
        "fetched installer script must not be executed directly" in error for error in errors
    )


def test_policy_rejects_unpinned_required_tooling(tmp_path: Path) -> None:
    policy = _policy_module()
    repository_root = _write_workflow(
        tmp_path,
        """name: validate
jobs:
  validate:
    steps:
      - uses: actions/checkout@1111111111111111111111111111111111111111
      - uses: astral-sh/setup-uv@2222222222222222222222222222222222222222
        with:
          version: latest
      - run: uv tool install cyclonedx-bom
""",
    )

    errors = policy.validate_workflows(repository_root)

    assert any("setup-uv must request" in error for error in errors)
    assert any("cyclonedx-bom version" in error for error in errors)
    assert any("cyclonedx-bom must use an exact version" in error for error in errors)


def test_policy_requires_checksum_verification_for_release_artifacts(tmp_path: Path) -> None:
    policy = _policy_module()
    repository_root = _write_workflow(
        tmp_path,
        f"""name: validate
jobs:
  validate:
    steps:
      - uses: actions/checkout@1111111111111111111111111111111111111111
      - uses: astral-sh/setup-uv@2222222222222222222222222222222222222222
        with:
          version: {policy.UV_VERSION}
      - run: |
          OSV_SCANNER_VERSION={policy.OSV_SCANNER_VERSION}
          OSV_SCANNER_SHA256={policy.OSV_SCANNER_SHA256}
          GRYPE_VERSION={policy.GRYPE_VERSION}
          GRYPE_SHA256={policy.GRYPE_SHA256}
          uv tool install cyclonedx-bom=={policy.CYCLONEDX_BOM_VERSION}
""",
    )

    errors = policy.validate_workflows(repository_root)

    assert any(
        "each downloaded release artifact must be checksum-verified" in error for error in errors
    )
