# Cairn — Project Brief

Baseline established 2026-08-21. Companion files: [project.yaml](../project.yaml) for
stable identity and system associations, [ARCHITECTURE.md](ARCHITECTURE.md) for the
system's shape, [adr/](adr/) for decisions already made.

This brief exists so the project can be picked up cold after a multi-week gap
without re-deriving anything from the code. Work happens here in bursts; that is
the expected cadence, not a problem to fix.

---

## 1. Why this project exists

Two reasons, both real, and they do not conflict.

**For the people who install it:** a business that wants an AI support chat today
faces a bad menu. The SaaS options charge per seat or per conversation, and every
customer question — including the ones containing order numbers, addresses, and
complaints — is sent to a third party. The self-build option means assembling
retrieval, streaming, guardrails, and an embeddable widget yourself. Cairn is the
third option: install it, point it at your own documents, and it runs entirely on
your own infrastructure at the cost of the server.

**For its author:** it is a public work sample that demonstrates production
engineering judgment rather than describing it, and requests that fall outside its
deliberate scope are routed to paid consulting. That motive is deliberately kept
out of the published documentation — the product is meant to persuade on its own
merits, and a plan document has never convinced anyone to run something in
production.

## 2. Intended users

Two distinct audiences, addressed by different documents.

* **The evaluator — a CTO or senior engineer** deciding build-vs-buy. They will
  spend a minute or two. What they are looking for is evidence that this is a real
  engineering artifact and not a weekend demo: frozen wire contracts, a real
  validation gate, an SBOM, documented CVE exceptions with re-check conditions, and
  a security document that states plainly what is *not* built yet.
* **The operator — whoever installs and runs it.** Frequently the same person.
  They need it to work from a clean clone, to be honest about its limits, and to
  keep working after CVEs are disclosed against it months later.

A third group is served but never addressed directly: **the end user**, a site
visitor asking a support question. They never see any of this documentation. Their
only requirement is a correct answer or an honest refusal.

## 3. The problem it addresses

Support chatbots fail businesses in two specific, well-understood ways: they
fabricate answers, and they leak customer conversations to a third-party API.
Cairn's entire design is organized around making both structurally difficult
rather than merely discouraged.

* Against fabrication: retrieval confidence is checked before the model is called,
  and a low-confidence query is refused mechanically without the provider ever
  being invoked. Answers carry citations.
* Against leakage: local models via Ollama are the default path, conversation text
  is never written to disk, and the server holds no conversation state at all.

The volume opportunity it targets is order-status questions — "where is my order"
— which industry sources place at 30–60% of ecommerce support volume. Those figures
are cited benchmarks, not Cairn measurements, and the README labels them as such.

## 4. Current state

**Admission-review observation recorded 2026-08-21; publication evidence updated
2026-08-22.**

Working end to end: an SSE chat endpoint with per-IP and per-session token buckets
and an origin allowlist; frozen Pydantic wire contracts that reject unknown fields
outright; deterministic echo and real Ollama providers behind one interface; an
embedded SQLite flat-vector index; markdown and PDF ingestion with content-hash
incremental reindex; retrieval that produces citations and refuses below a
confidence threshold; an evaluation harness with one committed report from a real
model; and a demo page that exercises the whole round trip.

Measured, not asserted: the publication baseline completed the full `make validate`
gate successfully, with one known Starlette TestClient deprecation warning. The
durable first-successful hosted-run evidence is recorded in `project.yaml`.

Not built: HTTP upload endpoint and scrape ingestion; the entire tool and
escalation layer; the always-on guardrail middleware and the adversarial test
suite (`backend/tests/adversarial/` holds only a `.gitkeep`); any admin surface,
and therefore any authentication; the production embeddable widget
(`widget/src/index.ts` is a single version constant); and the release bundler
(`make release` prints a placeholder).

