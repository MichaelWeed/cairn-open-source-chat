# Compatibility and capability discovery

Cairn publishes static, validated package metadata at `GET /api/v1/capabilities`.
The response is identical to `backend/app/capabilities.json`; it does not inspect
the environment, test credentials, probe providers, or expose corpus content and
paths. Readiness remains available separately at `GET /readyz`.

The current developer-preview values are recorded below. This JSON block is checked
against the packaged manifest in the backend test suite so documentation drift fails
validation.

<!-- capabilities-manifest:start -->
```json
{
  "schema_version": "1.0",
  "release": {
    "stage": "developer-preview",
    "version": "0.0.0"
  },
  "compatibility": {
    "chat_api": "1.0",
    "sse_events": "1.1",
    "widget": "0.1.0",
    "local_retrieval_store": "1",
    "local_corpus": "1"
  },
  "capabilities": {
    "providers": {
      "ollama": "available",
      "echo": "development_only",
      "gemini": "planned"
    },
    "embeddings": {
      "ollama": "available",
      "fake": "development_only",
      "hosted": "planned"
    },
    "retrieval": {
      "local_sqlite_flat": "available",
      "hosted_durable": "planned"
    },
    "corpus": {
      "local_directory": "available",
      "reviewed_manifest": "planned",
      "immutable_versions": "planned"
    },
    "widget": {
      "custom_element": "available",
      "production_configuration": "planned"
    },
    "safety": {
      "origin_allowlist": "available",
      "single_instance_rate_limits": "available",
      "deployment_wide_controls": "planned"
    },
    "operations": {
      "liveness": "available",
      "local_readiness": "available",
      "capability_discovery": "available",
      "hosted_readiness": "planned"
    }
  }
}
```
<!-- capabilities-manifest:end -->

## Upgrade rules before 1.0

- A patch release repairs documentation or implementation without changing a
  reported contract.
- A minor release may add a capability, field, provider, or adapter that existing
  consumers can ignore.
- A major release is required for removal, semantic reinterpretation, or an
  incompatible request, event, or schema change.
- Pre-1.0 compatibility is explicit: a breaking change updates the affected
  compatibility value and includes migration notes. Moving aliases are not versions.

Capability state is descriptive, not a runtime health signal. `available` means the
feature is built for its documented path, `development_only` identifies deterministic
development/test implementations, and `planned` means callers must not depend on it.

## Chat compatibility 1.0 and SSE compatibility 1.1

The public chat request keeps the same four keys and does not require a version
field. KAN-46a adds validation bounds without changing successful envelopes. SSE
1.1 adds error codes for invalid requests, budgets, concurrency, timeouts,
retrieval, and cancellation, plus `limit` and `cancelled` done reasons. Existing
event names and payload fields remain compatible.
