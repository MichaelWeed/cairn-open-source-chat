# Migrating a 0.0.0 preview to Cairn 0.1.0

Use this guide for an operator-owned preview instance. It moves one local,
single-instance deployment to the 0.1.0 source release; it does not create a
hosted service, multi-instance topology, or production certification.

Complete the release verification and safe extraction steps in
[RELEASE.md](RELEASE.md) before changing a running preview instance.

| Operator action | 0.1.0 requirement and decision |
| --- | --- |
| Record the existing state | Save the current release version, configuration, widget embed, corpus manifest, and a successful `GET /api/v1/capabilities` response. The endpoint is package metadata, not a health check; record `/readyz` separately. |
| Back up before change | Stop the old stack. Back up the reviewed corpus directory and `provenance.json`, then take one matched snapshot of the metadata SQLite database and `CHROMA_PATH/cairn-vectors-v1.sqlite3` in `cairn_data`. Do not copy live SQLite files. |
| Verify the 0.1.0 source release | Require successful `sha256sum --check SHA256SUMS`, `python3 scripts/verify_release.py dist/release`, and verifier-led extraction. Retain the verified old tree and its configuration until the new instance passes acceptance. |
| Discover 0.1.0 capabilities | After start, require `release.version` `0.1.0`, stage `developer-preview`, chat API `1.0`, SSE `1.1`, and widget compatibility `0.2.0` from `/api/v1/capabilities`. Treat a mismatch as a stop condition, not a field to edit. |
| Reconcile corpus provenance | Local corpus compatibility 2 requires a version 1 `provenance.json` with every Markdown or PDF document's reviewed public title, canonical HTTP(S) URL, exact-byte SHA-256, owner, review date, and `public: true`. Use regular files and mount them read-only. Re-ingest explicitly; legacy Chroma persistence is not read or migrated. |
| Select providers and retrieval | Use local Ollama for generation and embeddings on the supported path. Gemini is optional generation only and needs its optional image profile plus a runtime-only key; embeddings remain Ollama. Firestore retrieval is development-only, not a 0.1.0 production choice. |
| Recheck browser access | Configure `ORIGIN_ALLOWLIST` with each exact widget site origin. Upgrade embeds to the 0.2.0 configuration contract in [WIDGET.md](WIDGET.md): its `api-url` has no credentials, query, or fragment, and its capability negotiation must succeed before chat is enabled. |
| Keep public controls in their boundary | The deployment-wide `PUBLIC_*` controls are development-only. They require an external conforming shared-store adapter that the source release does not provide; they are not a deployment certification or provider billing limit. |
| Start and accept | Run `make live`, require `/readyz` to return `status: "ok"` with every public check true, exercise `/demo` or an allowlisted widget with one cited corpus answer, then verify refusal for an unrelated question. |
| Roll back if acceptance fails | Run `make live-down`, restore the preserved corpus and matching SQLite snapshot, return to the previous verified tree and configuration, start it, and repeat the old acceptance checks. Keep the 0.1.0 evidence for diagnosis; do not delete it as part of rollback. |

After migration, schedule the weekly `make verify` rescan and review the copied
SBOMs shipped in the verified release. The single-instance local SQLite design
does not support sharing the metadata or vector files between hosts or replicas.
