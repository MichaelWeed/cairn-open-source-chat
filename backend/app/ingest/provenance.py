"""Strict provenance validation for an operator-mounted startup corpus."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

MANIFEST_FILENAME = "provenance.json"
MANIFEST_VERSION = 1
MAX_MANIFEST_BYTES = 1_048_576
MAX_CITATION_TITLE_CHARS = 160
ENTRY_FIELDS = frozenset({"title", "url", "sha256", "owner", "reviewed_at", "public"})
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class CorpusProvenanceError(ValueError):
    """Raised when a startup corpus provenance manifest is not trustworthy."""


class _DuplicateJsonKeyError(ValueError):
    pass


@dataclass(frozen=True)
class SourceProvenance:
    title: str
    url: str
    sha256: str
    owner: str
    reviewed_at: date


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _required_text(entry: dict[str, object], field: str, relative_path: str) -> str:
    value = entry[field]
    if not isinstance(value, str) or not value.strip():
        raise CorpusProvenanceError(
            f"provenance entry {relative_path} field {field} must be a non-empty string"
        )
    return value.strip()


def _validated_relative_path(relative_path: str) -> str:
    parts = relative_path.split("/")
    if (
        not relative_path
        or relative_path.startswith("/")
        or "\\" in relative_path
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise CorpusProvenanceError(
            f"provenance document key is not a stable relative path: {relative_path!r}"
        )
    return relative_path


def _validated_url(value: str, relative_path: str) -> str:
    message = f"provenance entry {relative_path} URL must use an absolute HTTP(S) URL"
    if any(character.isspace() for character in value):
        raise CorpusProvenanceError(message)
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as error:
        raise CorpusProvenanceError(message) from error
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise CorpusProvenanceError(message)
    return value


def _parse_entry(relative_path: str, value: object) -> SourceProvenance:
    if not isinstance(value, dict) or set(value) != ENTRY_FIELDS:
        raise CorpusProvenanceError(
            f"provenance entry {relative_path} must contain exactly the required fields"
        )
    entry: dict[str, object] = value
    if entry["public"] is not True:
        raise CorpusProvenanceError(f"provenance entry {relative_path} is not public")

    title = _required_text(entry, "title", relative_path)
    if len(title) > MAX_CITATION_TITLE_CHARS:
        raise CorpusProvenanceError(
            f"provenance entry {relative_path} title must be at most "
            f"{MAX_CITATION_TITLE_CHARS} Unicode characters"
        )
    url = _validated_url(_required_text(entry, "url", relative_path), relative_path)
    owner = _required_text(entry, "owner", relative_path)
    sha256 = _required_text(entry, "sha256", relative_path)
    if SHA256_PATTERN.fullmatch(sha256) is None:
        raise CorpusProvenanceError(
            f"provenance entry {relative_path} sha256 must be 64 lowercase hexadecimal characters"
        )

    reviewed_text = _required_text(entry, "reviewed_at", relative_path)
    try:
        reviewed_at = date.fromisoformat(reviewed_text)
    except ValueError as error:
        raise CorpusProvenanceError(
            f"provenance entry {relative_path} reviewed_at must be YYYY-MM-DD"
        ) from error
    if reviewed_at.isoformat() != reviewed_text:
        raise CorpusProvenanceError(
            f"provenance entry {relative_path} reviewed_at must be YYYY-MM-DD"
        )

    return SourceProvenance(
        title=title,
        url=url,
        sha256=sha256,
        owner=owner,
        reviewed_at=reviewed_at,
    )


def load_provenance_manifest(
    corpus_path: Path, documents: dict[str, bytes]
) -> dict[str, SourceProvenance]:
    """Load and validate the complete startup manifest before ingestion begins."""
    manifest_path = corpus_path / MANIFEST_FILENAME
    if manifest_path.is_symlink():
        raise CorpusProvenanceError("provenance manifest must not be a symbolic link")
    try:
        encoded = manifest_path.read_bytes()
    except FileNotFoundError as error:
        raise CorpusProvenanceError(
            f"configured corpus requires {MANIFEST_FILENAME} at its root"
        ) from error
    except OSError as error:
        raise CorpusProvenanceError(f"could not read provenance manifest: {error}") from error

    if len(encoded) > MAX_MANIFEST_BYTES:
        raise CorpusProvenanceError(
            f"provenance manifest exceeds the {MAX_MANIFEST_BYTES}-byte limit"
        )
    try:
        decoded = json.loads(encoded.decode("utf-8"), object_pairs_hook=_strict_object)
    except UnicodeDecodeError as error:
        raise CorpusProvenanceError("provenance manifest must be UTF-8") from error
    except _DuplicateJsonKeyError as error:
        raise CorpusProvenanceError(f"provenance manifest contains a {error}") from error
    except json.JSONDecodeError as error:
        raise CorpusProvenanceError("provenance manifest must contain valid JSON") from error

    if not isinstance(decoded, dict) or set(decoded) != {"version", "documents"}:
        raise CorpusProvenanceError(
            "provenance manifest must contain exactly version and documents"
        )
    if type(decoded["version"]) is not int or decoded["version"] != MANIFEST_VERSION:
        raise CorpusProvenanceError(
            f"provenance manifest version must be {MANIFEST_VERSION}"
        )
    manifest_documents = decoded["documents"]
    if not isinstance(manifest_documents, dict):
        raise CorpusProvenanceError("provenance manifest documents must be an object")

    parsed: dict[str, SourceProvenance] = {}
    for raw_path, entry in manifest_documents.items():
        relative_path = _validated_relative_path(raw_path)
        parsed[relative_path] = _parse_entry(relative_path, entry)

    expected_paths = set(documents)
    manifested_paths = set(parsed)
    missing = sorted(expected_paths - manifested_paths)
    if missing:
        raise CorpusProvenanceError(
            f"missing provenance entries for: {', '.join(missing)}"
        )
    unlisted = sorted(manifested_paths - expected_paths)
    if unlisted:
        raise CorpusProvenanceError(
            f"unlisted provenance entries for: {', '.join(unlisted)}"
        )

    for relative_path, content in documents.items():
        actual_hash = hashlib.sha256(content).hexdigest()
        if parsed[relative_path].sha256 != actual_hash:
            raise CorpusProvenanceError(f"provenance hash mismatch for {relative_path}")
    return parsed
