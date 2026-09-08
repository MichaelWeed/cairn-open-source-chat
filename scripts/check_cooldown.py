#!/usr/bin/env python3
"""Fail if any locked package version is younger than the cooldown window.

Reads backend/uv.lock (TOML) and widget/package-lock.json, looks up each
pinned version's publish date on PyPI/npm, and fails the gate if anything
landed more recently than COOLDOWN_DAYS.
"""

import json
import sys
import tomllib
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

COOLDOWN_DAYS = 14
EARLY_RELEASE_APPROVALS = frozenset(
    {
        ("npm", "js-yaml", "4.3.2"),
        ("pypi", "pypdf", "6.18.0"),
    }
)
REPO_ROOT = Path(__file__).resolve().parent.parent
REQUEST_TIMEOUT_SECONDS = 10


def pypi_release_date(name: str, version: str) -> datetime | None:
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return None
    urls = data.get("urls") or []
    upload_times = [u["upload_time_iso_8601"] for u in urls if "upload_time_iso_8601" in u]
    if not upload_times:
        return None
    return min(datetime.fromisoformat(t) for t in upload_times)


def npm_release_date(name: str, version: str) -> datetime | None:
    url = f"https://registry.npmjs.org/{name}"
    try:
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return None
    time_str = (data.get("time") or {}).get(version)
    if not time_str:
        return None
    return datetime.fromisoformat(time_str.replace("Z", "+00:00"))


def backend_packages() -> list[tuple[str, str]]:
    lock = tomllib.loads((REPO_ROOT / "backend" / "uv.lock").read_text())
    return [
        (pkg["name"], pkg["version"])
        for pkg in lock.get("package", [])
        if pkg.get("source", {}).get("registry") == "https://pypi.org/simple"
    ]


def widget_packages() -> list[tuple[str, str]]:
    # widget/.npmrc sets omit=optional, so optional-only packages (e.g. the
    # libxmljs2 XML backend pulled in by cyclonedx-npm) are never installed
    # and shouldn't be held to the cooldown.
    lock = json.loads((REPO_ROOT / "widget" / "package-lock.json").read_text())
    packages = lock.get("packages", {})
    result = []
    for path, meta in packages.items():
        if not path or meta.get("optional"):
            continue
        name = meta.get("name") or path.rsplit("node_modules/", 1)[-1]
        version = meta.get("version")
        resolved = meta.get("resolved", "")
        if version and resolved.startswith("https://registry.npmjs.org/"):
            result.append((name, version))
    return result


def is_early_release_approved(ecosystem: str, name: str, version: str) -> bool:
    return (ecosystem, name, version) in EARLY_RELEASE_APPROVALS


def main() -> int:
    cutoff = datetime.now(UTC) - timedelta(days=COOLDOWN_DAYS)
    violations: list[str] = []
    unresolved: list[str] = []

    for name, version in backend_packages():
        released = pypi_release_date(name, version)
        if released is None:
            unresolved.append(f"pypi:{name}=={version}")
            continue
        if released > cutoff and not is_early_release_approved("pypi", name, version):
            violations.append(f"pypi:{name}=={version} released {released.date()} (< {COOLDOWN_DAYS}d ago)")

    for name, version in widget_packages():
        released = npm_release_date(name, version)
        if released is None:
            unresolved.append(f"npm:{name}@{version}")
            continue
        if released > cutoff and not is_early_release_approved("npm", name, version):
            violations.append(f"npm:{name}@{version} released {released.date()} (< {COOLDOWN_DAYS}d ago)")

    if unresolved:
        print(f"cooldown check: could not resolve publish date for {len(unresolved)} package(s), skipping them:")
        for u in unresolved:
            print(f"  ? {u}")

    if violations:
        print(f"cooldown check failed: {len(violations)} package(s) younger than {COOLDOWN_DAYS} days:")
        for v in violations:
            print(f"  x {v}")
        return 1

    print(f"cooldown check passed ({len(backend_packages()) + len(widget_packages())} packages checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
