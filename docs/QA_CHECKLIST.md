# Manual QA Checklist

Companion to `make eval` (DEVELOPER_README.md §6), which measures groundedness,
citation accuracy, and refusal correctness quantitatively against a question
set. This checklist is the qualitative counterpart: does it actually feel
right in a browser. Run it after any change to `backend/app/static/demo/`,
the chat SSE contract, or the retrieval/refusal pipeline. The production widget's
deterministic neutral-host browser matrix is documented in [WIDGET.md](WIDGET.md)
and runs through `widget`'s required test command.

## Setup

```
ollama serve                           # in another terminal, if not already running
ollama pull llama3.1:8b-instruct        # or set OLLAMA_MODEL in .env to a model you have
ollama pull nomic-embed-text            # or set EMBEDDING_MODEL in .env to one you have
make demo
```

`make demo` (`scripts/dev_demo.py`) boots the real app — real Ollama provider,
real embeddings, not the `echo`/`fake` defaults `make up` uses out of the box
— ingests `eval/corpus/` (the bundled returns/shipping/warranty sample) into
it, and serves it at `http://localhost:$CAIRN_PORT/demo` (8080 by default).
It checks for a non-empty Markdown corpus, a reachable Ollama service, and both
configured models before starting the server. It prints actionable fixes but
never pulls models automatically. Point `--corpus` at a different directory of
non-empty `.md` files to QA against other content.

`make up` is only the echo/fake container plumbing smoke test under the checked-in
configuration. It does not ingest this corpus, produce a grounded cited answer,
or expose an `/admin` route.

Re-running `make demo` is safe — `ingest_upload()`'s content-hash reindex
reports `unchanged` for anything already ingested rather than duplicating it.

## Deterministic retrieval-integrity gate

The non-live backend suite covers mixed below/equal/above-threshold results,
all-above refusal, stable adapter order, exact 12,000/12,001-character boundaries,
and one-to-one correspondence between eligible context documents and first-seen
citations. Poisoned source/text values containing closing tags, fake JSON, controls,
Unicode, and instruction-shaped prose must round-trip as JSON strings without
creating sibling chunks or server-owned fields. The adversarial profile specifically
denies DNS resolution; socket creation and connection; SQLite connection; built-in
and `Path` open/read calls; `os.getenv` plus credential/emulator environment-key
reads; subprocess run/Popen/check-output; the Firestore client factory; and
Gemini/Ollama stream methods. Direct negative controls prove each patched channel
fires. It is structural offline evidence, not a live model or universal
prompt-injection claim.

## API / functional (curl against the live server, not TestClient)

Automated readiness tests cover unchanged `/healthz` and `/readyz` bodies plus
offline Echo/fake, mocked Ollama and Gemini model discovery, static local/Firestore,
and injected lifecycle-exact profiles. They verify exact byte/model/name limits,
one shared Ollama catalog call, one selected route probe, cancellation, ownership,
and absence of raw model, endpoint, scope, error, or credential data. Validation
never contacts a live provider or store.
The focused request-accounting matrix also verifies exact 0/1/1-to-2 attempt policy,
cumulative usage, immutable price snapshots, retry and cleanup uncertainty,
final-send/callback ordering, cancellation identity, no public usage events, and no
live provider or store access.

`backend/tests/` covers this in-process; these rows exist because the real
ASGI server + real HTTP transport can behave differently than `TestClient`
(timeouts especially — see finding below). Re-run after any change to
`chat.py`, `contracts.py`, `ratelimit.py`, or a provider/embedding adapter.

| # | Scenario | Expected result | Status |
| - | --- | --- | --- |
| A1 | `/healthz`, `/readyz`, `/demo` redirect, `/demo/` static, unknown route | 200/200/307→200/200/404 respectively | Verified 2026-07-28 |
| A2 | Real chat request against a real local model, end to end | Grounded, cited answer streams correctly | Verified 2026-07-28 — see timeout finding below; broken until fixed same session |
| A3 | Contract edge cases: message >500 chars, empty message, extra field, bad `history` role, `history` >5 turns, malformed JSON, missing `session_id` | 422 with a Pydantic validation body in every case | Verified 2026-07-28 |
| A4 | Disallowed `Origin` header / allowed origin / CORS preflight `OPTIONS` | 403 / 200 / 200 with correct `access-control-*` headers | Verified 2026-07-28 |
| A5 | IP rate limit (capacity 20, refill 20/min) — 25 rapid requests, distinct sessions | Requests 1–20 succeed, 21–25 get a `rate_limited` error event (HTTP 200 — the error rides the SSE stream, not the status code) | Verified 2026-07-28, exact boundary |
| A6 | Session rate limit (capacity 10, refill 10/min) — 13 rapid requests, fixed `session_id` | Requests 1–10 succeed, 11–13 get the session-specific `rate_limited` message | Verified 2026-07-28, exact boundary |

**Finding, fixed same session: `OllamaProvider`/`OllamaEmbeddingFunction` used httpx's unconfigured 5s default timeout.** Every single real chat request against a real local model failed with `httpx.ReadTimeout` → a false `provider_unavailable` error — 100% reproducible, not an edge case (a warm 8B model took ~2.2s past the point retrieval finished, cold noticeably longer; still nowhere near enough margin, and `test_provider_ollama.py` mocks the transport so no automated test could catch it). Fixed with an explicit generous read timeout. If `make demo` or `make eval` ever produces `provider_unavailable` again, check this first before assuming the model or Ollama itself is broken.

## Browser scenarios

