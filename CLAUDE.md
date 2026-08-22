# Working Agreements

Applies to any agent-assisted contribution to this repo. See [DEVELOPER_README.md](DEVELOPER_README.md) for architecture and contracts, and [docs/PUBLIC-AVAILABILITY.md](docs/PUBLIC-AVAILABILITY.md) for the active publication milestone.

* **One component + its tests per task.** Never bundle two components in one change. Keep each change to a single independently verifiable boundary.
* **The tree is releasable after every merge.** No stubs, no TODOs, no half-finished implementations — enforced by the no-stub grep gate in `make validate`.
* **Contracts are frozen.** `backend/app/api/contracts.py` (once it exists, task 1.4) is the source of truth for the chat API and SSE events. Change it only via a PR that updates widget, tests, and docs together.
* **`make validate` must be green before push.** The pre-push hook and GitHub Actions run the identical command.
* **New dependencies need a one-line justification** recorded in `docs/DEPENDENCIES.md` at the moment they land in a lockfile.
* **Review is a full-diff read before merge.** No partial-diff approvals.
* **Out-of-scope requests** (CRM sync, auth-aware answers, multi-tenancy, voice) are closed per `docs/CONTRIBUTING.md`, not re-litigated per task.
