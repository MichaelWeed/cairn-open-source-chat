#!/usr/bin/env python3
"""Manual QA / screen-recording helper: boots the real app (Ollama
provider + embeddings, not the echo/fake defaults) with a corpus
ingested into it, then serves it for interactive browser testing.
Not part of `make validate` — this is for a human at the demo page,
not CI. See docs/QA_CHECKLIST.md for the scenarios to run against it.

Ingestion happens inside the app's own lifespan context through its
`document_collection`, the same vector-index path used by requests.
This keeps the helper aligned with the application's startup and
persistence lifecycle.

Run: `make demo` from the repo root, or directly:
    cd backend && uv run python ../scripts/dev_demo.py [--corpus PATH]

Requires a reachable Ollama (OLLAMA_BASE_URL) with OLLAMA_MODEL and
EMBEDDING_MODEL pulled -- defaults to the project's documented models
(llama3.1:8b-instruct / nomic-embed-text) unless overridden in .env.
The helper checks these prerequisites and never pulls models itself.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
DEFAULT_CORPUS_DIR = REPO_ROOT / "eval" / "corpus"

sys.path.insert(0, str(BACKEND_DIR))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.config import Settings  # noqa: E402
from app.ingest.pipeline import ingest_upload  # noqa: E402
from app.main import create_app  # noqa: E402


def _resolve_ollama_base_url(base_url: str) -> str:
    """.env's OLLAMA_BASE_URL is usually left at the compose-network
    default (http://ollama:11434) since that's what `make up` needs --
    "ollama" is the compose service name, only resolvable from inside
    that network. This script always runs on the host (make demo never
    runs inside the container), where that hostname can't resolve at
    all, so rewrite it to localhost with the same port rather than
    making the user maintain two different values for one .env key.
    """
    parsed = urlparse(base_url)
    if parsed.hostname != "ollama":
        return base_url
    rewritten = urlunparse(parsed._replace(netloc=f"localhost:{parsed.port or 11434}"))
    print(f"note: OLLAMA_BASE_URL={base_url!r} is compose's internal address; using "
          f"{rewritten!r} instead since this runs on the host, not in a container.")
    return rewritten


def _check_port_free(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            print(
                f"error: port {port} is already in use "
                f"(lsof -nP -iTCP:{port} -sTCP:LISTEN). "
                "Set CAIRN_PORT in .env to use a different one."
            )
            raise SystemExit(1) from None


def _configure_real_providers() -> None:
    os.environ["PROVIDER"] = "ollama"
    os.environ["EMBEDDING_PROVIDER"] = "ollama"


def _load_demo_settings() -> Settings:
    """Load settings only after forcing the real demo provider pair."""
    _configure_real_providers()
    settings = Settings(_env_file=REPO_ROOT / ".env")  # type: ignore[call-arg]
    settings.ollama_base_url = _resolve_ollama_base_url(settings.ollama_base_url)
    return settings


def _corpus_files(corpus_dir: Path) -> list[Path]:
    if not corpus_dir.is_dir():
        print(
            f"error: corpus directory {corpus_dir} does not exist. "
            "Restore eval/corpus or pass --corpus PATH to a directory of .md files."
        )
        raise SystemExit(1)

    paths = sorted(path for path in corpus_dir.glob("*.md") if path.read_bytes().strip())
    if not paths:
        print(
            f"error: corpus directory {corpus_dir} contains no non-empty .md files. "
            "Add at least one Markdown document or pass --corpus PATH."
        )
        raise SystemExit(1)
    return paths


def _model_is_available(model: str, available: set[str]) -> bool:
    if model in available:
        return True
    return ":" not in model and f"{model}:latest" in available


async def _check_ollama_models(
    settings: Settings, *, client: httpx.AsyncClient | None = None
) -> None:
    async def fetch_models(active_client: httpx.AsyncClient) -> set[str]:
        response = await active_client.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Ollama tags response must be an object")
        models = payload.get("models")
        if not isinstance(models, list):
            raise ValueError("Ollama tags response must contain a models list")

        available: set[str] = set()
        for item in models:
            if not isinstance(item, dict):
                raise ValueError("Ollama model entries must be objects")
            names = (item.get("name"), item.get("model"))
            if any(name is not None and not isinstance(name, str) for name in names):
                raise ValueError("Ollama model names must be strings")
            valid_names = {name for name in names if isinstance(name, str) and name}
            if not valid_names:
                raise ValueError("Ollama model entries must contain a name")
            available.update(valid_names)
        return available

    try:
        if client is None:
            async with httpx.AsyncClient(timeout=5.0) as owned_client:
                available = await fetch_models(owned_client)
        else:
            available = await fetch_models(client)
    except httpx.RequestError:
        print(
            f"error: couldn't reach Ollama at {settings.ollama_base_url} before startup. "
            "Start it with `ollama serve`, or correct OLLAMA_BASE_URL in .env."
        )
        raise SystemExit(1) from None
    except (httpx.HTTPStatusError, ValueError):
        print(
            f"error: Ollama at {settings.ollama_base_url} did not return a usable model list. "
            "Check the service and OLLAMA_BASE_URL, then retry `make demo`."
        )
        raise SystemExit(1) from None

    required = [
        ("chat", settings.ollama_model),
        ("embedding", settings.embedding_model),
    ]
    missing = [
        (role, model)
        for role, model in required
        if not _model_is_available(model, available)
    ]
    if not missing:
        return

    print("error: Ollama is reachable, but required models are unavailable:")
    for role, model in missing:
        print(f"  - {role}: {model}")
    print("Pull each missing model explicitly, then retry:")
    for model in dict.fromkeys(model for _, model in missing):
        print(f"  ollama pull {model}")
    print("Cairn does not download models automatically.")
    raise SystemExit(1)


async def main(corpus_dir: Path) -> None:
    corpus_files = _corpus_files(corpus_dir)
    # Settings' own default (env_file=".env") resolves relative to the
    # CWD, which is backend/ for this script (per `make demo`'s `cd
    # backend &&`) -- so plain Settings()/get_settings() would silently
    # miss the repo-root .env every other make target reads, and fall
    # back to defaults instead of what's actually configured. Anchor it
    # explicitly here rather than changing config.py's default, since
    # that default is correct for its other callers (tests want no .env
    # at all; compose injects real env vars and never reads the file).
    # _env_file is a documented pydantic-settings BaseSettings kwarg that
    # mypy's stubs don't model (it's injected dynamically, not part of
    # the generated __init__ signature it sees).
    settings = _load_demo_settings()
    await _check_ollama_models(settings)
    _check_port_free(settings.cairn_port)

    # `make demo` is the real-model preview path regardless of the
    # echo/fake smoke-test defaults in .env. Without real embeddings,
    # retrieval distance and citations are not meaningful.
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        try:
            for path in corpus_files:
                result = ingest_upload(
                    db=app.state.db,
                    collection=app.state.document_collection,
                    document_id=path.stem,
                    filename=path.name,
                    content=path.read_bytes(),
                )
                print(f"ingested {path.name}: {result.status} ({result.chunk_count} chunks)")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                print(
                    f"error: Ollama returned 404 for {exc.request.url} — the model is "
                    "probably not pulled yet.\n"
                    f"  ollama pull {settings.embedding_model}\n"
                    f"  ollama pull {settings.ollama_model}\n"
                    "Or point OLLAMA_MODEL / EMBEDDING_MODEL in .env at models you already "
                    "have (`ollama list`)."
                )
            else:
                print(f"error: Ollama request failed: {exc}")
            raise SystemExit(1) from None
        except httpx.ConnectError:
            print(
                f"error: couldn't reach Ollama at {settings.ollama_base_url} — "
                "is it running? (`ollama serve`, or check OLLAMA_BASE_URL in .env)"
            )
            raise SystemExit(1) from None

        url = f"http://localhost:{settings.cairn_port}/demo"
        print(
            f"\nServing at {url}  "
            f"(PROVIDER={os.environ['PROVIDER']}, "
            f"EMBEDDING_PROVIDER={os.environ['EMBEDDING_PROVIDER']})\n"
        )
        config = uvicorn.Config(
            app, host="127.0.0.1", port=settings.cairn_port, lifespan="off", log_level="info"
        )
        await uvicorn.Server(config).serve()


def run(corpus_dir: Path) -> None:
    try:
        asyncio.run(main(corpus_dir))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Boot Cairn with a real, ingested corpus for manual QA."
    )
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_DIR)
    args = parser.parse_args()
    run(args.corpus)
