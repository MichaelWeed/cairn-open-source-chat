# Cairn 0.1.0 operator release guide

Cairn 0.1.0 is a source-only developer preview for operator-owned,
single-instance self-hosting. It is not an author-hosted service and it is not
production-certified. The operator supplies the host, network boundary, corpus,
backups, and model provider. Local SQLite metadata and vector storage with real
Ollama embeddings are the supported deployable path. Gemini generation is an
explicit optional configuration; Firestore retrieval is development-only.

## 1. Obtain and verify the release

Obtain the `dist/release/` directory from the source-distribution channel you
trust. It must contain these release artifacts:

* `cairn-0.1.0.tar.gz`
* `release-manifest.json`
* `SHA256SUMS`
* `sbom/` containing the five copied CycloneDX SBOMs

Prerequisites are Python 3, a SHA-256 command that supports `--check`, Docker
or Podman with Compose, and an operator-selected corpus. Verify before
extracting:

```sh
(cd dist/release && sha256sum --check SHA256SUMS)
python3 scripts/verify_release.py dist/release
```

Use the verifier, rather than `tar`, for extraction. It verifies the release
again and rejects an unsafe archive layout. Supply an existing empty directory:

```sh
mkdir cairn-extract
python3 scripts/verify_release.py dist/release --extract-to cairn-extract
cd cairn-extract/cairn-0.1.0
```

The extracted tree is the source distribution. It builds the operator's local
containers; it does not install an author-operated service or certify a
production environment.

## 2. Configure and start the supported path

Choose a regular-file corpus directory with at least one non-empty Markdown or
PDF document and a version 1 `provenance.json` that covers every document. Keep
the corpus read-only to Cairn. See [CORPUS-PROVENANCE.md](CORPUS-PROVENANCE.md)
for the manifest schema and hash procedure.

Set the corpus and every site origin that will embed the widget, then start:

```sh
CAIRN_CORPUS_PATH=<absolute-corpus-directory> \
ORIGIN_ALLOWLIST=https://support.example \
make live
```

`make live` selects Docker Compose or Podman Compose, checks the fixed
operator-chosen port, starts Ollama, requires the configured chat and embedding
models to already be present, builds the backend, and waits for `/readyz`. It
does not pull models. The default models are `llama3.1:8b-instruct` and
`nomic-embed-text`; pull an alternative only through the operator's normal
Ollama workflow, then configure the matching model names.

For the normal local path, retain `PROVIDER=ollama`,
`EMBEDDING_PROVIDER=ollama`, and `RETRIEVAL_BACKEND=local`. The default port is
8080 unless `CAIRN_PORT` is set. A successful `make live` prints the selected
engine, Cairn URL, and exact embed. The normal URLs are:

```text
http://localhost:8080/healthz
http://localhost:8080/readyz
http://localhost:8080/demo
http://localhost:8080/api/v1/capabilities
```

Check liveness and readiness explicitly:

```sh
curl --fail --silent --show-error http://localhost:8080/healthz
curl --fail --silent --show-error http://localhost:8080/readyz
```

Open `/demo`, and use the printed widget snippet on an allowlisted site. Ask one
question whose answer is in the configured corpus and confirm that the answer
carries the expected citation. Then ask an unrelated question and confirm that
Cairn refuses rather than inventing an answer. `/readyz` must return HTTP 200
with `status: "ok"` and all three public checks true before treating the
instance as ready.

## 3. Provider and retrieval boundaries

Ollama generation and embeddings are the supported local configuration. Gemini
is optional generation only: build an image with
`CAIRN_INSTALL_GEMINI=true`, set `PROVIDER=gemini`, provide `GEMINI_API_KEY` only
at runtime, and keep `EMBEDDING_PROVIDER=ollama`. Do not pass a Gemini key as a
build argument. Gemini configuration does not create an author-hosted service or
production certification.

`RETRIEVAL_BACKEND=firestore` is for development and test composition only. It
is not a production retrieval path in 0.1.0. Keep the local default unless the
operator is deliberately performing the documented development-only exercise.
Likewise, the repository's deployment-wide public endpoint controls are
development-only; they are not a ready-made production admission or billing
system.

## 4. Stop, restart, back up, and roll back

Stop the grounded stack without deleting its named data or model volumes:

```sh
make live-down
```

Restart it with the same reviewed corpus and configuration using `make live`,
then repeat the readiness, cited-answer, and refusal checks.

Before an upgrade or configuration change, stop Cairn and back up both the
reviewed corpus directory (including `provenance.json`) and the SQLite state in
the `cairn_data` volume. The state consists of the metadata database and the
`CHROMA_PATH/cairn-vectors-v1.sqlite3` vector index. Keep the corpus backup and
state snapshot together with the release version and configuration used to create
them. Do not copy a live SQLite file while the service is writing it.

To roll back, stop the current stack, restore the matching corpus and SQLite
snapshot, return to the previous verified release tree and configuration, start
with `make live`, and repeat the checks in section 2. A rollback is not proven
until its `/readyz`, grounded-answer, and refusal checks pass. Do not reuse
SQLite data across replicas or network filesystems: one local instance is the
supported design point.

## 5. Ongoing supply-chain review

Every week, obtain the current vulnerability data under the operator's normal
security process and run the repository's operator verification command:

```sh
make verify
```

Review the five shipped SBOMs in `sbom/` alongside that result. A release
checksum verifies the delivered files; it does not replace a later vulnerability
rescan.