**The defining fact about the current state is now public evidence, not private
availability.** The 2026-08-21 admission review recorded the then-headless source
repository and absent hosted-run evidence. On 2026-08-22, the first sanitized
`main` push created remote main at `04a1db5d72884eb8fbf803ab46686ccc58d3a9b6`.
Its first hosted validation exposed fresh-CI SBOM-generator drift; a normal forward
fix moved published main to `ab1668696acc60ca7a696f6f738bb9425d1eb3ea`, where
GitHub Actions validate run 32605367945 completed successfully. The README badge
now has a real status; this remains public source availability, not a hosted service.

## 5. Delivered outcome

**Cairn is public, and a person who has never seen it can clone it and get a cited
answer without asking the author anything.**

This distribution and honesty outcome was delivered on 2026-08-22 without adding
a product capability: sanitized history was published, private planning remained
outside the public branch, documentation links and claims were reconciled, and
the cold-start path was rehearsed before and after publication.

## 6. How it was proved

The success test was performed rather than assumed. A fresh public clone with
`uv`, Ollama, and the two documented models already installed ran `make demo` and
completed a cited bundled-corpus chat round trip without a tracked-file edit or
model download. It returned healthy from `/readyz`, emitted citations for
`returns.md`, `warranty.md`, and `shipping.md`, emitted `done`, and shut down with
its port closed.

`make up` is intentionally a separate echo/fake container-plumbing smoke test. It
does not ingest a corpus, prove grounded answers, or expose an `/admin` route.

Supporting conditions that make that test meaningful:

* GitHub Actions run 32605367945 on published `main` completed successfully, so
  the README badge shows a real status.
* The tracked Markdown graph has zero missing relative targets or anchors.
* The GitHub description uses the approved grounded-answer wording and does not
  repeat the superseded unsupported “up to 40%” claim.

## 7. Explicitly out of scope

Out of scope for **the next outcome** — deliberately deferred, not abandoned:

* Any Phase 3+ feature work: tools, escalation, guardrail middleware, admin
  surfaces, the production widget.
* The two open ingestion tasks (scrape ingestion, HTTP upload endpoint).
* Making the evaluation statistically meaningful.
* Any deployment, hosted demo, or environment the author operates.

Out of scope for **the project**, closed by policy rather than re-argued per
request: CRM synchronization, auth-aware or per-user answers, multi-tenancy within
one deployment, and voice. These are the boundary that keeps a single-maintainer
project finishable.

## 8. Personal, open source, or commercial

All three, in a specific arrangement. The software is genuinely free and
Apache-2.0 with no paid tier, no per-seat fee, and no hosted upsell — the licence
is not a loss-leader for a commercial edition, because no commercial edition is
planned. It is simultaneously a personal project with one maintainer and an
intermittent cadence, and a professional artifact whose out-of-scope requests
route to paid consulting.

The practical consequence: it must meet a professional standard of honesty in
public, while being resumable by one person after long gaps.

## 9. Exposure

**Public source today, and nothing is deployed anywhere by the author.** The
repository is public; the software is not running as an author-hosted service.

The delivery model matters for how risk is assessed: the author operates no
service and holds no user data. Every risk in this project is borne by a
third-party operator who installs it. That inverts the usual calculation — the
obligation is not to secure a production environment but to be accurate about
what the software does and does not protect, because operators will make
deployment decisions from the documentation alone.

`docs/SECURITY.md` already discharges this well, including the admission that the
prompt-injection defense is a prompt-level mitigation whose adversarial test suite
does not yet exist.

## 10. Data, credentials, integrations, sensitive operations

* **Chat message text:** never persisted server-side. Passed through the rate
  limiter, retrieval, and provider, then discarded.
* **Conversation history:** the API accepts up to five caller-supplied turns, but
  the current demo sends an empty history array and persists no conversation
  history. The planned production widget will own client-side persistence; the
  server remains stateless.
* **Knowledge corpus:** operator-supplied. Chunk text and embeddings in the local
  SQLite flat index; per-document metadata in SQLite. Legacy Chroma files are
  untouched, and re-ingestion is explicit. This is the only durable data.
* **Credentials:** none handled today. `ADMIN_BOOTSTRAP_PASSWORD` is a reserved
  environment variable with no consumer, because no admin surface exists.
