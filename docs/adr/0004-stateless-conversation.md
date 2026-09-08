# ADR-0004 — The server holds no conversation state

**Decided:** 2026-07-11 · **Status:** Accepted

## Context

A support chat needs conversational context to be useful — a follow-up like "how
long does that take?" is meaningless without the previous turn. The conventional
approach is server-side sessions: store the transcript, key it by session ID, look
it up on each request.

That conventional approach quietly creates the thing this product exists to avoid.
Stored transcripts are customer data at rest: they need retention policies, backup
handling, deletion-request support, and disclosure in a privacy document. For a
product whose central claim is that conversations do not leave the operator's
infrastructure, storing them on disk is a strange place to land.

## Decision

The server keeps nothing between requests. The API accepts the last five turns from
the caller. The generic `<cairn-chat>` client keeps up to five turns in memory and
resends them, while the current demo sends an empty history array. The widget keeps
its client-generated opaque `session_id` in `sessionStorage` when available. The
identifier is used only as a rate-limit bucket key; it is linked to no account,
because none exists.

Chat message text passes through the rate limiter, retrieval, and the provider, and
is then discarded.

## Consequences

**The privacy claim becomes a structural fact.** "We do not store your conversations"
is verifiable by reading the code rather than by trusting a policy. A backup of the
system contains the operator's corpus and document metadata and no chat transcripts,
because none were ever written.

**Cost: history is capped and fragile.** Five turns is a hard limit, and the widget
keeps its transcript only in page memory, so a reload or navigation discards it.
There is no way to resume a conversation on another device, and no support agent
can look up what a customer asked earlier. For a product with an explicit
human-escalation path, that is a real limitation, and escalation will have to carry
whatever context it needs in the handoff itself.

**Cost: the client is trusted with its own history.** A client can send fabricated
history. Since answers are grounded in retrieved documents rather than in the
transcript, the blast radius is small, but it is not zero.

**Benefit: the web tier is horizontally scalable in principle.** No session
affinity, no shared session store. The actual blockers to running replicas are
elsewhere: in-memory rate limiting and the local SQLite flat-vector index
([ADR-0007](0007-sqlite-flat-vector-index.md)), not conversation state.
