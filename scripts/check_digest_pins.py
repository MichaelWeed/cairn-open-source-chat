#!/usr/bin/env python3
"""Fail if any compose.yaml `image:` or Dockerfile `FROM` isn't digest-pinned.

Deferred from task 1.3 (no compose file existed yet) to task 1.8. See
MASTER_PLAN.md §4 item 5.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
IMAGE_LINE = re.compile(r"^\s*image:\s*(\S+)\s*$")
FROM_LINE = re.compile(r"^\s*FROM\s+(\S+)", re.IGNORECASE)


def check_compose(path: Path) -> list[str]:
    violations = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        match = IMAGE_LINE.match(line)
        if match and "@sha256:" not in match.group(1):
            violations.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {match.group(1)!r} is not digest-pinned")
    return violations


def check_dockerfile(path: Path) -> list[str]:
    violations = []
    known_stages: set[str] = set()
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        match = FROM_LINE.match(line)
        if not match:
            continue
        ref = match.group(1)
        stage_name = ref.split(" ")[0] if " " not in ref else ref
        # `FROM <earlier-stage-name>` in a multi-stage build has no
        # registry digest of its own — nothing to pin.
        if ref in known_stages:
            continue
        if "@sha256:" not in ref:
            violations.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {ref!r} is not digest-pinned")
        if " AS " in line.upper():
            known_stages.add(line.upper().rsplit(" AS ", 1)[1].strip())
    return violations


def main() -> int:
    violations = []
    for compose_file in REPO_ROOT.glob("compose*.yaml"):
        violations += check_compose(compose_file)
    for dockerfile in REPO_ROOT.glob("*/Dockerfile"):
        violations += check_dockerfile(dockerfile)

    if violations:
        print(f"digest-pin lint failed: {len(violations)} un-pinned image reference(s):")
        for v in violations:
            print(f"  x {v}")
        return 1

    print("digest-pin lint passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
