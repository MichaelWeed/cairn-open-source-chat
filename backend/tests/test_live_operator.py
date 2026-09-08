import ast
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def live_operator() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "live.py"
    spec = importlib.util.spec_from_file_location("live_operator_for_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeRunner:
    def __init__(self, responses: Mapping[tuple[str, ...], tuple[int, str, str]]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []
        self.environments: list[Mapping[str, str] | None] = []

    def __call__(
        self,
        command: Sequence[str],
        *,
        timeout: float,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        args = list(command)
        self.calls.append(args)
        self.environments.append(env)
        returncode, stdout, stderr = self.responses.get(tuple(args), (1, "", "unavailable"))
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def test_engine_override_preference_fallback_and_missing(live_operator: ModuleType) -> None:
    override = FakeRunner({("custom", "compose", "version"): (0, "custom", "")})
    assert live_operator.select_compose_command(
        {"COMPOSE_CMD": "custom compose"}, runner=override
    ) == ["custom", "compose"]
    assert override.calls == [["custom", "compose", "version"]]

    podman = FakeRunner({("podman", "compose", "version"): (0, "podman", "")})
    assert live_operator.select_compose_command({}, runner=podman) == ["podman", "compose"]
    assert podman.calls == [["podman", "compose", "version"]]

    docker = FakeRunner(
        {
            ("podman", "compose", "version"): (1, "", "unavailable"),
            ("docker", "compose", "version"): (0, "docker", ""),
        }
    )
    assert live_operator.select_compose_command({}, runner=docker) == ["docker", "compose"]
    assert docker.calls == [
        ["podman", "compose", "version"],
        ["docker", "compose", "version"],
    ]

    missing = FakeRunner({})
    with pytest.raises(live_operator.LiveError, match="Podman Compose or Docker Compose"):
        live_operator.select_compose_command({}, runner=missing)


def test_corpus_requires_nested_nonempty_markdown_or_pdf(
    live_operator: ModuleType, tmp_path: Path
) -> None:
    with pytest.raises(live_operator.LiveError, match="CAIRN_CORPUS_PATH is required"):
        live_operator.validate_corpus(None)
    with pytest.raises(live_operator.LiveError, match="is not a directory"):
        live_operator.validate_corpus(str(tmp_path / "missing"))

    (tmp_path / "empty.md").write_text("\n")
    (tmp_path / "ignored.txt").write_text("ignored")
    with pytest.raises(live_operator.LiveError, match="non-empty Markdown or PDF"):
        live_operator.validate_corpus(str(tmp_path))

    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "guide.PDF").write_bytes(b"%PDF fixture")
    resolved, documents = live_operator.validate_corpus(str(tmp_path))
    assert resolved == tmp_path.resolve()
    assert documents == [nested / "guide.PDF"]


def test_port_validation_rejects_invalid_or_unrelated_conflicts(
    live_operator: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(live_operator.LiveError, match="not a valid port"):
        live_operator.configured_port("not-a-port")

    monkeypatch.setattr(live_operator, "_port_is_free", lambda port: False)
    compose = ["podman", "compose"]
    ps_command = tuple(live_operator.compose_command(compose, "ps", "-q", "backend"))
    unrelated = FakeRunner({ps_command: (0, "", "")})
    with pytest.raises(live_operator.LiveError, match="already in use"):
        live_operator.preflight_port(8080, compose, runner=unrelated)

    own_stack = FakeRunner({ps_command: (0, "backend-id\n", "")})
    live_operator.preflight_port(8080, compose, runner=own_stack)


def test_model_list_and_missing_guidance_never_execute_pull(live_operator: ModuleType) -> None:
    compose = ["podman", "compose"]
    list_command = tuple(
        live_operator.compose_command(compose, "exec", "-T", "ollama", "ollama", "list")
    )
    runner = FakeRunner(
        {
            list_command: (
                0,
                "NAME ID SIZE MODIFIED\nchat-model:latest abc 1 GB now\n",
                "",
            )
        }
    )
    available = live_operator.wait_for_models(compose, runner=runner, timeout_seconds=1)
    assert available == {"chat-model:latest"}

    output: list[str] = []
    with pytest.raises(live_operator.LiveError, match="required models are unavailable"):
        live_operator.require_models(
            compose,
            [("chat", "chat-model"), ("embedding", "embed-model")],
            available,
            emit=output.append,
        )
    rendered = "\n".join(output)
    assert "embedding: embed-model" in rendered
    pull_guidance = (
        "podman compose -f compose.yaml -f compose.live.yaml "
        "exec ollama ollama pull embed-model"
    )
    assert pull_guidance in rendered
    assert all("pull" not in argument for call in runner.calls for argument in call)

    duplicate_output: list[str] = []
    with pytest.raises(live_operator.LiveError):
        live_operator.require_models(
            compose,
            [("chat", "same-model"), ("embedding", "same-model")],
            set(),
            emit=duplicate_output.append,
        )
    assert sum("ollama pull same-model" in line for line in duplicate_output) == 1


def test_source_never_executes_a_pull_command(live_operator: ModuleType) -> None:
    assert live_operator.__file__ is not None
    source_path = Path(live_operator.__file__)
    tree = ast.parse(source_path.read_text())
    execution_functions = {"run", "run_command", "run_compose"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function_name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if function_name not in execution_functions:
            continue
        literals = [
            value.value
            for value in ast.walk(node)
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
        ]
        assert "pull" not in literals


class ReadyResponse:
    status = 200

    def __enter__(self) -> "ReadyResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(
            {
                "status": "ok",
                "checks": {"database": True, "vector_store": True, "corpus": True},
            }
        ).encode()


def test_readiness_success_timeout_and_exact_output(live_operator: ModuleType) -> None:
    assert live_operator.wait_for_readiness(
        8080,
        timeout_seconds=1,
        open_url=lambda *args, **kwargs: ReadyResponse(),
    ) == {
        "status": "ok",
        "checks": {"database": True, "vector_store": True, "corpus": True},
    }

    with pytest.raises(live_operator.LiveError, match="did not become ready"):
        live_operator.wait_for_readiness(
            8080,
            timeout_seconds=0,
            open_url=lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
            sleep=lambda _: None,
        )

    output: list[str] = []
    live_operator.print_success(
        compose=["podman", "compose"],
        corpus=Path("/operator/docs"),
        port=8080,
        origin_allowlist="http://localhost:4173",
        emit=output.append,
    )
    rendered = "\n".join(output)
    assert '<script src="http://localhost:8080/widget/widget.js" defer></script>' in rendered
    assert '<cairn-chat api-url="http://localhost:8080"></cairn-chat>' in rendered
    assert "Allowed site origin: http://localhost:4173" in rendered
    assert "Stop command: make live-down" in rendered
    assert "another origin must be included in ORIGIN_ALLOWLIST" in rendered

    override_output: list[str] = []
    live_operator._failure_commands(
        ["custom", "compose"], emit=override_output.append, explicit_override=True
    )
    assert "Stop command: COMPOSE_CMD='custom compose' make live-down" in override_output


def test_down_uses_grounded_files_without_deleting_volumes(live_operator: ModuleType) -> None:
    compose = ["docker", "compose"]
    command = live_operator.compose_command(compose, "down")
    assert command == [
        "docker",
        "compose",
        "-f",
        "compose.yaml",
        "-f",
        "compose.live.yaml",
        "down",
    ]
    assert "-v" not in command
    assert "--volumes" not in command

    runner = FakeRunner(
        {
            ("podman", "compose", "version"): (0, "podman", ""),
            tuple(live_operator.compose_command(["podman", "compose"], "down")): (
                0,
                "",
                "",
            ),
        }
    )
    live_operator.run_down({}, runner=runner, emit=lambda _: None)
    down_environment = runner.environments[-1]
    assert down_environment is not None
    assert down_environment["CAIRN_CORPUS_PATH"] == str(Path(live_operator.REPO_ROOT))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_executable_fake_compose_ollama_and_readiness_rehearsal(tmp_path: Path) -> None:
    repo_root = Path(__file__).parents[2]
    corpus = tmp_path / "corpus" / "nested"
    corpus.mkdir(parents=True)
    (corpus / "operator.md").write_text("# Operator corpus\n\nGrounded facts.")

    call_log = tmp_path / "compose-calls.jsonl"
    backend_marker = tmp_path / "backend-started"
    fake_compose = tmp_path / "fake-compose"
    fake_compose.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
with Path(os.environ["FAKE_COMPOSE_LOG"]).open("a") as stream:
    stream.write(json.dumps(args) + "\\n")
if args == ["version"]:
    print("fake compose 1.0")
elif args[-5:] == ["exec", "-T", "ollama", "ollama", "list"]:
    print("NAME ID SIZE MODIFIED")
    print("chat-model:latest abc 1 GB now")
    print("embed-model:latest def 1 GB now")
elif args[-3:] == ["up", "-d", "ollama"]:
    pass
elif args[-4:] == ["up", "--build", "-d", "backend"]:
    Path(os.environ["FAKE_BACKEND_MARKER"]).write_text("ready")
elif args[-1:] == ["down"]:
    pass
else:
    print(f"unexpected fake compose args: {args}", file=sys.stderr)
    raise SystemExit(2)
"""
    )
    fake_compose.chmod(0o755)

    port = _free_port()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            payload = json.dumps(
                {
                    "status": "ok",
                    "checks": {"database": True, "vector_store": True, "corpus": True},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server_holder: list[ThreadingHTTPServer] = []

    def serve_when_backend_starts() -> None:
        deadline = time.monotonic() + 10
        while not backend_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not backend_marker.exists():
            return
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        server_holder.append(server)
        server.serve_forever()

    server_thread = threading.Thread(target=serve_when_backend_starts, daemon=True)
    server_thread.start()
    env = os.environ.copy()
    env.update(
        {
            "COMPOSE_CMD": str(fake_compose),
            "CAIRN_CORPUS_PATH": str(corpus.parent),
            "CAIRN_PORT": str(port),
            "ORIGIN_ALLOWLIST": "http://localhost:4173",
            "OLLAMA_MODEL": "chat-model",
            "EMBEDDING_MODEL": "embed-model",
            "FAKE_COMPOSE_LOG": str(call_log),
            "FAKE_BACKEND_MARKER": str(backend_marker),
        }
    )
    try:
        result = subprocess.run(
            ["make", "live"],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
    finally:
        if server_holder:
            server_holder[0].shutdown()
            server_holder[0].server_close()
        server_thread.join(timeout=2)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Cairn URL: http://localhost:{port}" in result.stdout
    script_tag = f'<script src="http://localhost:{port}/widget/widget.js" defer></script>'
    assert script_tag in result.stdout
    assert f'<cairn-chat api-url="http://localhost:{port}"></cairn-chat>' in result.stdout
    calls = [json.loads(line) for line in call_log.read_text().splitlines()]
    assert calls == [
        ["version"],
        ["-f", "compose.yaml", "-f", "compose.live.yaml", "up", "-d", "ollama"],
        ["-f", "compose.yaml", "-f", "compose.live.yaml", "exec", "-T", "ollama", "ollama", "list"],
        ["-f", "compose.yaml", "-f", "compose.live.yaml", "up", "--build", "-d", "backend"],
    ]
    assert all("pull" not in argument for call in calls for argument in call)
