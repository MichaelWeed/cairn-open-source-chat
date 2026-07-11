import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.chat import router as chat_router
from app.config import Settings, get_settings
from app.db import bootstrap
from app.logging_config import configure_logging
from app.providers.base import Provider
from app.providers.echo import EchoProvider
from app.ratelimit import RateLimiter

logger = logging.getLogger("app")


def create_app(settings: Settings | None = None, provider: Provider | None = None) -> FastAPI:
    configure_logging()
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.db = bootstrap(settings.database_path)
        logger.info("app started", extra={"database_path": str(settings.database_path)})
        try:
            yield
        finally:
            app.state.db.close()

    app = FastAPI(title="Cairn", lifespan=lifespan)
    app.state.settings = settings
    # No provider registry yet (that's Phase 5's admin surface) — defaults
    # to the offline, deterministic EchoProvider. The demo page (task 1.8)
    # wires up OllamaProvider explicitly for the "via Ollama" round trip.
    app.state.provider = provider or EchoProvider()
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

        checks = {"database": db_ok}
        ready = all(checks.values())
        body = {"status": "ok" if ready else "not_ready", "checks": checks}
        return JSONResponse(body, status_code=200 if ready else 503)

    return app


# Run with: uvicorn app.main:create_app --factory
# (not a module-level `app = create_app()` singleton — that would bootstrap
# the SQLite file as an import-time side effect, including under pytest.)
