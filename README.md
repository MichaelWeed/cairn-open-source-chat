# Cairn

[![validate](https://github.com/MichaelWeed/cairn-open-source-chat/actions/workflows/validate.yml/badge.svg)](https://github.com/MichaelWeed/cairn-open-source-chat/actions/workflows/validate.yml)

**Self-hosted AI support chat that answers from your documents with citations and refuses to guess.**

Cairn is a free, Apache-2.0 developer preview for teams that want useful support
answers without a SaaS subscription, per-seat pricing, or a third party holding
their customer conversations. Point it at the documents your team trusts; Cairn
retrieves relevant passages, cites its sources, and refuses when retrieval is below
its configured confidence threshold. The supported default path uses local Ollama
models, so an operator can keep the service and its data in their own infrastructure.

**Developer preview / pre-release.** Today you can run the cited local demo and
use the generic `<cairn-chat>` custom element. It provides a shadow-DOM chat UI,
accepts `api-url` and `assistant-name`, and streams cited answers over SSE. Human
handoff, order-status deep links, an admin experience, the guardrail pipeline, and
any author-hosted service are planned, not available behavior. An optional Gemini
generation adapter is available for explicit opt-in use; it is excluded from the
local default install and does not establish production or hosted readiness.
Provider adapters normalize bounded, content-free token usage in memory. Cairn
also provides pure cost-accounting helpers for an operator-supplied immutable
price snapshot; no prices, budget enforcement, or accounting sink are bundled.

It is for CTOs and support leaders evaluating a grounded, self-hosted alternative
to an opaque support bot. The problem is practical: an unsupported answer can
mislead a customer, while a hosted bot can turn a support conversation into someone
else's data. Cairn's differentiators are grounded answers, visible citations,
low-confidence refusal, and a local-model-first deployment path.

## Try the developer preview

With `uv`, Ollama, and Cairn's default models already installed, run:

```sh
make demo
```

Open `http://localhost:8080/demo` and ask about shipping, returns, or warranties.
For prerequisite commands, the default model pair (`llama3.1:8b-instruct` and
`nomic-embed-text`), `.env` overrides, `make demo` versus `make up`, architecture,
API, security, evaluation, development, and operations, start with the
[full technical guide](DEVELOPER_README.md).

## Why this problem matters

“Where is my order?” tickets typically account for 30–60% of ecommerce support
volume¹, and delayed or missing packages are a leading reason customers contact
retail support². Those are industry benchmarks, not Cairn performance claims. They
motivate trustworthy answers from an operator's own knowledge base, not a claim
that Cairn provides order-status integrations today.

## Learn more

* [DEVELOPER_README.md](DEVELOPER_README.md) — full technical guide: setup, configuration, architecture, API, security, evaluation, development, and operations.
* [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) — machine-readable capability discovery, current compatibility versions, and pre-1.0 upgrade rules.
* [docs/CORPUS-PROVENANCE.md](docs/CORPUS-PROVENANCE.md) - versioned startup manifest and public citation contract.
* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/adr/](docs/adr/) — system shape, built-versus-planned boundaries, and decisions.
* [docs/SECURITY.md](docs/SECURITY.md) and [docs/PRIVACY.md](docs/PRIVACY.md) — security status, vulnerability reporting, and data handling.
* [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) — supported contribution boundary and non-goals.
* [docs/PUBLIC-AVAILABILITY.md](docs/PUBLIC-AVAILABILITY.md) — delivered public-availability record and the remaining tracking decision.

Built and maintained by [Michael Weed](https://github.com/MichaelWeed). See the
[Apache-2.0 license](LICENSE).

---

¹ [CorePiper, *What Is WISMO and How to Reduce 'Where Is My Order' Tickets*](https://corepiper.com/blog/what-is-wismo/) — WISMO tickets typically account for 30–60% of ecommerce support volume.
² [MeasuringU, *customer service study*](https://measuringu.com/customer-service/) — a delayed package or delivery problem was the top coded reason participants contacted retail support.
