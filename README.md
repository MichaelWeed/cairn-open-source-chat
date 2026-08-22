# Cairn

[![validate](https://github.com/MichaelWeed/cairn-open-source-chat/actions/workflows/validate.yml/badge.svg)](https://github.com/MichaelWeed/cairn-open-source-chat/actions/workflows/validate.yml)

**Cairn is building toward a free, self-hosted AI support chat that answers from your own docs and refuses to guess.**

Free and open source (Apache-2.0). No per-seat fees, no SaaS subscription, no conversation data leaving your infrastructure — the only cost is the server it runs on.

<!-- 30-second demo video goes here (MASTER_PLAN.md task 7.5) — recorded
     against the production widget once Phase 6 lands. -->

## Where "a third" comes from

"Where is my order?" tickets alone consume 30–60% of ecommerce support volume¹, and a delayed or missing package is the single most common reason customers contact retail support at all². Those are exactly the conversations Cairn is built to absorb: instant answers drawn from *your* ingested docs with a citation on every claim, order-status deep links, and a clean handoff to a human when the bot genuinely can't help.

## Why trust it

Support bots get businesses burned in two ways: they make things up, or they leak customer data to a third-party API. The current developer preview retrieves from a local corpus, refuses below its configured confidence threshold, cites retrieved sources, and uses local Ollama models. The broader guardrail and operator-control surfaces are planned work, not built behavior.

| If you're a... | This gets you |
| --- | --- |
| **CTO** evaluating build vs. buy | A developer-preview reference implementation with frozen API contracts, a local validation gate, generated SBOMs, and explicit built-vs-planned security documentation. |
| **Customer Service director** | A preview of grounded answers and source citations today; the production widget and human handoff are planned. |

## Status

**Pre-release, in active development.** See [MASTER_PLAN.md](MASTER_PLAN.md) for the phased build-out and current progress (order-status deep links land in Phase 3; the production embeddable widget in Phase 6).

## Developer preview quick start

Prerequisites: [uv](https://docs.astral.sh/uv/), Ollama running locally, and the two configured models already present. The helper checks the corpus, Ollama service, and both models before starting the server. It never downloads models.

```sh
ollama pull llama3.1:8b-instruct
ollama pull nomic-embed-text
make demo
```

Open `http://localhost:8080/demo` and ask a question about shipping, returns, or warranties. `make demo` ingests the bundled sample Markdown through the in-process ingestion helper, uses real Ollama embeddings and generation, and streams cited answers from the demo page. Copy `.env.example` to `.env` first only when you need to change the port, Ollama URL, or model names.

`make up` is a separate container plumbing smoke test. Its checked-in defaults use the deterministic echo provider and fake embeddings, do not ingest the sample corpus, and do not provide an `/admin` route. It is not the grounded-answer quick start.

## Docs

* [DEVELOPER_README.md](DEVELOPER_README.md) — architecture, API contract, security model, quick start.
* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — system shape, what's built vs. designed, and the constraints behind it; [docs/adr/](docs/adr/) records the decisions already made.
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
