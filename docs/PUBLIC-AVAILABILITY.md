# Milestone — Public availability

**Delivered 2026-08-22.** Cairn is public, and a person who has never seen it
can clone the published repository and obtain a cited answer from the bundled
corpus without changing a tracked file. This is a completion record, not a
replacement for an issue tracker: the GitHub Issues and Projects features are
enabled, but no issue, milestone, project board, or work object exists. See
[project.yaml](../project.yaml) for the durable source of that state.

The delivered outcome is source availability, not an author-hosted service. Cairn
is public at [MichaelWeed/cairn-open-source-chat](https://github.com/MichaelWeed/cairn-open-source-chat),
but no author-operated deployment exists.

## Completed work

1. **Private planning stays outside published history.** The public `main` history
   and tracked tree contain no private-plan document or inbound reference;
   recoverable private preservation remains outside the repository.
2. **Public documentation links resolve.** The tracked Markdown graph has zero
   missing relative targets or anchors. `docs/CONTRIBUTING.md` is the contribution
   policy, while [ARCHITECTURE.md](ARCHITECTURE.md) and [adr/](adr/) remain the
   durable design authorities.
3. **Public claims agree.** The GitHub description is now: “Free, self-hosted AI
   support chat that answers from your docs with citations and refuses to guess.”
   It does not repeat the superseded unsupported “up to 40%” claim.
4. **Cold-clone path was rehearsed before publication.** An isolated locked clone
   completed `make validate` and the real `make demo` cited-answer path with the
   documented, already-installed Ollama model pair. No model was downloaded.
5. **Only `main` was first-pushed and CI is green.** The first published ref was
   `04a1db5d72884eb8fbf803ab46686ccc58d3a9b6`; its first hosted validation failed
   only because the fresh CI runner resolved different SBOM-generator tooling. A
   normal forward fix moved published `main` to
   `ab1668696acc60ca7a696f6f738bb9425d1eb3ea`; GitHub Actions validate run
   [32605367945](https://github.com/MichaelWeed/cairn-open-source-chat/actions/runs/32605367945)
   completed successfully. The workflow runs the same `make validate` gate as
   local pre-push.
6. **The public-clone claim was proved after publication.** A fresh HTTPS clone of
   public `main` passed the full validation contract, had zero missing Markdown
   targets or anchors, returned healthy from `/readyz`, and streamed a bundled-
   corpus answer citing `returns.md`, `warranty.md`, and `shipping.md` before its
   `done` event. The demo then shut down cleanly and its port was closed.

Runtime `/readyz` also evaluates the selected provider/model, embedding, and
retrieval-route evidence internally while preserving its published response shape.
Hosted readiness remains explicitly development-only; this is not a production
deployment or hosted trust-policy claim.
Private request accounting is composed behind the unchanged chat/SSE surface. It has
no persistence, logging, budget-enforcement, capability, or public API seam.

## Evidence and maintenance note

The README Actions badge now resolves as SVG HTTP 200 and GitHub detects the
Apache-2.0 license. The failed first hosted validation did not invalidate the
publication outcome: its narrow SBOM-generator drift was fixed forward and the
recorded successful run above established the green published baseline. GitHub also
emitted a non-failing Node 20 action-runtime deprecation annotation. That is a
maintenance finding, not a failed validation gate.

## Open decision

| # | Decision | Status |
| --- | --- | --- |
| D4 | Whether all remaining development work migrates into GitHub Issues, or only the work needed for a future milestone | Open, nonblocking for this completed publication milestone |
