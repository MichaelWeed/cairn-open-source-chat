# ADR-0006 — The published port is fixed and operator-chosen, never auto-selected

**Decided:** 2026-07-13 · **Status:** Accepted

## Context

When a development stack finds its published port already in use, the common
conveniences are to hunt for the next free port automatically, or to let the
container engine fail with its own raw bind error.

Neither suits this system, because the port is not a local detail here. Three
separate things reference one specific port: the embed snippet pasted into a
third-party page, the CORS origin allowlist, and any reverse proxy in front of the
stack. A server that silently came up somewhere else would leave all three pointing
at nothing, and the resulting failure — a widget that loads but whose requests are
blocked — is far harder to diagnose than a refused start.

## Decision

The published port is `CAIRN_PORT`, set by the operator in `.env`, defaulting to
8080. It is never auto-selected. `make up` preflights the port before anything
builds and fails with the specific fix — set `CAIRN_PORT` and re-run — rather than
surfacing the engine's raw bind error.

The CORS origin allowlist derives from `CAIRN_PORT` unless `ORIGIN_ALLOWLIST` is set
explicitly. That derivation lives in `config.py`, where it is unit-tested, rather
than in compose interpolation, whose nested defaults are not portable across
container engines.

## Consequences

**Failures happen at the earliest, cheapest point** — before a build, with an
actionable message naming the file to edit.

**One value stays authoritative.** Changing the port in one place moves the demo
URL, the admin URL, and the CORS allowlist together. Nothing needs to be kept in
sync by hand.

**Cost: a taken port is a hard stop,** requiring an edit before the stack will run.
That is a deliberate trade of convenience for predictability.

**Related discovery.** Making the port configurable surfaced a separate defect:
compose interpolates `.env` into `compose.yaml` but does not pass `.env` to the
container. Several documented knobs — including `ORIGIN_ALLOWLIST` and the
retrieval and rate-limit settings — were silently never reaching the application.
Every knob is now forwarded explicitly in the `environment:` block. The standing
consequence: **a new setting must be added in three places** — `.env.example`,
`compose.yaml`, and `config.py`.
