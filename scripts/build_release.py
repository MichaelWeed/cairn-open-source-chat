#!/usr/bin/env python3
"""Build Cairn's deterministic, self-verifying source release."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

RELEASE_VERSION = "0.1.0"
ARCHIVE_NAME = f"cairn-{RELEASE_VERSION}.tar.gz"
RELEASE_ROOT = f"cairn-{RELEASE_VERSION}"
SBOM_SOURCES = (
    ("backend/sbom.cdx.json", "sbom/backend.cdx.json"),
    ("backend/sbom-firestore.cdx.json", "sbom/backend-firestore.cdx.json"),
    ("backend/sbom-gemini.cdx.json", "sbom/backend-gemini.cdx.json"),
    ("backend/sbom-hosted.cdx.json", "sbom/backend-hosted.cdx.json"),
    ("widget/sbom.cdx.json", "sbom/widget.cdx.json"),
)
EXCLUDED_PREFIXES = (".agent/", ".claude/", "dist/", "node_modules/")
EXCLUDED_NAMES = {".agent", ".claude", "dist", ".venv", "chroma", "__pycache__"}
FORBIDDEN_CONTENT = ("voice" "verdict", "talk" "alyze", "truffle" "-labz")
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
_CREDENTIAL_PATTERNS = (
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"(?:ghp|github_pat)_[A-Za-z0-9_]{20,}"),
    re.compile(rb"AIza[0-9A-Za-z_-]{30,}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(
        rb"(?:api[_-]?key|token|secret|password)\\s*[:=]\\s*[\"']?[A-Za-z0-9_-]{24,}",
        re.IGNORECASE,
    ),
)


class ReleaseError(RuntimeError):
    """Raised when a source tree is not safe to package."""


@dataclass(frozen=True)
class SourceFile:
    path: str
    data: bytes
    mode: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def _run_git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, check=False, capture_output=True, text=True
    )
    if result.returncode:
        raise ReleaseError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _canonical_json(value: object) -> bytes:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return (encoded + "\n").encode()


def _safe_relative(path: str) -> PurePosixPath:
    candidate = PurePosixPath(path)
    if not path or candidate.is_absolute() or ".." in candidate.parts or "." in candidate.parts:
        raise ReleaseError(f"unsafe tracked path: {path!r}")
    return candidate


def _is_excluded(path: str) -> bool:
    first = PurePosixPath(path).parts[0]
    return first in EXCLUDED_NAMES or path.startswith(EXCLUDED_PREFIXES)


def _ensure_release_inputs(root: Path) -> None:
    pyproject_path = root / "backend" / "pyproject.toml"
    manifest_path = root / "backend" / "app" / "capabilities.json"
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if pyproject["project"]["version"] != RELEASE_VERSION:
        raise ReleaseError("backend project version does not match the frozen release version")
    if manifest.get("release", {}).get("version") != RELEASE_VERSION:
        raise ReleaseError("capability release version does not match the frozen release version")
    license_path = root / "LICENSE"
    if not license_path.is_file():
        raise ReleaseError("missing required LICENSE")
    for source, _destination in SBOM_SOURCES:
        if not (root / source).is_file():
            raise ReleaseError(f"missing required SBOM: {source}")


def _ensure_root_instruction_link(root: Path) -> None:
    instruction = root / "AGENTS.md"
    if not instruction.is_symlink() or os.readlink(instruction) != "CLAUDE.md":
        raise ReleaseError("root AGENTS.md must be the exact AGENTS.md -> CLAUDE.md link")
    index = _run_git(root, "ls-files", "-s", "--", "AGENTS.md").split()
    if len(index) < 1 or index[0] != "120000":
        raise ReleaseError("root AGENTS.md is not tracked as the required link")
    if not (root / "CLAUDE.md").is_file() or (root / "CLAUDE.md").is_symlink():
        raise ReleaseError("regular CLAUDE.md is required")


def _contains_forbidden_content(path: str, data: bytes) -> str | None:
    lowered = data.lower()
    for name in FORBIDDEN_CONTENT:
        if name.encode() in lowered:
            return f"customer-specific forbidden name in {path}"
    for pattern in _CREDENTIAL_PATTERNS:
        if pattern.search(data):
            return f"obvious credential-like content in {path}"
    return None


def collect_source_files(root: Path) -> tuple[str, int, list[SourceFile]]:
    if _run_git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ReleaseError("release requires a clean committed checkout")
    _ensure_release_inputs(root)
    _ensure_root_instruction_link(root)
    commit = _run_git(root, "rev-parse", "HEAD").strip()
    commit_time = int(_run_git(root, "show", "-s", "--format=%ct", "HEAD").strip())
    tracked = sorted(item for item in _run_git(root, "ls-files", "-z").split("\0") if item)
    files: list[SourceFile] = []
    total = 0
    for relative in tracked:
        _safe_relative(relative)
        if relative == "AGENTS.md":
            continue
        if _is_excluded(relative):
            continue
        source = root / relative
        source_stat = source.lstat()
        if stat.S_ISLNK(source_stat.st_mode):
            raise ReleaseError(f"links are not releaseable source files: {relative}")
        if not stat.S_ISREG(source_stat.st_mode):
            raise ReleaseError(f"non-regular tracked file: {relative}")
        if source_stat.st_size > MAX_MEMBER_BYTES:
            raise ReleaseError(f"source member exceeds size bound: {relative}")
        data = source.read_bytes()
        if str(root).encode() in data:
            raise ReleaseError(f"absolute checkout path in {relative}")
        if forbidden := _contains_forbidden_content(relative, data):
            raise ReleaseError(forbidden)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ReleaseError("source inventory exceeds total size bound")
        mode = 0o755 if source_stat.st_mode & stat.S_IXUSR else 0o644
        files.append(SourceFile(relative, data, mode))
    if not files:
        raise ReleaseError("release inventory is empty")
    return commit, commit_time, files


def _write_archive(path: Path, files: list[SourceFile], commit_time: int) -> None:
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=commit_time) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                for source in files:
                    member = tarfile.TarInfo(f"{RELEASE_ROOT}/{source.path}")
                    member.size = len(source.data)
                    member.mode = source.mode
                    member.mtime = commit_time
                    member.uid = 0
                    member.gid = 0
                    member.uname = ""
                    member.gname = ""
                    archive.addfile(member, io.BytesIO(source.data))


def _file_record(path: str, data: bytes) -> dict[str, object]:
    return {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def _require_real_directory(path: Path, label: str) -> None:
    try:
        details = path.lstat()
    except FileNotFoundError as error:
        raise ReleaseError(f"missing output directory: {label}") from error
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise ReleaseError(f"output directory must be a real directory: {label}")


def _release_output_directory(root: Path) -> Path:
    _require_real_directory(root, "repository root")
    dist = root / "dist"
    if dist.exists() or dist.is_symlink():
        _require_real_directory(dist, "dist")
    else:
        dist.mkdir()
        _require_real_directory(dist, "dist")
    release_dir = dist / "release"
    if release_dir.exists() or release_dir.is_symlink():
        _require_real_directory(release_dir, "dist/release")
    else:
        release_dir.mkdir()
        _require_real_directory(release_dir, "dist/release")
    return release_dir


def _replace_complete_release(staging: Path, release_dir: Path) -> None:
    targets = (ARCHIVE_NAME, "release-manifest.json", "SHA256SUMS", "sbom")
    backups: list[tuple[Path, Path]] = []
    installed: list[Path] = []
    try:
        for name in targets:
            target = release_dir / name
            if target.exists() or target.is_symlink():
                backup = release_dir / f".previous-{name}-{os.getpid()}"
                if backup.exists() or backup.is_symlink():
                    raise ReleaseError(f"refusing to overwrite staging collision: {backup.name}")
                os.replace(target, backup)
                backups.append((target, backup))
            os.replace(staging / name, target)
            installed.append(target)
    except (OSError, ReleaseError) as error:
        for target in reversed(installed):
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()
        for target, backup in reversed(backups):
            if backup.exists() or backup.is_symlink():
                os.replace(backup, target)
        raise ReleaseError(f"could not install complete release: {error}") from error
    for _target, backup in backups:
        if backup.is_dir():
            shutil.rmtree(backup)
        elif backup.exists() or backup.is_symlink():
            backup.unlink()


def build_release(root: Path) -> Path:
    commit, commit_time, files = collect_source_files(root)
    release_dir = _release_output_directory(root)
    with tempfile.TemporaryDirectory(prefix=".staging-", dir=release_dir) as temporary:
        staging = Path(temporary)
        archive_path = staging / ARCHIVE_NAME
        _write_archive(archive_path, files, commit_time)
        sbom_records: list[dict[str, object]] = []
        sbom_dir = staging / "sbom"
        sbom_dir.mkdir()
        for source, destination in SBOM_SOURCES:
            data = (root / source).read_bytes()
            copied = staging / destination
            copied.write_bytes(data)
            sbom_records.append(_file_record(destination, data))
        source_records = [_file_record(item.path, item.data) for item in files]
        license_record = next(record for record in source_records if record["path"] == "LICENSE")
        manifest = {
            "schema_version": 1,
            "release": {
                "name": "cairn",
                "source_commit": commit,
                "source_commit_time": commit_time,
                "version": RELEASE_VERSION,
            },
            "archive": _file_record(ARCHIVE_NAME, archive_path.read_bytes()),
            "source": {"root": RELEASE_ROOT, "files": source_records},
            "sboms": sbom_records,
            "license": license_record,
        }
        manifest_path = staging / "release-manifest.json"
        manifest_path.write_bytes(_canonical_json(manifest))
        checksums = [
            _file_record(ARCHIVE_NAME, archive_path.read_bytes()),
            _file_record("release-manifest.json", manifest_path.read_bytes()),
        ]
        checksums.extend(sbom_records)
        (staging / "SHA256SUMS").write_text(
            "".join(f"{record['sha256']}  {record['path']}\n" for record in checksums),
            encoding="ascii",
        )
        _replace_complete_release(staging, release_dir)
    return release_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        root = args.root if args.root.is_absolute() else Path.cwd() / args.root
        release_dir = build_release(root)
    except ReleaseError as error:
        print(f"release build failed: {error}")
        return 1
    print(f"release built: {release_dir / ARCHIVE_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
