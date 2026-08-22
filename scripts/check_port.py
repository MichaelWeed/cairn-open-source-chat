#!/usr/bin/env python3
"""Preflight for `make up`: fail fast, with the fix, if CAIRN_PORT is taken.

Without this, a squatted host port surfaces as the engine's raw
`bind: address already in use` after the images have already built. The
port stays fixed and operator-chosen rather than auto-selected — the embed
snippet, CORS allowlist, and any reverse proxy all reference one specific
port, so the remedy for a conflict is choosing a port, not hunting for a
free one. See MASTER_PLAN.md task 1.9.
"""

import os
import socket
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 8080


def configured_port() -> int:
    """CAIRN_PORT from the environment, else .env, else the default."""
    value = os.environ.get("CAIRN_PORT", "").strip()
    if not value:
        env_file = REPO_ROOT / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.strip().startswith("CAIRN_PORT="):
                    value = line.strip().split("=", 1)[1].strip()
    if not value:
        return DEFAULT_PORT
    try:
        port = int(value)
    except ValueError:
        port = -1
    if not 0 < port < 65536:
        print(f"port check failed: CAIRN_PORT={value!r} is not a valid port — fix it in .env")
        raise SystemExit(1)
    return port


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def own_stack_is_running() -> bool:
    """True if the occupant is Cairn's own backend container (re-running
    `make up` against a live stack is fine — compose just recreates it)."""
    compose_cmd = os.environ.get("COMPOSE_CMD", "").split()
    if not compose_cmd:
        return False
    try:
        result = subprocess.run(
            [*compose_cmd, "ps", "-q", "backend"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() != ""


def main() -> int:
    port = configured_port()
    if port_is_free(port):
        print(f"port check passed: {port} is free")
        return 0
    if own_stack_is_running():
        print(f"port check passed: {port} is held by the running Cairn stack")
        return 0
    print(f"port check failed: port {port} is already in use on this host.")
    print(f"  See what's using it:   lsof -nP -iTCP:{port} -sTCP:LISTEN")
    print("  Or pick another port:  set CAIRN_PORT in .env and re-run `make up` —")
    print("  the demo URL and CORS origin allowlist follow it automatically.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