Each row is a scenario, its expected result, and status as of the last time
it was actually driven through a browser (not just read from source). Update
the date when you re-verify; add a row if you find a new scenario worth
tracking.

| # | Scenario | Expected result | Status |
| - | --- | --- | --- |
| 1 | Ask an in-corpus question | Grounded answer, citation chips for the matching document(s) | Verified 2026-07-28 |
| 2 | Ask an out-of-corpus question | Refusal renders as a normal assistant bubble (not error-styled); no citations; provider never called | Verified 2026-07-28 |
| 3 | Click a citation chip | Real `http(s)` URL → navigates; anything else → inert badge, not a dead link | Inert fallback verified 2026-07-28. Manifested startup HTTP(S) citations are covered by automated startup and SSE tests; public-link browser navigation awaits the separately approved neutral-corpus proof. |
| 4 | Multi-turn conversation on a viewport shorter than the content | Latest reply + input box both in view, no manual scrolling | Verified 2026-07-28 |
| 5 | Resize the viewport mid-conversation (phone rotation is the realistic trigger) | Scroll position re-settles to the latest message | **Fixed same session** (was broken — stale scrollTop left the log showing the earliest exchange). Verified via a synthetic `dispatchEvent(new Event('resize'))`, since this automation tool doesn't fire a real `resize` event on viewport change (confirmed via a counter) — a real device rotation does. |
| 6 | Light vs. dark color scheme, quantified contrast | WCAG AA (≥4.5:1 normal text) | Verified 2026-07-28 via computed-style luminance calculation, not eyeballing. Dark mode: 6.4–15.5:1 across all text/bg pairs, comfortable margin. Light mode: passes everywhere but secondary text (`.tagline`, `footer`) sits at 4.59:1 — over the floor but with almost no safety margin. Consider nudging `--fg-muted` slightly darker in light mode. |
| 7 | Tab to input, then send button | Visible focus ring on both | Verified 2026-07-28 |
| 8 | HTML/script injection in a message (`<img src=x onerror=...>`) | Rendered as literal escaped text, script never executes | Verified 2026-07-28 empirically (not just by reading the `textContent` usage) — injected a callback the payload would set on success; it never fired, and the DOM shows the tag HTML-escaped |
| 9 | Emoji, RTL (Arabic/Hebrew), CJK, styled Unicode in a message | Renders correctly, no layout breakage | Verified 2026-07-28 |
| 10 | 3 rapid/overlapping sends before the first reply finishes | Each reply resolves against its own question, no cross-contamination | Verified 2026-07-28 — no disable-during-send exists, so this can genuinely happen; confirmed safe |
| 11 | Message over the 500-char cap submitted through the UI (no client-side length limit exists) | Clean error, not a raw HTTP/validation dump | Verified 2026-07-28. **Recommendation, not fixed:** no character counter or client-side cap exists — a user gets zero feedback until after hitting send. Standard chat-UI practice is a visible counter approaching the limit. |
| 12 | Network failure before any response arrives (`fetch()` throws, or non-2xx status) | Clean, plain-language error; no stuck state | **Fixed same session** — was a raw `TypeError: Failed to fetch` shown directly to the user, plus a permanently-orphaned empty bubble with its "typing" dot animating forever. Verified via killing the server and via a 500-status simulation. |
| 13 | Network failure mid-stream, after some data already arrived (server dies while generating) | Same clean error; no stuck state | **Fixed same session** — was completely silent: status text frozen on "Generating a reply" forever, pulsing dot forever, *no error shown at all, not even in the console*. Confirmed via killing the server mid-response and, more reliably, via a deterministic `fetch` override that lets N reads through before erroring. |
| 14 | Long single-word input (e.g. a long URL) | Wraps inside the bubble, doesn't overflow the card | Verified 2026-07-28 |
| 15 | Press Enter with text in the input | Submits the form, same as clicking send | **Resolved as a tooling limitation, not a page bug.** Rigorously retested: confirmed `document.activeElement.id === "input"` and the correct text present immediately before pressing Return, and the form still didn't submit. But: no `keydown` handler exists anywhere in the page (only a `submit` listener that runs after the browser's native trigger), and submit-on-Enter for a single-text-input form is baseline HTML behavior requiring zero JS. This matches a known class of CDP/automation limitation — synthetic key dispatch not satisfying the `isTrusted`-gated internal check browsers use for this specific default action, even though the same synthetic keystroke correctly lands characters in the field. High confidence this works for real users on real keyboards; a real-keyboard sanity check is still worth 30 seconds before shipping. |
| 16 | Full pass in a real browser (Chrome/Safari/Firefox), not an automated one | Same as above | **Still unverified.** Everything in this file has been checked through an automated preview browser tool, never a real one. |
| 17 | `RETRIEVAL_MAX_DISTANCE` refusal threshold against the *documented default* models (`llama3.1:8b-instruct` / `nomic-embed-text`) | Refusal/answer boundary behaves sanely | **Still unverified end to end.** Every live test to date (including this session) used whichever Ollama models happened to be available locally, not the documented defaults. Run `make eval` after pulling the documented defaults before trusting `RETRIEVAL_MAX_DISTANCE=1.2` in production. |

## Known, deliberately out of scope for this checklist

* Embeddable widget UX is covered by the automated neutral-host matrix, including
  responsive geometry, computed contrast, safe areas, strict CSP, CORS denial,
  keyboard/focus, reduced motion, forced colors, history, and clear behavior. This
  manual checklist still covers the separate demo page only.
* Live semantic prompt-injection resistance remains outside this manual checklist;
  the built adversarial retrieval gate proves deterministic structural properties
  without contacting a model.