* **Secrets in the repository:** none. `.env` is gitignored; `.env.example`
  ships empty values.
* **External integrations:** exactly one — Ollama, at an operator-configured base
  URL. No other outbound calls exist at runtime. A hosted provider adapter is
  planned but not implemented; adding one would send chat text off-premises.
* **Sensitive operations:** none in the current build. No authentication, no
  payments, no destructive endpoints, and no attacker-reachable ingestion surface,
  because ingestion is not yet routed over HTTP.

## 11. Architecture and dependencies

Summarized in [ARCHITECTURE.md](ARCHITECTURE.md); the API contract and operational
detail are in [DEVELOPER_README.md](../DEVELOPER_README.md). In one line: a FastAPI
service streaming SSE, retrieving from a local SQLite flat index, calling a local
Ollama model, with SQLite for metadata, fronted today by a demo page that sends no
conversation history. The production widget and its client-side history are
planned work.

Every dependency carries a written justification in
[DEPENDENCIES.md](DEPENDENCIES.md), including the reasoning for version pins.

## 12. Deployment and operating model

There is no deployment. The intended model is that an operator runs
`docker compose` or `podman compose` on one host, on an operator-chosen fixed
port, behind their own TLS-terminating reverse proxy. Single instance is the
design point; horizontal scaling is not a supported path and cannot be reached by
mounting either SQLite file on replicas. The local index is O(N) per synchronous
query and supports only the bounded corpus design point.

The operating obligation that outlives the release: nothing external will warn a
self-hosted operator about CVEs disclosed after they installed. `make verify` is
the operator-side re-scan, and the documentation recommends a weekly cadence. The
release bundler that would ship it does not exist yet.

## 13. Largest risks, gaps, assumptions, unknowns

**Risks**

* *CI toolchain maintenance remains live.* The first hosted run exposed SBOM
  generator drift, which was corrected by pinning the generator toolchain before
  successful run 32605367945. GitHub also reported a non-failing Node 20
  action-runtime deprecation annotation; it needs maintenance attention, not a
  gate waiver.
* *The pre-push hook runs the full gate.* Future pushes still require scanners, a
  container engine, and registry access; this is deliberate friction, not an
  unresolved first-publication risk.
* *Single maintainer, intermittent cadence.* The mitigation is that the repository
  is written to be resumable cold, which is unusually well done here — the QA
  checklist records what was verified, when, and what remains unverified.

**Gaps**

* The demo page has never been driven through a real browser; all verification to
  date went through an automated preview browser.
* `RETRIEVAL_MAX_DISTANCE=1.2` has never been validated against the documented
  default models. It is a starting point presented as a default.
* Structured logging enforces "no message bodies" by convention and review, with
  no mechanical filter.
* Public documentation must resolve to existing, durable authorities. The
  contribution policy lives in `docs/CONTRIBUTING.md`; architecture and ADRs are
  the design authorities rather than a second design document.

**Assumptions worth marking as assumptions**

* That an operator-facing README aimed at a CTO is the right funnel. Untested —
  nobody outside the author has seen the project.
* That local 8B-class models produce answers good enough for real support traffic
  on a real corpus. The one committed evaluation used three documents and six
  questions, and its own report says so.

**Unknowns**

* Whether anyone wants this. Zero external signal exists, by construction.

---

## Provenance of this brief

Distinguishing where each claim came from, since the sourcing matters:

* **Observed in the repository and publication evidence:** everything in sections
  4, 6, 10, 11, and the concrete parts of 13 — commit history, remote state,
  GitHub Actions run 32605367945, fresh public-clone evidence, test results, file
  contents, dependency and CVE-exception records.
* **Stated by the author (2026-08-21):** the intermittent cadence; that the next
  outcome is public credibility; that the success test is the clean-clone run;
  that private planning material stays out of published documentation; that
  tracking moves to GitHub Issues and Projects rather than Jira.
* **Inferred:** the 2026-08-21 admission review found publication rather than
  capability to be the binding constraint. That constraint is now closed; the
  risk profile remains dominated by third-party operators rather than by the author.
