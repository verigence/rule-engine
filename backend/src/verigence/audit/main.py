"""main.py — FastAPI application factory for Verigence Rule Engine."""
from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from verigence.audit.observability import (
    attach_correlation_to_current_span,
    configure_observability,
    current_trace_context,
    record_metric,
    shutdown_observability,
)
from verigence.audit.settings import get_settings

logger = structlog.get_logger(__name__)

CORRELATION_ID_HEADER = "X-Correlation-ID"
TRACE_ID_HEADER = "X-Trace-ID"
_CORRELATION_SAFE = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")

_ERROR_BY_STATUS: dict[int, tuple[str, str]] = {
    400: ("RULE-REQ-400", "Invalid request"),
    401: ("RULE-AUTH-401", "Authentication required"),
    403: ("RULE-AUTH-403", "Not authorized for this operation"),
    404: ("RULE-REQ-404", "Requested resource was not found"),
    405: ("RULE-REQ-405", "HTTP method is not allowed for this resource"),
    409: ("RULE-CONFLICT-409", "Request conflicts with the current resource state"),
    422: ("RULE-REQ-422", "Request could not be processed"),
    500: ("RULE-SYS-500", "Rule Engine encountered an unexpected technical error"),
    503: ("RULE-DEP-503", "A required Rule Engine dependency is unavailable"),
}


def _is_valid_correlation_id(value: str) -> bool:
    return 1 <= len(value) <= 128 and all(c in _CORRELATION_SAFE for c in value)


def _correlation_id() -> str:
    value = structlog.contextvars.get_contextvars().get("correlation_id")
    return str(value) if value else str(uuid.uuid4())


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else "unmatched"


def _error_contract(status_code: int, correlation_id: str) -> dict[str, Any]:
    code, message = _ERROR_BY_STATUS.get(
        status_code,
        (f"RULE-HTTP-{status_code}", "Rule Engine request failed"),
    )
    return {
        "errorCode": code,
        "errorMessage": message,
        "correlationId": correlation_id,
    }


def _safe_validation_issues(errors: list[dict[str, Any]]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for error in errors:
        location = ".".join(str(part) for part in error.get("loc", ()))
        issues.append(
            {
                "field": location or "request",
                "type": str(error.get("type", "validation_error")),
            }
        )
    return issues


def create_app() -> FastAPI:
    from verigence.audit.logging_config import configure_logging  # noqa: PLC0415

    configure_logging()
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(fastapi_app: FastAPI):  # type: ignore[arg-type]
        del fastapi_app
        from verigence.audit.scheduler.batch import get_batch_scheduler  # noqa: PLC0415

        scheduler = get_batch_scheduler()
        scheduler.start()
        logger.info("audit_scheduler_started", batch_hour=settings.batch_hour)
        try:
            yield
        finally:
            scheduler.shutdown(wait=False)
            logger.info("audit_scheduler_stopped")
            shutdown_observability()

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

    observability_state = configure_observability(app, settings)
    logger.info(
        "rule_engine_observability_configured",
        logs_enabled=observability_state.logs_enabled,
        errors_enabled=observability_state.errors_enabled,
        metrics_enabled=observability_state.metrics_enabled,
        traces_enabled=observability_state.traces_enabled,
    )

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

    app.add_middleware(
        CORSMiddleware,
        allow_origins=(
            ["*"] if not settings.is_production else ["https://di-ops.verigence.app"]
        ),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[CORRELATION_ID_HEADER, TRACE_ID_HEADER],
    )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        del request
        correlation_id = _correlation_id()
        issues = _safe_validation_issues(exc.errors())
        body = _error_contract(400, correlation_id)
        body["validationIssues"] = issues
        logger.warning(
            "rule_engine_validation_error",
            error_code=body["errorCode"],
            error_category="REQUEST",
            http_status=400,
            correlation_id=correlation_id,
        )
        return JSONResponse(
            status_code=400,
            content=body,
            headers={CORRELATION_ID_HEADER: correlation_id},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        del request
        correlation_id = _correlation_id()
        if isinstance(exc.detail, dict) and "errorCode" in exc.detail:
            body = dict(exc.detail)
            body["correlationId"] = correlation_id
        else:
            body = _error_contract(exc.status_code, correlation_id)
        logger.warning(
            "rule_engine_business_error",
            error_code=body.get("errorCode"),
            error_category="HTTP",
            http_status=exc.status_code,
            correlation_id=correlation_id,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=body,
            headers={CORRELATION_ID_HEADER: correlation_id},
        )

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next) -> Response:  # type: ignore[type-arg]
        incoming = request.headers.get(CORRELATION_ID_HEADER, "")
        correlation_id = (
            incoming if incoming and _is_valid_correlation_id(incoming) else str(uuid.uuid4())
        )
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
        attach_correlation_to_current_span(correlation_id)

        start = time.perf_counter()
        try:
            response: Response = await call_next(request)
        except Exception as exc:  # noqa: BLE001
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            body = _error_contract(500, correlation_id)
            logger.error(
                "rule_engine_unhandled_error",
                error_code=body["errorCode"],
                error_category="TECHNICAL",
                error_class=type(exc).__name__,
                http_status=500,
                method=request.method,
                route=_route_template(request),
                duration_ms=duration_ms,
                correlation_id=correlation_id,
            )
            response = JSONResponse(
                status_code=500,
                content=body,
                headers={CORRELATION_ID_HEADER: correlation_id},
            )

        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        trace_id, _span_id = current_trace_context()
        if trace_id:
            response.headers[TRACE_ID_HEADER] = trace_id

        route = _route_template(request)
        status_class = f"{response.status_code // 100}xx"
        metric_labels = {
            "method": request.method,
            "route": route,
            "status_class": status_class,
        }
        record_metric("rule_engine.http.requests", labels=metric_labels)
        record_metric(
            "rule_engine.http.duration_ms",
            duration_ms,
            kind="histogram",
            labels=metric_labels,
        )
        if response.status_code >= 400:
            record_metric("rule_engine.http.errors", labels=metric_labels)

        logger.info(
            "http_request",
            method=request.method,
            route=route,
            status=response.status_code,
            duration_ms=duration_ms,
        )
        return response

    from verigence.audit.api.health import router as health_router  # noqa: PLC0415
    from verigence.audit.api.v1.audit import router as audit_router  # noqa: PLC0415
    from verigence.audit.api.v1.internal import router as internal_router  # noqa: PLC0415

    app.include_router(health_router)
    app.include_router(internal_router)
    app.include_router(audit_router)

    if settings.sentry_dsn:
        try:
            import sentry_sdk  # type: ignore[import]

            sentry_sdk.init(
                dsn=settings.sentry_dsn,
                environment=settings.env.value,
                traces_sample_rate=0.1 if settings.observability_traces_enabled else 0.0,
            )
        except ImportError:
            logger.warning("sentry_sdk_not_installed", error_code="RULE-OBS-SENTRY-MISSING")

    return app
