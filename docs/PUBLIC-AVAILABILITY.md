# Milestone — Public availability

The next meaningful outcome, its work items, and the decisions still open.
Established 2026-08-21 by the project baseline review; see
[PROJECT-BRIEF.md](PROJECT-BRIEF.md) for why this is the outcome that matters.

**This file is a staging record, not a permanent tracker.** Work tracking belongs
in GitHub Issues and Projects (see [project.yaml](../project.yaml)). Those do not
exist yet, and the plan should not live only in a chat transcript in the meantime.
Retire this file once the issues are open, leaving the milestone definition and the
decision log behind if they are still useful.

---

## The outcome

**Cairn is public, and a person who has never seen it can clone it and get a cited
answer without asking the author anything.**

No new capability is required to get there. Everything below is publication,
accuracy, and proof.

## The success test

On a machine that has never built this project: clone, `cp .env.example .env`,
`make up`, and complete a chat round trip — with no undocumented step, no question
asked of the author, and no edit to a tracked file.

Supporting conditions: the GitHub Actions run on `main` has actually executed and
is green; no published document links to a file that does not exist; no published
claim contradicts another.

## Method

**Kanban, continuous flow, WIP limit 1.** Not Scrum: sprints need a cadence this
project does not have, and timeboxes that are routinely missed produce false
signal rather than information. The board's highest-value function here is as a
"where was I" device after a multi-week gap.

No estimates, dates, or story points — deliberately.

## Work items

In dependency order. Item 1 must land before item 5, because pushing publishes
history.

1. **Separate private planning material from published documentation.**
   Relocate `MASTER_PLAN.md` outside the repository and rewrite its inbound
   references — roughly forty across `README.md`, `DEVELOPER_README.md`, four
   `docs/` files, `CLAUDE.md`, and comments in `backend/app/retrieval.py`,
   `compose.yaml`, `Makefile`, and `osv-scanner.toml`. Blocked on decision D1.

2. **Resolve every dangling documentation reference.**
   `docs/CONTRIBUTING.md` and `docs/SOLUTION_DESIGN.md` are linked from published
   documents and do not exist. `CONTRIBUTING.md` also carries the out-of-scope
   closure policy that several other documents point at. Blocked on decision D3.

3. **Make the public claims agree.**
   The GitHub repository description says "up to 40%"; `README.md` says "a third"
   and footnotes it to two industry sources. The sourced number wins. Fixing this
   is a repository-settings change, not a code change.

4. **Rehearse the cold-clone install on a machine that has never built Cairn.**
   From a local clone first, recording every undocumented step, missing
   prerequisite, and surprise. This is the success test, so it runs before
   publication rather than after. Known candidates to hit: the Ollama model pull
   is a documented prerequisite but not preflighted, and `make up` defaults to the
   `echo` provider, so a first-run chat round trip proves plumbing rather than
   real answers.

5. **First push, and a green Actions run on `main`.**
   The only thing that turns the README badge from blank into evidence, and the
   first ever execution of the CI half of `make validate`. Note that the pre-push
   hook runs the full gate locally: osv-scanner, grype, cyclonedx-py, and a running
   container engine must all work, or the push will not start.

6. **Re-run the cold-clone rehearsal against the published repository.**
   Closes the milestone by proving the actual claim, not a local approximation.

## Open decisions

| # | Decision | Why it is blocking | Status |
| --- | --- | --- | --- |
| D1 | Strip `MASTER_PLAN.md` from all 35 commits before the first push, or move it out going forward and accept that it stays readable in history | Free today because nothing has been pushed; effectively impossible afterwards. Gates work item 1, which gates the push | **Open — needs the author** |
| D2 | Where private planning material lives once it leaves the repository | Work item 1 needs a destination. An Obsidian vault was mentioned; no path is recorded | Open |
| D3 | Write `docs/SOLUTION_DESIGN.md`, or remove the links to it | Work item 2 cannot complete either way without this | Open |
| D4 | Whether all of `MASTER_PLAN.md` §5's phase checkboxes migrate into GitHub Issues, or only the items in this milestone | Affects how much of the private plan has to be restructured rather than relocated | Open |

## Findings from the baseline review

Recorded because they are easy to rediscover expensively.

* **The public repository has existed since 2026-07-11 and is empty.** All 35
  commits are local. `git ls-remote` returns nothing.
* **The CI badge is inert.** It points at a workflow that has never executed, so
  it renders as no status rather than as proof — the pitch's primary credibility
  signal is currently switched off.
* **The SBOM drift gate is lower-risk than it looks.** The concern was that the
  committed SBOM, generated on macOS/arm64, would not match one generated on
  Linux/x86-64 in CI, failing `sbom-check` on the first run. Checked 2026-08-21:
  the SBOM is generated with `--output-reproducible`, carries no platform
  qualifiers in its purls, and the only platform-gated packages in `uv.lock` are
  Windows-gated. Still unproven until a CI run completes.
* **The first push is the most likely stall point,** because the pre-push hook
  runs the entire gate and needs a container engine plus three external scanners
  present locally.
