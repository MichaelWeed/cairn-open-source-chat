# Cairn

[![validate](https://github.com/MichaelWeed/cairn-open-source-chat/actions/workflows/validate.yml/badge.svg)](https://github.com/MichaelWeed/cairn-open-source-chat/actions/workflows/validate.yml)

**Self-hosted customer-support chat that cites its sources and refuses to guess.**

Cairn is an embeddable chat widget backed by a FastAPI service that answers from *your* ingested docs — every answer carries citations, and it refuses instead of hallucinating when it isn't confident. Runs on local models (Ollama) by default, so support conversations never have to leave your infrastructure.

## Why this exists

Support teams get burned by two failure modes: bots that make things up, and bots that leak data to a third-party API. Cairn is built so neither can happen — grounded answers only, self-hosted by default, and every guardrail decision is visible and toggleable, not a black box.

| If you're a... | This gets you |
| --- | --- |
| **CTO** evaluating build vs. buy | A production-shaped reference implementation: frozen API contracts, layered prompt-injection defenses, SBOM + vulnerability scanning on every release, a real CI gate — not a demo repo. |
| **Customer Service director** | A support widget that never invents an answer, always shows its source, and hands off to a human cleanly when it should. |

## Status

**Pre-release, in active development.** See [MASTER_PLAN.md](MASTER_PLAN.md) for the phased build-out and current progress.

## Docs

* [DEVELOPER_README.md](DEVELOPER_README.md) — architecture, API contract, security model, quick start.
* [MASTER_PLAN.md](MASTER_PLAN.md) — phases, task board, delivery model.

## Get in touch

Built and maintained by [Michael Weed](https://github.com/MichaelWeed). Open to consulting engagements — `docs/CONTRIBUTING.md` (added in Phase 7) will cover scope and the non-goals this project deliberately doesn't chase.
