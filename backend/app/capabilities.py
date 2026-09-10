"""Validated, static capability metadata shipped with Cairn."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

CapabilityState = Literal["available", "development_only", "planned"]


class CapabilityModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Release(CapabilityModel):
    stage: Literal["developer-preview"]
    version: Literal["0.0.0"]


class Compatibility(CapabilityModel):
    chat_api: Literal["1.0"]
    sse_events: Literal["1.1"]
    widget: Literal["0.1.0"]
    local_retrieval_store: Literal["1"]
    local_corpus: Literal["2"]
    provider_accounting: Literal["1.0"]


class Providers(CapabilityModel):
    ollama: CapabilityState
    echo: CapabilityState
    gemini: CapabilityState


class Embeddings(CapabilityModel):
    ollama: CapabilityState
    fake: CapabilityState
    hosted: CapabilityState


class Retrieval(CapabilityModel):
    local_sqlite_flat: CapabilityState
    hosted_durable: CapabilityState


class Corpus(CapabilityModel):
    local_directory: CapabilityState
    reviewed_manifest: CapabilityState
    immutable_versions: CapabilityState


class Widget(CapabilityModel):
    custom_element: CapabilityState
    production_configuration: CapabilityState


class Safety(CapabilityModel):
    origin_allowlist: CapabilityState
    single_instance_rate_limits: CapabilityState
    deployment_wide_controls: CapabilityState


class Operations(CapabilityModel):
    liveness: CapabilityState
    local_readiness: CapabilityState
    capability_discovery: CapabilityState
    hosted_readiness: CapabilityState
    provider_usage_cost: CapabilityState


class Capabilities(CapabilityModel):
    providers: Providers
    embeddings: Embeddings
    retrieval: Retrieval
    corpus: Corpus
    widget: Widget
    safety: Safety
    operations: Operations


class CapabilityManifest(CapabilityModel):
    schema_version: Literal["1.1"]
    release: Release
    compatibility: Compatibility
    capabilities: Capabilities


_MANIFEST_PATH = Path(__file__).with_name("capabilities.json")
_MANIFEST = CapabilityManifest.model_validate_json(_MANIFEST_PATH.read_text(encoding="utf-8"))


def capability_manifest() -> dict[str, object]:
    """Return a new JSON-safe representation of the validated package data."""
    return _MANIFEST.model_dump(mode="json")
