import asyncio
import importlib
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.capabilities import router as capabilities_router
from app.api.chat import router as chat_router
from app.api.contracts import REQUEST_BODY_MAX_BYTES
from app.config import Settings, validated_settings_snapshot
from app.db import bootstrap
from app.embeddings import default_embedding_function
from app.ingest.startup import ingest_corpus
from app.logging_config import (
    AppStartedLog,
    configure_logging,
    emit_app_log,
    emit_startup_corpus_ingested,
)
from app.providers.base import Provider
from app.providers.echo import EchoProvider
from app.providers.ollama import OllamaProvider
from app.ratelimit import RateLimiter
from app.readiness import (
    BudgetReadinessProbe,
    GeminiReadinessProbe,
    OllamaCatalogProbe,
    OllamaCatalogReadinessProbe,
    ReadinessError,
    ReadinessEvaluator,
    public_readiness,
)
from app.request_accounting import (
    _APPLICATION_BINDING_AUTHORITY,
    RequestAccountingError,
    RequestAccountingSessionFactory,
    bind_application_owned_provider,
    provider_attempt_policy,
)
from app.retrieval import LocalRetrievalAdapter
from app.retrieval_contracts import (
    DistanceMeasure,
    ExactCorpusReference,
    LocalActiveScope,
    RetrievalAdapter,
    RetrievalError,
    RetrievalScope,
)
from app.retrieval_route import (
    LifecycleRetrievalRouteResolver,
    ResolvedRetrievalRoute,
    RetrievalRouteResolver,
    StaticRetrievalRouteResolver,
    binding_from_firestore_adapter,
)
from app.telemetry import (
    NullTelemetrySink,
    ReadinessTelemetryUnit,
    TelemetrySink,
    build_telemetry_projector,
    monotonic_ns,
)
from app.vectorstore import get_document_collection, get_vector_client

STATIC_DIR = Path(__file__).parent / "static"


class _RequestBodyTooLarge(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=413, detail="Request body too large")


