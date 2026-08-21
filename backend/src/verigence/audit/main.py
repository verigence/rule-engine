"""main.py — FastAPI application factory for Verigence Audit service.

Creates the FastAPI app, registers middleware, includes routers,
and exposes /health/live.

Lifespan: starts/stops the APScheduler nightly batch job.
"""
from __future__ import annotations

import time
import traceback
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from verigence.audit.settings import get_settings

logger = structlog.get_logger(__name__)

CORRELATION_ID_HEADER = "X-Correlation-ID"
_CORRELATION_SAFE = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")


def _is_valid_correlation_id(value: str) -> bool:
    return 1 <= len(value) <= 128 and all(c in _CORRELATION_SAFE for c in value)


def create_app() -> FastAPI:
    from verigence.audit.logging_config import configure_logging  # noqa: PLC0415
    configure_logging()
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(fastapi_app: FastAPI):  # type: ignore[arg-type]
        """Start APScheduler nightly batch on startup; stop on shutdown."""
        from verigence.audit.scheduler.batch import get_batch_scheduler  # noqa: PLC0415
        scheduler = get_batch_scheduler()
        scheduler.start()
        logger.info("audit_scheduler_started", batch_hour=settings.batch_hour)
        yield
        scheduler.shutdown(wait=False)
        logger.info("audit_scheduler_stopped")

    app = FastAPI(
        title="Verigence Audit API",
        version="0.1.0",
        description=(
            "Verigence Price Anomaly Rule Engine — audit service. "
            "Runs 85 anomaly detection rules against vehicle sale document chains. "
            "All protected endpoints require a Bearer JWT issued by the Verigence Security module "
            "(iss=verigence-security, aud=verigence-platform). "
            "Response envelope: {\"errorCode\":\"000\",\"errorMessage\":\"Success\",\"data\":{...}}."
        ),
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # ── OpenAPI security scheme ───────────────────────────────────────────────
    def custom_openapi() -> dict:  # type: ignore[return]
        if app.openapi_schema:
            return app.openapi_schema
        from fastapi.openapi.utils import get_openapi  # noqa: PLC0415
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        schema.setdefault("components", {})["securitySchemes"] = {
            "BearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": (
                    "Security-module-issued JWT. "
                    "Claims: iss=verigence-security, aud=verigence-platform, permissions[]. "
                    "Dev/CI mock format: mock.<tenantId>.<actorId>.<ROLE>[.<ROLE>...]"
                ),
            }
        }
        schema["security"] = [{"BearerAuth": []}]
        app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = custom_openapi  # type: ignore[method-assign]

    # ── CORS ─────────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=(
            ["*"] if not settings.is_production
            else ["https://di-ops.verigence.app"]
        ),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[CORRELATION_ID_HEADER],
    )

    # ── Layer 1: RequestValidationError → 400 ────────────────────────────────
    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        correlation_id = structlog.contextvars.get_contextvars().get(
            "correlation_id", str(uuid.uuid4())
        )
        return JSONResponse(
            status_code=400,
            content={
                "errorCode": "400",
                "errorMessage": "Invalid request",
                "detail": str(exc.errors()),
                "correlationId": correlation_id,
            },
            headers={CORRELATION_ID_HEADER: correlation_id},
        )

    # ── Layer 2: HTTPException pass-through ───────────────────────────────────
    @app.exception_handler(HTTPException)
    async def _http_exception_handler(
        request: Request, exc: HTTPException
    ) -> JSONResponse:
        correlation_id = structlog.contextvars.get_contextvars().get(
            "correlation_id", str(uuid.uuid4())
        )
        if isinstance(exc.detail, dict) and "errorCode" in exc.detail:
            body = dict(exc.detail)
            body.setdefault("correlationId", correlation_id)
        else:
            body = {
                "errorCode": str(exc.status_code),
                "errorMessage": str(exc.detail),
                "correlationId": correlation_id,
            }
        return JSONResponse(
            status_code=exc.status_code,
            content=body,
            headers={CORRELATION_ID_HEADER: correlation_id},
        )

    # ── Layer 3: Correlation ID middleware + catch-all ────────────────────────
    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next) -> Response:  # type: ignore[type-arg]
        incoming = request.headers.get(CORRELATION_ID_HEADER, "")
        correlation_id = (
            incoming
            if incoming and _is_valid_correlation_id(incoming)
            else str(uuid.uuid4())
        )
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)

        start = time.perf_counter()
        try:
            response: Response = await call_next(request)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "unhandled_exception",
                exc_type=type(exc).__name__,
                exc_msg=str(exc),
                traceback=traceback.format_exc(),
            )
            return JSONResponse(
                status_code=500,
                content={
                    "errorCode": "500",
                    "errorMessage": "Internal server error",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "correlationId": correlation_id,
                },
                headers={CORRELATION_ID_HEADER: correlation_id},
            )
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        logger.info(
            "http_request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=duration_ms,
        )
        return response

    # ── Routers ───────────────────────────────────────────────────────────────
    from verigence.audit.api.health import router as health_router            # noqa: PLC0415
    from verigence.audit.api.v1.internal import router as internal_router    # noqa: PLC0415
    from verigence.audit.api.v1.audit import router as audit_router          # noqa: PLC0415

    app.include_router(health_router)
    app.include_router(internal_router)
    app.include_router(audit_router)

    # ── Sentry ────────────────────────────────────────────────────────────────
    if settings.sentry_dsn:
        try:
            import sentry_sdk  # type: ignore[import]
            sentry_sdk.init(
                dsn=settings.sentry_dsn,
                environment=settings.env.value,
                traces_sample_rate=0.1,
            )
        except ImportError:
            logger.warning("sentry_sdk not installed; error tracking disabled")

    return app
