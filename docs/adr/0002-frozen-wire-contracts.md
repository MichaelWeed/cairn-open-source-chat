# ADR-0002 — Wire contracts are frozen Pydantic models that forbid unknown fields

**Decided:** 2026-07-11 · **Status:** Accepted

## Context

The chat API is consumed by a widget that will be embedded on pages the maintainer
does not control and cannot update. Once someone pastes an embed snippet into their
site, that client is effectively permanent. A backend that silently accepts and
ignores unexpected fields lets client and server drift apart quietly, and the drift
surfaces later as behaviour nobody can reproduce.

The streaming surface makes this worse: an SSE event stream has six event types
whose payloads are easy to change accidentally and hard to test end to end.

## Decision

Every request, response, and SSE event shape is a Pydantic model in
`backend/app/api/contracts.py`, declared with `extra="forbid"`. Unknown fields are
rejected with a validation error rather than ignored. Structural limits — the
500-character message cap, the 5-turn history limit — are enforced in the contract
itself, so they hold before any handler code runs.

The file is treated as frozen: it changes only in a change that updates the widget,
the tests, and the documentation together.

## Consequences

**Errors arrive early and loudly.** A malformed or outdated client gets a 422 with a
validation body at the edge, not a confusing partial response from deep inside a
handler. The QA checklist verifies all seven malformed-request cases return 422.

**Input caps are structural.** The message cap is not a policy a handler might
forget to check; it is part of the type. An attacker cannot reach the provider with
an oversized payload by finding a code path that skipped the check.

**Cost: changes are deliberately expensive.** Adding a field to the chat request is
a coordinated multi-file change. This is the intended friction, but it does slow
routine iteration, and it means the widget cannot ship a field ahead of the server.

**Cost: strictness is user-visible.** A client sending a harmless extra field gets a
hard rejection. For a widely embedded public API this would be hostile; for a widget
whose only supported client ships from this repository, it is the right trade.

**Note on error transport.** Errors ride the SSE stream as `error` events with HTTP
200, rather than as HTTP status codes — a rate-limited request is a 200 whose stream
carries `rate_limited`. This is a consequence of streaming, and it surprises people;
it is verified explicitly in `docs/QA_CHECKLIST.md`.
