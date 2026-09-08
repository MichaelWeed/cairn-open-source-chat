# Contributing to Cairn

Cairn is a pre-release, Apache-2.0 open-source project. Contributions are
welcome when they strengthen the supported self-hosted chat, its documentation,
or its verification evidence without expanding the project's deliberate scope.
Read [README.md](../README.md) for the public overview,
[DEVELOPER_README.md](../DEVELOPER_README.md) for operating and extension
guidance, and [ARCHITECTURE.md](ARCHITECTURE.md) for built-versus-designed
system boundaries.

## Preparing a change

Keep one component and its tests in each change. Preserve a releasable tree: no
stubs, TODOs, or partial implementations. Before submitting a change, run
`make validate`. The required commands in [`.agent-test-contract.yaml`](../.agent-test-contract.yaml)
are authoritative, and the final gate is `make validate`.

The chat and SSE models in `backend/app/api/contracts.py` are frozen. Any real
contract change must update the widget, tests, and documentation together and
receive review as one coordinated change.

If a dependency changes, update its lockfile and SBOM as required by the gate,
and add a one-line justification to [DEPENDENCIES.md](DEPENDENCIES.md). Review
the complete diff before proposing it; partial-diff review is not sufficient.

## Scope boundary

The supported boundary is grounded, cited answers over an operator-supplied
corpus, low-confidence refusal, local-model-first self-hosting, a generic
embeddable widget, order-status deep links, and clean human escalation. CRM
synchronization, auth-aware or per-user answers, multi-tenancy, and voice are
out of scope. Do not reopen those proposals as ordinary feature requests; they
belong outside this repository's roadmap.

## Security reports

Do not disclose suspected vulnerabilities in a public issue. Follow the private
reporting route in [SECURITY.md](SECURITY.md).
