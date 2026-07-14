#!/usr/bin/env python3
"""Manual QA / screen-recording helper: boots the real app (Ollama
provider + embeddings, not the echo/fake defaults) with a corpus
ingested into it, then serves it for interactive browser testing.
Not part of `make validate` — this is for a human at the demo page,
not CI. See docs/QA_CHECKLIST.md for the scenarios to run against it.

Ingestion happens inside the app's own lifespan context rather than a
second `chromadb.PersistentClient` against the same `CHROMA_PATH`, per
the single-writer invariant in DEVELOPER_README.md #1 — a second
handle desyncs the long-lived server handle's view of on-disk HNSW
segments, breaking every later query until the process restarts.

Run: `make demo` from the repo root, or directly:
    cd backend && uv run python ../scripts/dev_demo.py [--corpus PATH]

Requires a reachable Ollama (OLLAMA_BASE_URL) with OLLAMA_MODEL and
EMBEDDING_MODEL pulled -- defaults to the project's documented models
(llama3.1:8b-instruct / nomic-embed-text) unless overridden in .env.
Set PROVIDER=echo in .env first for instant, deterministic replies
when you only need to check UI/wiring, not real generation quality.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
DEFAULT_CORPUS_DIR = REPO_ROOT / "eval" / "corpus"

sys.path.insert(0, str(BACKEND_DIR))

# setdefault, not override: an explicit .env value still wins. Real
# embeddings are the point of this script -- without them, retrieval
# distance is meaningless and every citation/refusal check is unreliable.
os.environ.setdefault("EMBEDDING_PROVIDER", "ollama")
os.environ.setdefault("PROVIDER", "ollama")

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.ingest.pipeline import ingest_upload  # noqa: E402
from app.main import create_app  # noqa: E402


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


async def main(corpus_dir: Path) -> None:
    settings = get_settings()
    _check_port_free(settings.cairn_port)
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        try:
            for path in sorted(corpus_dir.glob("*.md")):
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Boot Cairn with a real, ingested corpus for manual QA."
    )
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_DIR)
    args = parser.parse_args()
    asyncio.run(main(args.corpus))
