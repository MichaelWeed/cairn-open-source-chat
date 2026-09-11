import hashlib
import json
import re
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.capabilities import CapabilityManifest, capability_manifest
from app.config import Settings
from app.main import create_app
from app.vectorstore import SCHEMA_VERSION

REPOSITORY_ROOT = Path(__file__).parents[2]
MANIFEST_PATH = REPOSITORY_ROOT / "backend" / "app" / "capabilities.json"
COMPATIBILITY_DOC = REPOSITORY_ROOT / "docs" / "COMPATIBILITY.md"

ACCEPTED_BASE_MANIFEST: dict[str, Any] = {
    "schema_version": "1.1",
    "release": {"stage": "developer-preview", "version": "0.0.0"},
    "compatibility": {
        "chat_api": "1.0",
        "sse_events": "1.1",
        "widget": "0.2.0",
        "local_retrieval_store": "1",
        "local_corpus": "2",
        "provider_accounting": "1.0",
    },
    "capabilities": {
        "providers": {
            "ollama": "available",
            "echo": "development_only",
            "gemini": "available",
        },
        "embeddings": {
            "ollama": "available",
            "fake": "development_only",
            "hosted": "planned",
        },
        "retrieval": {
            "local_sqlite_flat": "available",
            "hosted_durable": "development_only",
        },
        "corpus": {
            "local_directory": "available",
            "reviewed_manifest": "available",
            "immutable_versions": "development_only",
        },
        "widget": {
            "custom_element": "available",
            "production_configuration": "available",
        },
        "safety": {
            "origin_allowlist": "available",
            "single_instance_rate_limits": "available",
            "deployment_wide_controls": "planned",
        },
        "operations": {
            "liveness": "available",
            "local_readiness": "available",
            "capability_discovery": "available",
            "hosted_readiness": "planned",
            "provider_usage_cost": "available",
        },
    },
}

EXPECTED_MANIFEST: dict[str, Any] = {
    **ACCEPTED_BASE_MANIFEST,
    "capabilities": {
        **ACCEPTED_BASE_MANIFEST["capabilities"],
        "corpus": {
            **ACCEPTED_BASE_MANIFEST["capabilities"]["corpus"],
            "local_directory": "development_only",
        },
        "operations": {
            **ACCEPTED_BASE_MANIFEST["capabilities"]["operations"],
            "hosted_readiness": "development_only",
        },
    },
}


def _changed_json_leaves(before: Any, after: Any, pointer: str = "") -> set[str]:
    if isinstance(before, dict) and isinstance(after, dict):
        if before.keys() != after.keys():
            return {pointer}
        return {
            changed
            for key in before
            for changed in _changed_json_leaves(before[key], after[key], f"{pointer}/{key}")
        }
    return {pointer} if before != after else set()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(database_path=tmp_path / "test.db", chroma_path=tmp_path / "chroma")
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_endpoint_matches_packaged_manifest_exactly(client: TestClient) -> None:
    asset = json.loads(MANIFEST_PATH.read_text())

    response = client.get("/api/v1/capabilities")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == asset == EXPECTED_MANIFEST
    assert len(response.content) == 985
    assert (
        hashlib.sha256(response.content).hexdigest()
        == "f1f4efc723038a69bf030b5a776786c5a5cb0a44bf46cb28712f36b8d717f980"
    )


def test_packaged_manifest_changes_only_accepted_capability_leaves() -> None:
    asset_bytes = MANIFEST_PATH.read_bytes()
    asset = json.loads(asset_bytes)

    assert (
        hashlib.sha256(asset_bytes).hexdigest()
        == "570c0cb3334f7720bf3c43eea8dbd760c96c52a2972dc7c0fb7e52b72f03f61e"
    )
    assert _changed_json_leaves(ACCEPTED_BASE_MANIFEST, asset) == {
        "/capabilities/corpus/local_directory",
        "/capabilities/operations/hosted_readiness",
    }


@pytest.mark.parametrize(
    "invalid_manifest",
    [
        {**EXPECTED_MANIFEST, "unexpected": "field"},
        {
            **EXPECTED_MANIFEST,
            "release": {**EXPECTED_MANIFEST["release"], "unexpected": "field"},
        },
        {
            **EXPECTED_MANIFEST,
            "capabilities": {
                **EXPECTED_MANIFEST["capabilities"],
                "providers": {
                    **EXPECTED_MANIFEST["capabilities"]["providers"],
                    "unexpected": "available",
                },
            },
        },
        {
            **EXPECTED_MANIFEST,
            "capabilities": {
                **EXPECTED_MANIFEST["capabilities"],
                "providers": {
                    **EXPECTED_MANIFEST["capabilities"]["providers"],
                    "ollama": "sometimes",
                },
            },
        },
    ],
)
def test_manifest_schema_rejects_unknown_keys_and_invalid_states(
    invalid_manifest: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        CapabilityManifest.model_validate(invalid_manifest)


def test_manifest_callers_receive_fresh_json_safe_data() -> None:
    first = capability_manifest()
    cast(dict[str, Any], first["release"])["stage"] = "mutated"

    assert capability_manifest() == EXPECTED_MANIFEST


def test_endpoint_is_static_and_content_free(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = "not-for-capability-output"
    private_path = str(tmp_path / "private-corpus")
    monkeypatch.setenv("PROVIDER_API_KEY", secret)
    monkeypatch.setenv("CAIRN_CORPUS_PATH", private_path)

    response = client.get(
        "/api/v1/capabilities?requested_provider=private",
        headers={"X-Capability-Probe": "request-derived-value"},
    )
    encoded = response.text

    assert response.json() == EXPECTED_MANIFEST
    assert secret not in encoded
    assert private_path not in encoded
    assert "request-derived-value" not in encoded


def test_reported_versions_match_repository_sources() -> None:
    pyproject = tomllib.loads((REPOSITORY_ROOT / "backend" / "pyproject.toml").read_text())
    widget_source = (REPOSITORY_ROOT / "widget" / "src" / "index.ts").read_text()
    widget_version = re.search(r'CAIRN_WIDGET_VERSION = "([^"]+)"', widget_source)

    assert EXPECTED_MANIFEST["release"]["version"] == pyproject["project"]["version"]
    assert widget_version is not None
    assert EXPECTED_MANIFEST["compatibility"]["widget"] == widget_version.group(1)
    assert EXPECTED_MANIFEST["compatibility"]["local_retrieval_store"] == str(SCHEMA_VERSION)


def test_compatibility_document_manifest_matches_asset() -> None:
    document = COMPATIBILITY_DOC.read_text()
    snapshot = document.split("<!-- capabilities-manifest:start -->", 1)[1].split(
        "<!-- capabilities-manifest:end -->", 1
    )[0]
    json_block = snapshot.split("```json", 1)[1].split("```", 1)[0]

    assert json.loads(json_block) == EXPECTED_MANIFEST
    assert "docs/COMPATIBILITY.md" in (REPOSITORY_ROOT / "README.md").read_text()
    assert "docs/COMPATIBILITY.md" in (REPOSITORY_ROOT / "DEVELOPER_README.md").read_text()
