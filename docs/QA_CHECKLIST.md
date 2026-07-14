# Manual QA Checklist

Companion to `make eval` (DEVELOPER_README.md §6), which measures groundedness,
citation accuracy, and refusal correctness quantitatively against a question
set. This checklist is the qualitative counterpart: does it actually feel
right in a browser. Run it after any change to `backend/app/static/demo/`,
the chat SSE contract, or the retrieval/refusal pipeline.

## Setup

```
cp .env.example .env   # if you haven't already
ollama pull llama3.1:8b-instruct        # or set OLLAMA_MODEL in .env to a model you have
ollama pull nomic-embed-text            # or set EMBEDDING_MODEL in .env to one you have
make demo
```

`make demo` (`scripts/dev_demo.py`) boots the real app — real Ollama provider,
real embeddings, not the `echo`/`fake` defaults `make up` uses out of the box
— ingests `eval/corpus/` (the bundled returns/shipping/warranty sample) into
it, and serves it at `http://localhost:$CAIRN_PORT/demo` (8080 by default).
It prints ingestion results and the URL on startup. Point `--corpus` at a
different directory of `.md` files to QA against other content. Set
`PROVIDER=echo` in `.env` first if you only need to check UI/wiring and want
instant, deterministic replies instead of real generation.

Re-running `make demo` is safe — `ingest_upload()`'s content-hash reindex
reports `unchanged` for anything already ingested rather than duplicating it.

## Scenarios

Each row is a scenario, its expected result, and status as of the last time
it was actually driven through a browser (not just read from source). Update
the date when you re-verify; add a row if you find a new scenario worth
tracking.

| # | Scenario | Expected result | Status |
| - | --- | --- | --- |
| 1 | Ask an in-corpus question (e.g. "How many days do I have to return an item?") | Grounded answer, citation chips for the matching document(s) appear below the reply | Verified 2026-07-14 |
| 2 | Ask an out-of-corpus question (e.g. "Can you recommend a good restaurant nearby?") | Refusal template renders as a normal assistant bubble (not styled as an error); no citation chips; server log shows no `/api/chat` (or provider) call for that turn — confirms the refusal gate fires before generation, not after | Verified 2026-07-14 |
| 3 | Click a citation chip | If the source has a real `http(s)` URL, it navigates; otherwise it's a plain badge, not a dead link | Verified 2026-07-14 — currently always a badge, since uploaded documents have no real URL until task 2.3/5.3 |
| 4 | Multi-turn conversation on a viewport shorter than the accumulated content (mobile especially — try 375×812) | After every reply, the input box and the full latest reply are both in view without the user manually scrolling | Verified 2026-07-14 — was broken (page-level scroll didn't follow new content, only the inner log panel did), fixed same session |
| 5 | Toggle light vs. dark color scheme | Legible contrast, consistent styling, no unstyled/invisible elements in either | Verified 2026-07-14 |
| 6 | Tab to the input, then to the send button | Visible focus ring on both | Verified 2026-07-14 |
| 7 | Press Enter with text in the input | Submits the form, same as clicking send | **Unverified.** Got inconsistent results through browser-automation tooling (standard HTML default, nothing in the page's JS blocks it — plausibly a tooling artifact, not a real bug). Confirm with an actual keyboard in a real browser. |
| 8 | Full pass in a real browser (Chrome/Safari/Firefox), not an automated/sandboxed one | Same as above | **Unverified.** Everything above was checked through an automated preview browser tool, never a real one. |
| 9 | Long single-word input (e.g. a long URL pasted into the message) | Wraps inside the bubble, doesn't overflow the card | Verified 2026-07-14 |
| 10 | `RETRIEVAL_MAX_DISTANCE` refusal threshold against the *documented default* models (`llama3.1:8b-instruct` / `nomic-embed-text`) | Refusal/answer boundary behaves sanely | **Unverified end to end.** Every live test so far (this session and the `eval/reports/` history) used whichever Ollama models happened to be available locally, not the documented defaults. Run `make eval` after pulling the documented defaults before trusting `RETRIEVAL_MAX_DISTANCE=1.2` in production. |

## Known, deliberately out of scope for this checklist

* Production widget UX (shadow DOM, embed on a third-party page) — doesn't exist yet, lands in Phase 6. This checklist covers the demo page only.
* Adversarial/prompt-injection cases — that's the adversarial suite, task 4.4, a `make validate` gate item, not manual QA.
