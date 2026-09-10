# Startup corpus provenance

Cairn requires a `provenance.json` file at the root of every corpus mounted through
`CORPUS_PATH`. The manifest binds each Markdown or PDF file to a reviewed public
source before startup ingestion can make the corpus ready.

This contract is implementation-neutral. Cairn does not fetch the source, choose a
publisher, or approve content. The operator selects and reviews the material, records
its provenance, and mounts both the documents and manifest read-only.

## Manifest format

Schema version 1 has one entry for every supported document, keyed by its stable
POSIX-style path relative to the corpus root:

```json
{
  "version": 1,
  "documents": {
    "policies/returns.md": {
      "title": "Returns and refunds",
      "url": "https://docs.example.com/policies/returns",
      "sha256": "9d03a75d93d8f725c8d52af430b0b89d29a3e24d7a3f5464f53a3f6dd21d1c1b",
      "owner": "Documentation team",
      "reviewed_at": "2026-09-08",
      "public": true
    }
  }
}
```

Each entry contains exactly these fields:

| Field | Contract |
| --- | --- |
| `title` | Non-empty public title of at most 160 Unicode characters shown on the citation chip |
| `url` | Absolute `http` or `https` canonical source URL without embedded credentials |
| `sha256` | Lowercase SHA-256 of the exact mounted file bytes |
| `owner` | Non-empty operator-recorded content owner |
| `reviewed_at` | Review date in `YYYY-MM-DD` form |
| `public` | Must be `true`; this is the operator's explicit public-source attestation |

Generate the digest from the exact file that will be mounted. For example:

```sh
sha256sum policies/returns.md
# macOS also provides: shasum -a 256 policies/returns.md
```

## Startup behavior

Validation covers the whole corpus before Cairn writes document metadata, replaces
vector chunks, or removes stale documents. Startup fails explicitly and readiness is
not established when:

* `provenance.json` is absent, oversized, not UTF-8, or not valid JSON;
* the schema version, top-level keys, entry fields, path syntax, URL, hash, or review
  date is invalid;
* JSON keys are duplicated;
* a Markdown or PDF file has no entry, or an entry names no mounted document;
* a supported document or `provenance.json` is a symbolic link;
* an entry is not marked public; or
* the recorded SHA-256 does not match the mounted bytes.

Changing only a verified title or URL refreshes stored chunk metadata on the next
startup, even when the document bytes have not changed. Removing or renaming a file
requires the same manifest change in that mounted corpus version.

## Deterministic candidate planning

The same validated version 1 manifest can also feed Cairn's internal offline
candidate planner together with the exact same-read document byte objects. The
planner preserves title, URL, owner, review date, public attestation, and each exact
source-byte SHA-256 while deriving stable document/chunk identities and a complete
frozen plan for one exact corpus ID and version. It performs no file, network,
provider, database, or vector-store access on its own.

The semantic manifest SHA-256 is distinct from each entry's source-byte SHA-256. It
canonically binds manifest version, sorted relative paths, and all six entry values,
so JSON whitespace and object-key order do not matter while any provenance or source
hash change does. This planning step does not persist, publish, activate, update, or
delete a corpus candidate.

## Citation behavior and boundary

Manifested startup documents use the verified `title` and `url` in the existing
`CitationSource {id, title, url}` response. The widget already renders valid HTTP(S)
sources as links that open in a new tab with `noopener noreferrer`.

Direct programmatic ingestion has no provenance manifest and deliberately keeps the
existing `document://<id>` internal-reference fallback. The widget displays that
fallback as an inert citation badge, not a dead link.

The manifest records an operator decision; it is not a license audit, URL availability
check, DNS lookup, or claim that Cairn endorses the source. Content selection and legal
approval remain outside this repository capability.