class ChatRequestBodyLimitMiddleware:
    """Count received chat-body bytes without buffering the whole request."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or scope["path"] != "/api/v1/chat/message"
        ):
            await self.app(scope, receive, send)
            return

        received_bytes = 0

        async def receive_with_limit() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.max_bytes:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, receive_with_limit, send)
        except _RequestBodyTooLarge:
            response = JSONResponse(
                {"detail": "Request body too large"},
                status_code=413,
            )
            await response(scope, receive, send)


def _default_provider(settings: Settings) -> Provider:
    if settings.provider == "ollama":
        return OllamaProvider(base_url=settings.ollama_base_url, model=settings.ollama_model)
    if settings.provider == "echo":
        return EchoProvider()
    if settings.provider == "gemini":
        api_key = settings.gemini_api_key
        if api_key is None:
            raise RuntimeError("GEMINI_API_KEY is required when PROVIDER=gemini")
        try:
            gemini_module = importlib.import_module("app.providers.gemini")
        except ModuleNotFoundError:
            raise RuntimeError(
                "Gemini provider requires the optional 'gemini' dependency profile"
            ) from None
        factory = cast(Callable[..., Provider], gemini_module.create_gemini_provider)
        return factory(
            api_key=api_key.get_secret_value(),
            model=settings.gemini_model,
            timeout_seconds=settings.gemini_timeout_seconds,
            max_retries=settings.gemini_max_retries,
        )
    raise RuntimeError("Unsupported generation provider configuration")


async def _close_owned_provider(provider: Provider) -> None:
    close = getattr(provider, "aclose", None)
    if close is not None:
        await close()


def _firestore_policy(settings: Settings) -> tuple[ExactCorpusReference, DistanceMeasure, float]:
    scope = ExactCorpusReference(
        corpus_id=settings.firestore_corpus_id,
        corpus_version=settings.firestore_corpus_version,
    )
    measure = cast(DistanceMeasure, settings.firestore_distance_measure)
    maximum = settings.firestore_max_distance
    if maximum is None:
        raise RuntimeError("FIRESTORE_MAX_DISTANCE is required")
    return scope, measure, maximum


def _default_firestore_adapter(settings: Settings) -> RetrievalAdapter:
    from app.retrieval_firestore import (
        FirestoreRetrievalAdapter,
        create_firestore_vector_client,
    )

    scope, measure, _ = _firestore_policy(settings)
    dimensions = settings.firestore_embedding_dimensions
    timeout = settings.firestore_query_timeout_seconds
    retries = settings.firestore_max_retries
    if dimensions is None or timeout is None or retries is None:
        raise RuntimeError("Firestore retrieval configuration is incomplete")
    embedding_function = default_embedding_function(settings)
    client = create_firestore_vector_client(settings.firestore_project_id)
    return FirestoreRetrievalAdapter(
        client=client,
        embedding_function=embedding_function,
        scope=scope,
        embedding_identity=settings.firestore_embedding_identity,
        embedding_dimensions=dimensions,
        distance_measure=cast("Literal['cosine', 'euclidean']", measure),
        timeout_seconds=timeout,
        max_retries=retries,
        owns_client=True,
    )


async def _close_owned_retrieval(adapter: RetrievalAdapter) -> None:
    close = getattr(adapter, "aclose", None)
    if close is not None:
        await close()


async def _close_application_resources(
    *,
    owns_retrieval: bool,
    retrieval: RetrievalAdapter | None,
    vector_client: Any,
    db: sqlite3.Connection | None,
    catalog_probe: OllamaCatalogProbe | None,
    owns_provider: bool,
    provider: Provider,
) -> None:
    """Close owned resources in order, stopping immediately on cancellation."""

    close_failed = False

    async def close_async(operation: Callable[[], Any]) -> None:
        nonlocal close_failed
        try:
            await operation()
        except asyncio.CancelledError:
            raise
        except Exception:
            close_failed = True

    def close_sync(operation: Callable[[], Any]) -> None:
        nonlocal close_failed
        try:
            operation()
        except Exception:
            close_failed = True

    if owns_retrieval and retrieval is not None:
        await close_async(lambda: _close_owned_retrieval(retrieval))
    if vector_client is not None:
        close_sync(vector_client.close)
    if db is not None:
        close_sync(db.close)
    if catalog_probe is not None:
        await close_async(catalog_probe.aclose)
    if owns_provider:
        await close_async(lambda: _close_owned_provider(provider))
    if close_failed:
        raise ReadinessError() from None


def create_app(
    settings: Settings | None = None,
    provider: Provider | None = None,
    retrieval_adapter: RetrievalAdapter | None = None,
    retrieval_scope: RetrievalScope | None = None,
    retrieval_distance_measure: DistanceMeasure | None = None,
    retrieval_max_distance: float | None = None,
    retrieval_route_resolver: RetrievalRouteResolver | None = None,
    ollama_catalog_probe: OllamaCatalogReadinessProbe | None = None,
    budget_readiness_probe: BudgetReadinessProbe | None = None,
    request_accounting_factory: RequestAccountingSessionFactory | None = None,
    telemetry_sink: TelemetrySink | None = None,
    telemetry_monotonic_ns: Callable[[], int] | None = None,
    telemetry_utc_clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    settings = validated_settings_snapshot(settings)
    configure_logging(utc_clock=telemetry_utc_clock)
    if retrieval_route_resolver is not None and (
        retrieval_adapter is not None or retrieval_scope is not None
    ):
        raise RetrievalError("invalid_request") from None
    if retrieval_route_resolver is not None and not isinstance(
        retrieval_route_resolver, RetrievalRouteResolver
    ):
        raise RetrievalError("invalid_request") from None
    accounting_policy = provider_attempt_policy(settings)
    if request_accounting_factory is not None:
        if (
            provider is not None
            or type(request_accounting_factory) is not RequestAccountingSessionFactory
        ):
            del provider, request_accounting_factory
            raise RequestAccountingError from None
        factory_matches = False
        try:
            factory_matches = request_accounting_factory.matches_policy(accounting_policy)
        except RequestAccountingError:
            pass
        if not factory_matches:
            del request_accounting_factory
            raise RequestAccountingError from None
        selected_accounting_factory = request_accounting_factory
    else:
        selected_accounting_factory = RequestAccountingSessionFactory(policy=accounting_policy)
    owns_provider = provider is None
    selected_provider = _default_provider(settings) if provider is None else provider
    provider_accounting_binding = None
    if owns_provider:
        try:
            provider_accounting_binding = bind_application_owned_provider(
                settings,
                selected_provider,
                authority=_APPLICATION_BINDING_AUTHORITY,
            )
        except RequestAccountingError:
            # Test-only substituted defaults remain usable in controls-disabled
            # composition, but never receive accounting authority.
            provider_accounting_binding = None
    telemetry_projector = build_telemetry_projector(
        settings,
        selected_provider,
        provider_accounting_binding,
    )
    selected_telemetry_sink = NullTelemetrySink() if telemetry_sink is None else telemetry_sink
    selected_telemetry_clock = (
        monotonic_ns if telemetry_monotonic_ns is None else telemetry_monotonic_ns
    )
    selected_scope: RetrievalScope
    selected_measure: DistanceMeasure
    selected_max_distance: float
    if retrieval_adapter is None and settings.retrieval_backend == "firestore":
        selected_scope, selected_measure, selected_max_distance = _firestore_policy(settings)
    else:
        selected_scope = retrieval_scope if retrieval_scope is not None else LocalActiveScope()
        selected_measure = (
            retrieval_distance_measure if retrieval_distance_measure is not None else "squared_l2"
        )
        if type(selected_measure) is not str or selected_measure not in {
            "cosine",
            "squared_l2",
        }:
            raise RetrievalError("invalid_request") from None
        selected_max_distance = (
            settings.retrieval_max_distance
            if retrieval_max_distance is None
            else retrieval_max_distance
        )
    owns_retrieval = retrieval_adapter is None and settings.retrieval_backend == "firestore"

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.corpus_ready = False
        db: sqlite3.Connection | None = None
        vector_client = None
        selected_retrieval: RetrievalAdapter | None = retrieval_adapter
        selected_resolver = retrieval_route_resolver
        owned_catalog_probe: OllamaCatalogProbe | None = None
        try:
            db = bootstrap(settings.database_path)
            app.state.db = db
            vector_client = get_vector_client(settings)
            app.state.vector_client = vector_client
            app.state.document_collection = get_document_collection(vector_client, settings)
            if selected_retrieval is None:
                if selected_resolver is None:
                    selected_retrieval = (
                        _default_firestore_adapter(settings)
                        if settings.retrieval_backend == "firestore"
                        else LocalRetrievalAdapter(app.state.document_collection)
                    )
            if selected_resolver is None:
                assert selected_retrieval is not None
                exact_binding = (
                    binding_from_firestore_adapter(selected_scope, selected_retrieval)
                    if type(selected_scope) is ExactCorpusReference
                    else None
                )
                selected_resolver = StaticRetrievalRouteResolver(
                    ResolvedRetrievalRoute(
                        scope=selected_scope,
                        adapter=selected_retrieval,
                        exact_binding=exact_binding,
                    )
                )
                app.state.retrieval_adapter = selected_retrieval
            app.state.retrieval_route_resolver = selected_resolver
            uses_ollama_catalog = (
                settings.provider == "ollama" or settings.embedding_provider == "ollama"
            )
            selected_catalog_probe = ollama_catalog_probe if uses_ollama_catalog else None
            if uses_ollama_catalog and selected_catalog_probe is None:
                owned_catalog_probe = OllamaCatalogProbe.application_owned(
                    base_url=settings.ollama_base_url
                )
                selected_catalog_probe = owned_catalog_probe

            exact_lifecycle = type(selected_resolver) is LifecycleRetrievalRouteResolver
            expected_readiness_scope: RetrievalScope | None
            if exact_lifecycle:
                retrieval_profile = "lifecycle_exact"
                expected_readiness_scope = None
                retrieval_composition_valid = settings.deployment_mode in {
                    "development",
                    "test",
                }
            elif settings.retrieval_backend == "firestore":
                retrieval_profile = "firestore_static"
                expected_readiness_scope = _firestore_policy(settings)[0]
                retrieval_composition_valid = True
            else:
                retrieval_profile = "local_static"
                expected_readiness_scope = selected_scope
                retrieval_composition_valid = True

            def database_readiness() -> bool:
                try:
                    app.state.db.execute("SELECT 1").fetchone()
                except sqlite3.Error:
                    return False
                return True

            def vector_readiness() -> bool:
                app.state.vector_client.heartbeat()
                return True

            app.state.readiness_evaluator = ReadinessEvaluator(
                database_probe=database_readiness,
                corpus_probe=lambda: app.state.corpus_ready,
                local_vector_probe=vector_readiness,
                retrieval_route_resolver=selected_resolver,
                retrieval_profile=cast(
                    "Literal['local_static', 'firestore_static', 'lifecycle_exact']",
                    retrieval_profile,
                ),
                expected_retrieval_scope=expected_readiness_scope,
                provider_name=settings.provider,
                provider_model=(
                    settings.ollama_model
                    if settings.provider == "ollama"
                    else settings.gemini_model
                    if settings.provider == "gemini"
                    else ""
                ),
                embedding_name=settings.embedding_provider,
                embedding_model=(
                    settings.embedding_model if settings.embedding_provider == "ollama" else ""
                ),
                gemini_probe=(
                    cast(GeminiReadinessProbe, selected_provider)
                    if settings.provider == "gemini"
                    else None
                ),
                ollama_catalog_probe=selected_catalog_probe,
                budget_probe=budget_readiness_probe,
                retrieval_composition_valid=retrieval_composition_valid,
            )
            if settings.corpus_path is not None:
                summary = ingest_corpus(
                    db=app.state.db,
                    collection=app.state.document_collection,
                    corpus_path=settings.corpus_path,
                )
                emit_startup_corpus_ingested(
                    summary.document_count,
                    summary.chunk_count,
                )
            app.state.corpus_ready = True
        except Exception:
            await _close_application_resources(
                owns_retrieval=owns_retrieval,
                retrieval=selected_retrieval,
                vector_client=vector_client,
                db=db,
                catalog_probe=owned_catalog_probe,
                owns_provider=owns_provider,
                provider=selected_provider,
            )
            raise
        emit_app_log(AppStartedLog())
        try:
            yield
        finally:
            await _close_application_resources(
                owns_retrieval=owns_retrieval,
                retrieval=selected_retrieval,
                vector_client=vector_client,
                db=app.state.db,
                catalog_probe=owned_catalog_probe,
                owns_provider=owns_provider,
                provider=selected_provider,
            )

    app = FastAPI(title="Cairn", lifespan=lifespan)
    app.state.settings = settings
    app.state.provider = selected_provider
    app.state.provider_accounting_binding = provider_accounting_binding
    app.state.request_accounting_factory = selected_accounting_factory
    app.state.telemetry_projector = telemetry_projector
    app.state.telemetry_sink = selected_telemetry_sink
    app.state.telemetry_monotonic_ns = selected_telemetry_clock
    app.state.retrieval_scope = selected_scope
    app.state.retrieval_distance_measure = selected_measure
    app.state.retrieval_max_distance = selected_max_distance
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
    app.add_middleware(ChatRequestBodyLimitMiddleware, max_bytes=REQUEST_BODY_MAX_BYTES)

    app.include_router(capabilities_router)
    app.include_router(chat_router)
    app.mount("/demo", StaticFiles(directory=STATIC_DIR / "demo", html=True), name="demo")
    app.mount("/widget", StaticFiles(directory=STATIC_DIR / "widget"), name="widget")

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        telemetry = ReadinessTelemetryUnit(
            app.state.telemetry_projector,
            app.state.telemetry_sink,
            app.state.telemetry_monotonic_ns,
        )
        telemetry.start()
        report = await app.state.readiness_evaluator.evaluate()
        telemetry.complete(report)
        ready, checks = public_readiness(report)
        body = {"status": "ok" if ready else "not_ready", "checks": checks}
        return JSONResponse(body, status_code=200 if ready else 503)

    return app


# Run with: uvicorn app.main:create_app --factory
# (not a module-level `app = create_app()` singleton — that would bootstrap
# the SQLite file as an import-time side effect, including under pytest.)
