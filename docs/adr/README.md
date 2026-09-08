# Architecture Decision Records

One file per decision that is expensive to reverse. Each records the situation that
forced the choice, what was chosen, and what it costs — not a summary of how the
code works today.

The first six were written retrospectively on 2026-08-21, from evidence in the
repository and its commit history. The **Decided** date on each is when the
repository shows the decision was actually made, not when it was written down.

| # | Decision | Decided | Status |
| --- | --- | --- | --- |
| [0001](0001-self-hosted-local-first.md) | Self-hosted, single-tenant, local models by default | 2026-07-10 | Accepted |
| [0002](0002-frozen-wire-contracts.md) | Wire contracts are frozen Pydantic models that forbid unknown fields | 2026-07-11 | Accepted |
| [0003](0003-single-writer-vector-store.md) | The vector store has exactly one in-process writer | 2026-07-12 | Superseded by 0007 |
| [0004](0004-stateless-conversation.md) | The server holds no conversation state | 2026-07-11 | Accepted |
| [0005](0005-one-validation-command.md) | One validation command serves as both pre-push hook and CI | 2026-07-10 | Accepted |
| [0006](0006-fixed-operator-chosen-port.md) | The published port is fixed and operator-chosen, never auto-selected | 2026-07-13 | Accepted |
| [0007](0007-sqlite-flat-vector-index.md) | Use a local SQLite flat-vector index | 2026-09-07 | Accepted |

A new ADR is warranted when a choice constrains future work, has a cost worth
recording, or would otherwise be re-litigated by whoever picks this up next.
Routine implementation choices do not need one.
