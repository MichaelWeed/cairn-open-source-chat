# ADR-0005 — One validation command serves as both pre-push hook and CI

**Decided:** 2026-07-10 · **Status:** Accepted

## Context

The usual arrangement is a CI configuration file that gradually diverges from what
developers run locally: CI grows steps nobody runs before pushing, local runs skip
things CI enforces, and "it passed on my machine" becomes a recurring category of
wasted time. On a single-maintainer project with an intermittent cadence, that drift
is worse than on a team project — there is nobody to notice it, and long gaps mean
the local environment is often the thing that has quietly changed.

The project also makes public supply-chain claims: SBOMs, digest pinning, dependency
cooldown, vulnerability scanning. Claims like those are worth nothing if the check
that enforces them is optional or environment-dependent.

## Decision

`make validate` is the entire gate, and it is invoked from exactly two places: the
local `pre-push` hook, and the GitHub Actions workflow. Both run the identical
command. The workflow file contains no validation logic of its own — only toolchain
installation followed by `make validate`.

The gate chains: ruff, mypy, pytest, the widget build with its size budget, a
no-stub/no-TODO grep, osv-scanner across both lockfiles, a grype image scan, SBOM
regeneration with a drift check against the committed copy, digest-pin linting, and
the 14-day dependency cooldown check.

## Consequences

**Drift is structurally impossible,** not merely discouraged. There is one
definition of "green," and CI cannot enforce something a developer has no way to run.

**Cost: the local environment must carry the full toolchain.** Pushing requires
osv-scanner, grype, cyclonedx-py, a running container engine for the image scan, and
network access to PyPI and npm for the cooldown check. A machine missing any of them
cannot push at all. This is a real cost for a project resumed after long gaps on a
possibly-reimaged machine, and it is the most likely obstacle to the very first push.

**Cost: the gate is slow and network-dependent.** The cooldown check queries package
registries for every locked package; the image scan builds a container. This is not
a gate that runs in seconds.

**Cost: it is all-or-nothing.** There is no "quick" mode, so there is no supported
way to push a documentation typo without a full container build. The friction is
accepted deliberately, but it is friction.

**Unproven as of 2026-08-21.** The gate has only ever run on the author's macOS
machine. Because nothing has been pushed, the GitHub Actions half has never executed
once, and the claim that both halves agree is therefore untested. The specific
concern — SBOM drift between macOS and Linux — was examined on 2026-08-21 and looks
low-risk, since the SBOM is generated reproducibly with no platform qualifiers and
the only platform-gated packages in `uv.lock` are Windows-gated. Low risk is not no
risk, and it stays unproven until a run completes.
