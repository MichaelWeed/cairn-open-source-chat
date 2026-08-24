#!/usr/bin/env python3
"""Start and stop Cairn's grounded localhost Compose stack.

The bootstrap validates operator-owned prerequisites and reports model pull
commands when needed, but it never downloads a model itself.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILES = ("-f", "compose.yaml", "-f", "compose.live.yaml")
DEFAULT_PORT = 8080
DEFAULT_CHAT_MODEL = "llama3.1:8b-instruct"
DEFAULT_EMBEDDING_MODEL = "nomic-embed-text"
ENGINE_TIMEOUT_SECONDS = 10.0
COMPOSE_TIMEOUT_SECONDS = 1200.0
MODEL_TIMEOUT_SECONDS = 60.0
READINESS_TIMEOUT_SECONDS = 180.0

Runner = Callable[..., subprocess.CompletedProcess[str]]
Emitter = Callable[[str], None]
OpenUrl = Callable[..., Any]


class LiveError(RuntimeError):
    """An actionable operator-facing startup failure."""


def run_command(
    command: Sequence[str],
    *,
    timeout: float,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    args = list(command)
    try:
        return subprocess.run(
            args,
            cwd=REPO_ROOT,
            env=dict(env) if env is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(
            args, 127, "", f"{args[0]} could not be executed: {exc}"
        )
    except subprocess.TimeoutExpired as exc:
        raise LiveError(
            f"command timed out after {timeout:g}s: {shlex.join(args)}"
        ) from exc


def _validated_command(command: list[str], *, runner: Runner) -> bool:
    try:
        result = runner([*command, "version"], timeout=ENGINE_TIMEOUT_SECONDS)
    except LiveError:
        return False
    return result.returncode == 0


def select_compose_command(
    environ: Mapping[str, str], *, runner: Runner = run_command
) -> list[str]:
    override = environ.get("COMPOSE_CMD", "").strip()
    if override:
        try:
            command = shlex.split(override)
        except ValueError as exc:
            raise LiveError(f"COMPOSE_CMD could not be parsed: {exc}") from exc
        if not command or not _validated_command(command, runner=runner):
            raise LiveError(
                f"COMPOSE_CMD is not usable: {override!r}. Start its container engine "
                "and verify its Compose integration, then retry."
            )
        return command

    for command in (["podman", "compose"], ["docker", "compose"]):
        if _validated_command(command, runner=runner):
            return command
    raise LiveError(
        "Podman Compose or Docker Compose is required. Install Podman with "
        "`podman compose` or Docker with `docker compose`, start the engine, "
        "then retry. You may set COMPOSE_CMD to another compatible command."
    )


def compose_command(compose: Sequence[str], *arguments: str) -> list[str]:
    return [*compose, *COMPOSE_FILES, *arguments]


def run_compose(
    compose: Sequence[str],
    *arguments: str,
    runner: Runner = run_command,
    env: Mapping[str, str] | None = None,
    timeout: float = COMPOSE_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    command = compose_command(compose, *arguments)
    result = runner(command, timeout=timeout, env=env)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no error output"
        raise LiveError(f"command failed ({result.returncode}): {shlex.join(command)}\n{detail}")
    return result


def _has_non_whitespace(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(64 * 1024):
                if chunk.strip():
                    return True
    except OSError as exc:
        raise LiveError(f"corpus document {path} could not be read: {exc}") from exc
    return False


def validate_corpus(raw_path: str | None) -> tuple[Path, list[Path]]:
    if raw_path is None or not raw_path.strip():
        raise LiveError(
            "CAIRN_CORPUS_PATH is required. Set it to an absolute directory "
            "containing non-empty Markdown or PDF documents."
        )
    corpus = Path(raw_path).expanduser().resolve()
    if not corpus.is_dir():
        raise LiveError(f"CAIRN_CORPUS_PATH={corpus} is not a directory")
    documents = sorted(
        path
        for path in corpus.rglob("*")
        if path.is_file()
        and path.suffix.lower() in {".md", ".pdf"}
        and _has_non_whitespace(path)
    )
    if not documents:
        raise LiveError(
            f"CAIRN_CORPUS_PATH={corpus} contains no non-empty Markdown or PDF files"
        )
    return corpus, documents


def _read_env_file(path: Path = REPO_ROOT / ".env") -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def _settings(environ: Mapping[str, str]) -> dict[str, str]:
    values = _read_env_file()
    values.update(environ)
    return values


def configured_port(value: str | None) -> int:
    raw = (value or str(DEFAULT_PORT)).strip()
    try:
        port = int(raw)
    except ValueError:
        port = -1
    if not 0 < port < 65536:
        raise LiveError(f"CAIRN_PORT={raw!r} is not a valid port")
    return port


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def preflight_port(
    port: int,
    compose: Sequence[str],
    *,
    runner: Runner = run_command,
    env: Mapping[str, str] | None = None,
) -> None:
    if _port_is_free(port):
        return
    result = runner(
        compose_command(compose, "ps", "-q", "backend"),
        timeout=ENGINE_TIMEOUT_SECONDS,
        env=env,
    )
    if result.returncode == 0 and result.stdout.strip():
        return
    raise LiveError(
        f"port {port} is already in use. Inspect it with "
        f"`lsof -nP -iTCP:{port} -sTCP:LISTEN`, set CAIRN_PORT to an "
        "operator-chosen free port, and retry."
    )


def _parse_model_list(output: str) -> set[str]:
    models: set[str] = set()
    for index, line in enumerate(output.splitlines()):
        columns = line.split()
        if not columns or (index == 0 and columns[0].upper() == "NAME"):
            continue
        models.add(columns[0])
    return models


def wait_for_models(
    compose: Sequence[str],
    *,
    runner: Runner = run_command,
    env: Mapping[str, str] | None = None,
    timeout_seconds: float = MODEL_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> set[str]:
    deadline = time.monotonic() + timeout_seconds
    command = compose_command(compose, "exec", "-T", "ollama", "ollama", "list")
    last_detail = "Ollama did not answer"
    while True:
        try:
            result = runner(command, timeout=ENGINE_TIMEOUT_SECONDS, env=env)
        except LiveError as exc:
            last_detail = str(exc)
        else:
            if result.returncode == 0:
                return _parse_model_list(result.stdout)
            last_detail = result.stderr.strip() or result.stdout.strip() or last_detail
        if time.monotonic() >= deadline:
            raise LiveError(
                f"the Compose Ollama service did not become queryable within "
                f"{timeout_seconds:g}s: {last_detail}"
            )
        sleep(min(1.0, max(0.0, deadline - time.monotonic())))


def _model_available(model: str, available: set[str]) -> bool:
    return model in available or (":" not in model and f"{model}:latest" in available)


def require_models(
    compose: Sequence[str],
    required: Sequence[tuple[str, str]],
    available: set[str],
    *,
    emit: Emitter = print,
) -> None:
    missing = [(role, model) for role, model in required if not _model_available(model, available)]
    if not missing:
        return
    emit("Missing required Ollama models:")
    for role, model in missing:
        emit(f"  - {role}: {model}")
    emit("Cairn never downloads models. Run each command you choose, then retry:")
    for model in dict.fromkeys(model for _, model in missing):
        guidance = compose_command(compose, "exec", "ollama", "ollama", "pull", model)
        emit(f"  {shlex.join(guidance)}")
    raise LiveError("required models are unavailable")


def wait_for_readiness(
    port: int,
    *,
    timeout_seconds: float = READINESS_TIMEOUT_SECONDS,
    open_url: OpenUrl = urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    url = f"http://localhost:{port}/readyz"
    deadline = time.monotonic() + timeout_seconds
    last_detail = "no response"
    while True:
        try:
            with open_url(url, timeout=2.0) as response:
                payload = json.loads(response.read())
                checks = payload.get("checks") if isinstance(payload, dict) else None
                if (
                    response.status == 200
                    and isinstance(payload, dict)
                    and payload.get("status") == "ok"
                    and isinstance(checks, dict)
                    and checks
                    and all(value is True for value in checks.values())
                ):
                    return cast(dict[str, Any], payload)
                last_detail = f"HTTP {response.status}: {payload!r}"
        except (HTTPError, URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
            last_detail = str(exc)
        if time.monotonic() >= deadline:
            raise LiveError(
                f"Cairn did not become ready at {url} within {timeout_seconds:g}s "
                f"(last result: {last_detail})"
            )
        sleep(min(1.0, max(0.0, deadline - time.monotonic())))


def _stop_command(explicit_override: bool, compose: Sequence[str]) -> str:
    if explicit_override:
        return f"COMPOSE_CMD={shlex.quote(shlex.join(compose))} make live-down"
    return "make live-down"


def print_success(
    *,
    compose: Sequence[str],
    corpus: Path,
    port: int,
    origin_allowlist: str,
    emit: Emitter = print,
    explicit_override: bool = False,
) -> None:
    base_url = f"http://localhost:{port}"
    allowed = origin_allowlist.strip() or base_url
    emit(f"Selected engine: {shlex.join(compose)}")
    emit(f"Corpus path: {corpus}")
    emit(f"Allowed site origin: {allowed}")
    emit(f"Cairn URL: {base_url}")
    emit("Embed this exact snippet:")
    emit(f'<script src="{base_url}/widget/widget.js" defer></script>')
    emit(f'<cairn-chat api-url="{base_url}"></cairn-chat>')
    emit("A page on another origin must be included in ORIGIN_ALLOWLIST before embedding.")
    emit(f"Stop command: {_stop_command(explicit_override, compose)}")


def _failure_commands(
    compose: Sequence[str], *, emit: Emitter, explicit_override: bool = False
) -> None:
    emit(f"Inspect status: {shlex.join(compose_command(compose, 'ps'))}")
    emit(f"Inspect backend logs: {shlex.join(compose_command(compose, 'logs', 'backend'))}")
    emit(f"Stop command: {_stop_command(explicit_override, compose)}")


def run_up(
    environ: Mapping[str, str],
    *,
    runner: Runner = run_command,
    emit: Emitter = print,
    dry_run: bool = False,
) -> None:
    settings = _settings(environ)
    compose = select_compose_command(environ, runner=runner)
    corpus, documents = validate_corpus(settings.get("CAIRN_CORPUS_PATH"))
    port = configured_port(settings.get("CAIRN_PORT"))
    origin_allowlist = settings.get("ORIGIN_ALLOWLIST", "")
    command_env = dict(environ)
    command_env["CAIRN_CORPUS_PATH"] = str(corpus)
    preflight_port(port, compose, runner=runner, env=command_env)

    emit(f"Selected engine: {shlex.join(compose)}")
    emit(f"Validated corpus: {corpus} ({len(documents)} supported document(s))")
    if dry_run:
        emit("Dry run complete: engine, corpus, and fixed port preflight passed.")
        emit("No Compose service was started and no Ollama model store was inspected.")
        return

    explicit_override = bool(environ.get("COMPOSE_CMD", "").strip())
    run_compose(compose, "up", "-d", "ollama", runner=runner, env=command_env)
    available = wait_for_models(compose, runner=runner, env=command_env)
    required = [
        ("chat", settings.get("OLLAMA_MODEL", DEFAULT_CHAT_MODEL)),
        ("embedding", settings.get("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)),
    ]
    try:
        require_models(compose, required, available, emit=emit)
    except LiveError:
        emit(f"Stop command: {_stop_command(explicit_override, compose)}")
        raise

    try:
        run_compose(
            compose,
            "up",
            "--build",
            "-d",
            "backend",
            runner=runner,
            env=command_env,
        )
        wait_for_readiness(port)
    except LiveError:
        _failure_commands(compose, emit=emit, explicit_override=explicit_override)
        raise
    print_success(
        compose=compose,
        corpus=corpus,
        port=port,
        origin_allowlist=origin_allowlist,
        emit=emit,
        explicit_override=explicit_override,
    )


def run_down(
    environ: Mapping[str, str], *, runner: Runner = run_command, emit: Emitter = print
) -> None:
    compose = select_compose_command(environ, runner=runner)
    settings = _settings(environ)
    command_env = dict(environ)
    command_env["CAIRN_CORPUS_PATH"] = (
        settings.get("CAIRN_CORPUS_PATH", "").strip() or str(REPO_ROOT)
    )
    emit(f"Selected engine: {shlex.join(compose)}")
    run_compose(compose, "down", runner=runner, env=command_env)
    emit("Grounded Cairn stack stopped. Named data and model volumes were preserved.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("up", help="validate prerequisites and start the grounded stack")
    subparsers.add_parser("down", help="stop the grounded stack without deleting volumes")
    check_parser = subparsers.add_parser("check", help="run a non-mutating live preflight")
    check_parser.add_argument("--dry-run", action="store_true", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "down":
            run_down(os.environ)
        else:
            run_up(os.environ, dry_run=args.action == "check")
    except LiveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("error: interrupted; Cairn readiness was not claimed", file=sys.stderr)
        print("Stop command: make live-down", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
