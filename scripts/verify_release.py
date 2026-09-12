#!/usr/bin/env python3
"""Verify and, when requested, safely extract a Cairn source release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from build_release import (
    ARCHIVE_NAME,
    MAX_MEMBER_BYTES,
    MAX_TOTAL_BYTES,
    RELEASE_ROOT,
    RELEASE_VERSION,
    SBOM_SOURCES,
    ReleaseError,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_TOP_LEVEL = {"schema_version", "release", "archive", "source", "sboms", "license"}


def _safe_path(path: str) -> PurePosixPath:
    parsed = PurePosixPath(path)
    if not path or parsed.is_absolute() or "." in parsed.parts or ".." in parsed.parts:
        raise ReleaseError(f"unsafe release path: {path!r}")
    return parsed


def _regular_file(path: Path) -> bytes:
    try:
        details = path.lstat()
    except FileNotFoundError as error:
        raise ReleaseError(f"missing release file: {path.name}") from error
    if not stat.S_ISREG(details.st_mode):
        raise ReleaseError(f"release file is not regular: {path.name}")
    if details.st_size > MAX_TOTAL_BYTES:
        raise ReleaseError(f"release file exceeds size bound: {path.name}")
    return path.read_bytes()


def _record(value: Any, expected_path: str | None = None) -> tuple[str, str, int]:
    if not isinstance(value, dict) or set(value) != {"path", "sha256", "size"}:
        raise ReleaseError("invalid manifest file record")
    path, digest, size = value["path"], value["sha256"], value["size"]
    if not isinstance(path, str) or not isinstance(digest, str) or not isinstance(size, int):
        raise ReleaseError("invalid manifest file record types")
    _safe_path(path)
    if expected_path is not None and path != expected_path:
        raise ReleaseError(f"unexpected manifest path: {path}")
    if not _SHA256.fullmatch(digest) or size < 0 or size > MAX_MEMBER_BYTES:
        raise ReleaseError(f"invalid manifest bounds for {path}")
    return path, digest, size


def _parse_manifest(data: bytes) -> dict[str, Any]:
    try:
        manifest = json.loads(data)
    except json.JSONDecodeError as error:
        raise ReleaseError("release manifest is not valid JSON") from error
    if not isinstance(manifest, dict) or set(manifest) != _REQUIRED_TOP_LEVEL:
        raise ReleaseError("release manifest has an unexpected schema")
    if manifest["schema_version"] != 1:
        raise ReleaseError("unsupported release manifest schema")
    release = manifest["release"]
    if not isinstance(release, dict) or set(release) != {
        "name",
        "source_commit",
        "source_commit_time",
        "version",
    }:
        raise ReleaseError("invalid release metadata")
    if release["name"] != "cairn" or release["version"] != RELEASE_VERSION:
        raise ReleaseError("release identity does not match the frozen interface")
    if not isinstance(release["source_commit"], str) or not re.fullmatch(
        r"[0-9a-f]{40}", release["source_commit"]
    ):
        raise ReleaseError("invalid source commit")
    if not isinstance(release["source_commit_time"], int) or release["source_commit_time"] < 0:
        raise ReleaseError("invalid source commit time")
    _record(manifest["archive"], ARCHIVE_NAME)
    source = manifest["source"]
    if (
        not isinstance(source, dict)
        or set(source) != {"root", "files"}
        or source["root"] != RELEASE_ROOT
    ):
        raise ReleaseError("invalid source inventory")
    if not isinstance(source["files"], list) or not source["files"]:
        raise ReleaseError("source inventory is empty")
    seen = set()
    for value in source["files"]:
        path, _digest, _size = _record(value)
        if path in seen:
            raise ReleaseError(f"duplicate source inventory path: {path}")
        seen.add(path)
    expected_sboms = [destination for _source, destination in SBOM_SOURCES]
    if not isinstance(manifest["sboms"], list) or len(manifest["sboms"]) != len(expected_sboms):
        raise ReleaseError("missing release SBOM records")
    actual_sboms = [_record(value)[0] for value in manifest["sboms"]]
    if actual_sboms != expected_sboms:
        raise ReleaseError("release SBOM inventory does not match the frozen interface")
    license_path, _license_hash, _license_size = _record(manifest["license"], "LICENSE")
    if license_path not in seen:
        raise ReleaseError("license is absent from the source inventory")
    source_license = next(record for record in source["files"] if record["path"] == "LICENSE")
    if manifest["license"] != source_license:
        raise ReleaseError("license record does not match the source inventory")
    source_sboms = {source: destination for source, destination in SBOM_SOURCES}
    source_records = {record["path"]: record for record in source["files"]}
    for sbom in manifest["sboms"]:
        copied_path, copied_hash, copied_size = _record(sbom)
        source_path = next(
            source for source, destination in source_sboms.items() if destination == copied_path
        )
        source_record = source_records.get(source_path)
        if source_record is None or (copied_hash, copied_size) != (
            source_record["sha256"],
            source_record["size"],
        ):
            raise ReleaseError("copied SBOM does not match the source inventory")
    return manifest


def _validate_checksums(release_dir: Path, manifest: dict[str, Any], manifest_data: bytes) -> None:
    checksum_data = _regular_file(release_dir / "SHA256SUMS")
    try:
        lines = checksum_data.decode("ascii").splitlines()
    except UnicodeDecodeError as error:
        raise ReleaseError("SHA256SUMS is not ASCII") from error
    manifest_record = {
        "path": "release-manifest.json",
        "sha256": hashlib.sha256(manifest_data).hexdigest(),
        "size": len(manifest_data),
    }
    expected = [manifest["archive"], manifest_record, *manifest["sboms"]]
    parsed: list[tuple[str, str]] = []
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\s]+)", line)
        if match is None:
            raise ReleaseError("invalid SHA256SUMS line")
        parsed.append((match.group(2), match.group(1)))
    expected_pairs = [(item["path"], item["sha256"]) for item in expected]
    if parsed != expected_pairs:
        raise ReleaseError("SHA256SUMS does not bind the complete release")
    for item in expected:
        path, digest, size = _record(item)
        data = _regular_file(release_dir / path)
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise ReleaseError(f"checksum drift: {path}")


def _validate_archive(
    release_dir: Path, manifest: dict[str, Any]
) -> list[tuple[tarfile.TarInfo, bytes]]:
    archive_data = _regular_file(release_dir / ARCHIVE_NAME)
    archive_path, digest, size = _record(manifest["archive"], ARCHIVE_NAME)
    if len(archive_data) != size or hashlib.sha256(archive_data).hexdigest() != digest:
        raise ReleaseError(f"checksum drift: {archive_path}")
    expected = {
        record["path"]: (record["sha256"], record["size"])
        for record in manifest["source"]["files"]
    }
    members: list[tuple[tarfile.TarInfo, bytes]] = []
    seen_paths: set[str] = set()
    total = 0
    try:
        with tarfile.open(release_dir / ARCHIVE_NAME, mode="r:gz") as archive:
            for member in archive:
                if not member.isreg() or member.issym() or member.islnk():
                    raise ReleaseError(f"archive member is not a regular file: {member.name}")
                name = _safe_path(member.name)
                if len(name.parts) < 2 or name.parts[0] != RELEASE_ROOT:
                    raise ReleaseError(f"archive member is outside the release root: {member.name}")
                source_path = str(PurePosixPath(*name.parts[1:]))
                if source_path in seen_paths:
                    raise ReleaseError(f"duplicate archive member: {source_path}")
                seen_paths.add(source_path)
                if member.size < 0 or member.size > MAX_MEMBER_BYTES:
                    raise ReleaseError(f"archive member exceeds size bound: {source_path}")
                total += member.size
                if total > MAX_TOTAL_BYTES:
                    raise ReleaseError("archive exceeds total size bound")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ReleaseError(f"archive member cannot be read: {source_path}")
                data = extracted.read(MAX_MEMBER_BYTES + 1)
                if len(data) != member.size:
                    raise ReleaseError(f"archive member size drift: {source_path}")
                item = expected.get(source_path)
                if item is None or item != (hashlib.sha256(data).hexdigest(), len(data)):
                    raise ReleaseError(f"archive inventory mismatch: {source_path}")
                members.append((member, data))
    except (tarfile.TarError, OSError) as error:
        raise ReleaseError("archive cannot be read safely") from error
    actual = {str(PurePosixPath(*_safe_path(member.name).parts[1:])) for member, _data in members}
    if actual != set(expected):
        raise ReleaseError("archive does not match the complete source inventory")
    return members


def verify_release(release_dir: Path) -> tuple[dict[str, Any], list[tuple[tarfile.TarInfo, bytes]]]:
    if release_dir.is_symlink() or not release_dir.is_dir():
        raise ReleaseError("release directory must be a real directory")
    manifest_data = _regular_file(release_dir / "release-manifest.json")
    manifest = _parse_manifest(manifest_data)
    _validate_checksums(release_dir, manifest, manifest_data)
    return manifest, _validate_archive(release_dir, manifest)


def extract_release(destination: Path, members: list[tuple[tarfile.TarInfo, bytes]]) -> Path:
    if destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):
        raise ReleaseError("extraction destination must be an existing empty directory")
    with tempfile.TemporaryDirectory(prefix=".cairn-extract-", dir=destination) as temporary:
        stage = Path(temporary) / RELEASE_ROOT
        stage.mkdir()
        for member, data in members:
            relative = PurePosixPath(*_safe_path(member.name).parts[1:])
            target = stage.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                raise ReleaseError(f"duplicate extraction target: {relative}")
            with target.open("xb") as handle:
                handle.write(data)
            os.chmod(target, member.mode & 0o777)
        final = destination / RELEASE_ROOT
        os.replace(stage, final)
    return final


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("--extract-to", type=Path)
    args = parser.parse_args()
    try:
        release_dir = args.release_dir
        if not release_dir.is_absolute():
            release_dir = Path.cwd() / release_dir
        manifest, members = verify_release(release_dir)
        if args.extract_to is not None:
            destination = args.extract_to
            if not destination.is_absolute():
                destination = Path.cwd() / destination
            extracted = extract_release(destination, members)
            print(f"release verified and extracted: {extracted}")
        else:
            print(f"release verified: cairn {manifest['release']['version']}")
    except ReleaseError as error:
        print(f"release verification failed: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
