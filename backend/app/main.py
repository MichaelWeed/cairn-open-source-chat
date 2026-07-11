import logging
import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.chat import router as chat_router
from app.config import Settings, get_settings
from app.db import bootstrap
from app.logging_config import configure_logging
from app.providers.base import Provider
from app.providers.echo import EchoProvider
from app.providers.ollama import OllamaProvider
from app.ratelimit import RateLimiter
from app.vectorstore import get_chroma_client, get_document_collection

logger = logging.getLogger("app")

STATIC_DIR = Path(__file__).parent / "static"


def _default_provider(settings: Settings) -> Provider:
    # No provider registry yet (that's Phase 5's admin surface). PROVIDER
    # lets the demo page (task 1.8) exercise both round trips: `echo`
    # (default — offline, deterministic) or `ollama`.
    if os.environ.get("PROVIDER", "echo").lower() == "ollama":
        return OllamaProvider(base_url=settings.ollama_base_url, model=settings.ollama_model)
    return EchoProvider()


def create_app(settings: Settings | None = None, provider: Provider | None = None) -> FastAPI:
    configure_logging()
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.db = bootstrap(settings.database_path)
        app.state.chroma_client = get_chroma_client(settings)
        app.state.document_collection = get_document_collection(app.state.chroma_client, settings)
        logger.info(
            "app started",
            extra={
                "database_path": str(settings.database_path),
                "chroma_path": str(settings.chroma_path),
            },
        )
        try:
            yield
        finally:
            app.state.db.close()

    app = FastAPI(title="Cairn", lifespan=lifespan)
    app.state.settings = settings
    app.state.provider = provider or _default_provider(settings)
    app.state.ip_rate_limiter = RateLimiter(
        settings.rate_limit_ip_capacity, settings.rate_limit_ip_refill_per_minute
    )
    app.state.session_rate_limiter = RateLimiter(
        settings.rate_limit_session_capacity, settings.rate_limit_session_refill_per_minute
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.origins,
        allow_methods=["POST", "GET"],
        allow_headers=["*"],
    )

    app.include_router(chat_router)
    app.mount("/demo", StaticFiles(directory=STATIC_DIR / "demo", html=True), name="demo")

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        db: sqlite3.Connection = app.state.db
        try:
            db.execute("SELECT 1").fetchone()
            db_ok = True
        except sqlite3.Error:
            db_ok = False

        try:
            app.state.chroma_client.heartbeat()
            vector_store_ok = True
        except Exception:
            vector_store_ok = False

        checks = {"database": db_ok, "vector_store": vector_store_ok}
        ready = all(checks.values())
        body = {"status": "ok" if ready else "not_ready", "checks": checks}
        return JSONResponse(body, status_code=200 if ready else 503)

    return app


# Run with: uvicorn app.main:create_app --factory
# (not a module-level `app = create_app()` singleton — that would bootstrap
# the SQLite file as an import-time side effect, including under pytest.)
