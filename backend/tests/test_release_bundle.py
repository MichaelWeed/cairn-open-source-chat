import gzip
import hashlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY_ROOT = Path(__file__).parents[2]
BUILD_SCRIPT = REPOSITORY_ROOT / "scripts" / "build_release.py"
VERIFY_SCRIPT = REPOSITORY_ROOT / "scripts" / "verify_release.py"


def _run(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *arguments], cwd=root, check=False, text=True, capture_output=True
    )


def _git(root: Path, *arguments: str) -> None:
    result = subprocess.run(
        ["git", *arguments], cwd=root, check=False, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def _commit(root: Path, message: str) -> None:
    _git(root, "add", ".")
    _git(root, "commit", "-m", message)


@pytest.fixture
def release_repo(tmp_path: Path) -> Path:
    root = tmp_path / "release-repo"
    (root / "scripts").mkdir(parents=True)
    (root / "backend" / "app").mkdir(parents=True)
    (root / "backend").joinpath("pyproject.toml").write_text(
        "[project]\nname = \"cairn-backend\"\nversion = \"0.1.0\"\n", encoding="utf-8"
    )
    (root / "backend" / "app" / "capabilities.json").write_text(
        json.dumps({"release": {"version": "0.1.0"}}), encoding="utf-8"
    )
    for name in (
        "sbom.cdx.json",
        "sbom-firestore.cdx.json",
        "sbom-gemini.cdx.json",
        "sbom-hosted.cdx.json",
    ):
        (root / "backend" / name).write_text("{}\n", encoding="utf-8")
    (root / "widget").mkdir()
    (root / "widget" / "sbom.cdx.json").write_text("{}\n", encoding="utf-8")
    (root / "LICENSE").write_text("Apache-2.0\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text("release policy\n", encoding="utf-8")
    (root / "AGENTS.md").symlink_to("CLAUDE.md")
    (root / ".gitignore").write_text("__pycache__/\ndist/\n", encoding="utf-8")
    shutil.copy2(BUILD_SCRIPT, root / "scripts" / "build_release.py")
    shutil.copy2(VERIFY_SCRIPT, root / "scripts" / "verify_release.py")
    _git(root, "init")
    _git(root, "config", "user.email", "release-test@example.invalid")
    _git(root, "config", "user.name", "Release Test")
    _commit(root, "release fixture")
    return root


def _build(root: Path) -> subprocess.CompletedProcess[str]:
    return _run(root, str(BUILD_SCRIPT), "--root", str(root))


def _load_verify_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_release_under_test", VERIFY_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = list(sys.path)
    sys.path.insert(0, str(VERIFY_SCRIPT.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = previous
    return module


def test_release_happy_path_is_reproducible_and_extractable(
    release_repo: Path, tmp_path: Path
) -> None:
    first = _build(release_repo)
    assert first.returncode == 0, first.stdout + first.stderr
    release = release_repo / "dist" / "release"
    first_bytes = {
        path.relative_to(release): path.read_bytes()
        for path in release.rglob("*")
        if path.is_file()
    }
    second = _build(release_repo)
    assert second.returncode == 0, second.stdout + second.stderr
    second_bytes = {
        path.relative_to(release): path.read_bytes()
        for path in release.rglob("*")
        if path.is_file()
    }
    assert second_bytes == first_bytes
    extraction = tmp_path / "extraction"
    extraction.mkdir()
    verified = _run(release_repo, str(VERIFY_SCRIPT), str(release), "--extract-to", str(extraction))
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert (extraction / "cairn-0.1.0" / "CLAUDE.md").read_text() == "release policy\n"
    assert not (extraction / "cairn-0.1.0" / "AGENTS.md").exists()


def test_release_rejects_tracked_links_other_than_root_instruction_link(release_repo: Path) -> None:
    (release_repo / "linked.txt").symlink_to("LICENSE")
    _commit(release_repo, "add unsafe link")
    result = _build(release_repo)
    assert result.returncode == 1
    assert "links are not releaseable source files" in result.stdout


def test_verifier_rejects_archive_traversal_member(tmp_path: Path) -> None:
    verifier = _load_verify_module()
    archive_path = tmp_path / "cairn-0.1.0.tar.gz"
    with archive_path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                info = tarfile.TarInfo("cairn-0.1.0/../escape.txt")
                info.size = 1
                archive.addfile(info, fileobj=io.BytesIO(b"x"))
    data = archive_path.read_bytes()
    manifest = {
        "archive": {
            "path": archive_path.name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        },
        "source": {
            "files": [
                {"path": "escape.txt", "sha256": hashlib.sha256(b"x").hexdigest(), "size": 1}
            ]
        },
    }
    with pytest.raises(verifier.ReleaseError, match="unsafe release path"):
        verifier._validate_archive(tmp_path, manifest)


def test_verifier_rejects_checksum_mutation(release_repo: Path) -> None:
    assert _build(release_repo).returncode == 0
    archive = release_repo / "dist" / "release" / "cairn-0.1.0.tar.gz"
    archive.write_bytes(archive.read_bytes() + b"mutation")
    result = _run(release_repo, str(VERIFY_SCRIPT), str(archive.parent))
    assert result.returncode == 1
    assert "checksum drift" in result.stdout


def test_build_rejects_forbidden_content_and_preserves_complete_release(release_repo: Path) -> None:
    assert _build(release_repo).returncode == 0
    archive = release_repo / "dist" / "release" / "cairn-0.1.0.tar.gz"
    before = archive.read_bytes()
    (release_repo / "customer.txt").write_text("Voice" "Verdict customer data\n", encoding="utf-8")
    _commit(release_repo, "introduce forbidden content")
    result = _build(release_repo)
    assert result.returncode == 1
    assert "customer-specific forbidden name" in result.stdout
    assert archive.read_bytes() == before
