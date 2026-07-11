.PHONY: validate verify eval release no-stub-check

# Merge gate — see MASTER_PLAN.md §4. Each phase adds a real check here;
# nothing in this file may claim success it hasn't earned.
validate: no-stub-check

# Scoped to source directories, not docs/*.md — the plan and README
# discuss this policy in prose, which isn't a stub marker.
no-stub-check:
	@! grep -rEn --exclude-dir=node_modules --exclude-dir=.venv \
		'TODO|FIXME|XXX|NotImplementedError' backend widget eval scripts 2>/dev/null \
		|| (echo "no-stub gate failed: remove the markers above before merging" && exit 1)

# Operator gate — see MASTER_PLAN.md §4. Populated in task 1.3 / Phase 7.
verify:
	@echo "verify: nothing to check yet — populated in task 1.3 and Phase 7"

# Eval harness — Phase 2.6.
eval:
	@echo "eval: harness lands in task 2.6"

# Release bundler — Phase 7.2.
release:
	@echo "release: bundler lands in task 7.2"
