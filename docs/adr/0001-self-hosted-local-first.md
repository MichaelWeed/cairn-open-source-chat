# ADR-0001 — Self-hosted, single-tenant, local models by default

**Decided:** 2026-07-10 (founding constraint) · **Status:** Accepted

## Context

Support chat is a category where the buyer's real objection is rarely capability.
It is that customer conversations — containing order numbers, addresses,
complaints, and occasionally payment context — get sent to a third-party API under
someone else's data-handling terms, and that the vendor charges per seat or per
conversation forever.

A product can answer that objection contractually, with a data-processing agreement
and a privacy policy, or structurally, by arranging things so the data has nowhere
else to go. Contractual answers require the buyer to trust the vendor. Structural
ones do not.

## Decision

Cairn is self-hosted, single-tenant, and runs local models via Ollama by default.
The operator owns every component. There is no author-operated service, no hosted
tier, and no telemetry. A hosted OpenAI-compatible provider is a supported adapter
target, but it is off by default and turning it on is documented as an explicit
choice with data consequences.

Single-tenancy is part of the same decision: one deployment serves one
organization. An operator needing to serve two runs two deployments.

## Consequences

**Accepted costs.** Local 8B-class models are slower and less capable than frontier
hosted models, and answer quality depends on hardware the operator supplies. A
single instance's concurrency ceiling is set by Ollama's inference parallelism
rather than by the web layer. There is no usage telemetry, so there is no feedback
signal about how deployments actually behave — the evaluation harness exists partly
to fill that gap locally.

**What it buys.** The privacy claim needs no trust: with the default configuration
there is no egress path for chat text. It also removes an entire class of
obligation — no infrastructure to operate, no customer data held, no uptime
commitment, no per-tenant isolation code to get wrong.

**What it forecloses.** Multi-tenancy, auth-aware per-user answers, and CRM
synchronization are all closed as non-goals downstream of this decision, not
separately. Each would reintroduce either shared infrastructure or an identity
system that this architecture deliberately does not have.

**Where it shows up in the code.** No authentication anywhere; SQLite and embedded
Chroma rather than networked datastores; in-process rate limiting; and a
configuration surface built around one operator, not a tenant hierarchy.
