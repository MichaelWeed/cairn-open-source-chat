# Cairn

[![validate](https://github.com/MichaelWeed/cairn-open-source-chat/actions/workflows/validate.yml/badge.svg)](https://github.com/MichaelWeed/cairn-open-source-chat/actions/workflows/validate.yml)

**Cut a third of your support chat volume with a free, self-hosted AI chat that answers from your own docs — and refuses to guess.**

Free and open source (Apache-2.0). No per-seat fees, no SaaS subscription, no conversation data leaving your infrastructure — the only cost is the server it runs on.

<!-- 30-second demo video goes here (MASTER_PLAN.md task 7.5) — recorded
     against the production widget once Phase 6 lands. -->

## Where "a third" comes from

"Where is my order?" tickets alone consume 30–60% of ecommerce support volume¹, and a delayed or missing package is the single most common reason customers contact retail support at all². Those are exactly the conversations Cairn is built to absorb: instant answers drawn from *your* ingested docs with a citation on every claim, order-status deep links, and a clean handoff to a human when the bot genuinely can't help.

## Why trust it

Support bots get businesses burned in two ways: they make things up, or they leak customer data to a third-party API. Cairn is built so neither can happen — if an answer isn't grounded in your docs, it refuses instead of hallucinating; and it runs on local models (Ollama) by default, so support conversations never have to leave your infrastructure. Every guardrail decision is visible and toggleable, not a black box.

| If you're a... | This gets you |
| --- | --- |
| **CTO** evaluating build vs. buy | A production-shaped reference implementation: frozen API contracts, layered prompt-injection defenses, SBOM + vulnerability scanning on every release, a real CI gate — not a demo repo. |
| **Customer Service director** | A support widget that never invents an answer, always shows its source, and hands off to a human cleanly when it should. |

## Status

**Pre-release, in active development.** See [MASTER_PLAN.md](MASTER_PLAN.md) for the phased build-out and current progress (order-status deep links land in Phase 3; the production embeddable widget in Phase 6).

## Docs

* [DEVELOPER_README.md](DEVELOPER_README.md) — architecture, API contract, security model, quick start.
* [MASTER_PLAN.md](MASTER_PLAN.md) — phases, task board, delivery model, and the post-1.0 recommended roadmap.
* [docs/SECURITY.md](docs/SECURITY.md) — what's actually built and tested today vs. planned; vulnerability reporting.
* [docs/PRIVACY.md](docs/PRIVACY.md) — what data is handled, where it lives, and data segregation (this is a single-tenant deployment, not a multi-tenant SaaS).

## Get in touch

Built and maintained by [Michael Weed](https://github.com/MichaelWeed). Need something past the roadmap — CRM sync, auth-aware answers, multi-tenancy, or a tuned deployment for your stack? Those are consulting scope: open an issue or reach out directly. `docs/CONTRIBUTING.md` (added in Phase 7) will cover scope and the non-goals this project deliberately doesn't chase.

## License

[Apache-2.0](LICENSE).

---

¹ [CorePiper, *What Is WISMO and How to Reduce 'Where Is My Order' Tickets*](https://corepiper.com/blog/what-is-wismo/) — WISMO tickets typically account for 30–60% of ecommerce support volume.
² [MeasuringU, *customer service study*](https://measuringu.com/customer-service/) — a delayed package or delivery problem was the top coded reason participants contacted retail support.

These are industry benchmarks, not Cairn-measured results. The eval harness (`make eval`) measures what Cairn actually does on your corpus, and a full committed eval report backs every product-measured number before 1.0 (MASTER_PLAN.md task 7.4).
