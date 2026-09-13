"""Independent, fail-open OTLP observability for Verigence Rule Engine."""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import structlog
from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry._logs import SeverityNumber
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from verigence.audit.settings import Settings

_SAFE_REMOTE_LOG_ATTRIBUTES = frozenset(
    {
        "actor_id", "attempt", "audit_run_id", "correlation_id", "dependency",
        "duration_ms", "env", "error_category", "error_class", "error_code",
        "http_status", "journey_id", "method", "operation", "phase", "project_id",
        "result", "retryable", "route", "rule_code", "rule_id", "rule_key",
        "severity", "status", "status_code", "tenant_id",
    }
)
_FORBIDDEN_METRIC_LABELS = frozenset(
    {
        "actor_id", "audit_run_id", "correlation_id", "customer_id", "journey_id",
        "project_id", "span_id", "tenant_id", "trace_id",
    }
)
_SEVERITY = {
    "debug": SeverityNumber.DEBUG,
    "info": SeverityNumber.INFO,
    "warning": SeverityNumber.WARN,
    "error": SeverityNumber.ERROR,
    "critical": SeverityNumber.FATAL,
    "exception": SeverityNumber.ERROR,
}


@dataclass(frozen=True)
class ObservabilityState:
    logs_enabled: bool
    errors_enabled: bool
    metrics_enabled: bool
    traces_enabled: bool


_otel_logger: Any | None = None
_logger_provider: LoggerProvider | None = None
_meter_provider: MeterProvider | None = None
_tracer_provider: TracerProvider | None = None
_meter: Any | None = None
_instruments: dict[tuple[str, str], Any] = {}
_export_all_logs = False
_export_errors = False


def _resource(settings: Settings) -> Resource:
    version = (
        os.getenv("VERIGENCE_GIT_SHA")
        or os.getenv("RAILWAY_GIT_COMMIT_SHA")
        or os.getenv("VERIGENCE_RELEASE")
        or "unknown"
    )
    return Resource.create(
        {
            "service.namespace": "verigence",
            "service.name": settings.observability_service_name,
            "service.version": version,
            "deployment.environment.name": settings.env.value,
        }
    )


def _signal_endpoint_configured(signal: str) -> bool:
    return bool(
        os.getenv(f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT", "").strip()
        or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    )


def _bootstrap_warning(capability: str, reason: str, exc: BaseException | None = None) -> None:
    payload: dict[str, str] = {
        "severity": "WARNING",
        "event": "rule_engine_observability_capability_disabled",
        "capability": capability,
        "reason": reason,
        "service_name": "verigence-rule-engine",
    }
    if exc is not None:
        payload["exception_type"] = type(exc).__name__
    sys.stderr.write(json.dumps(payload, separators=(",", ":")) + "\n")


def _timeout_ms(settings: Settings) -> int:
    return int(settings.observability_export_timeout_seconds * 1000)


def _configure_logs(settings: Settings, resource: Resource) -> tuple[bool, bool]:
    global _otel_logger, _logger_provider, _export_all_logs, _export_errors
    all_logs = settings.observability_logs_enabled
    errors = settings.observability_errors_enabled
    if not all_logs and not errors:
        return False, False
    if not _signal_endpoint_configured("logs"):
        if all_logs:
            _bootstrap_warning("logs", "missing_otlp_endpoint")
        if errors:
            _bootstrap_warning("errors", "missing_otlp_endpoint")
        return False, False
    try:
        provider = LoggerProvider(resource=resource)
        provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(timeout=settings.observability_export_timeout_seconds),
                max_queue_size=settings.observability_max_queue_size,
                max_export_batch_size=settings.observability_max_export_batch_size,
                schedule_delay_millis=settings.observability_batch_delay_ms,
                export_timeout_millis=_timeout_ms(settings),
            )
        )
        _logger_provider = provider
        _otel_logger = provider.get_logger("verigence.rule_engine")
        _export_all_logs = all_logs
        _export_errors = errors
        return all_logs, errors
    except Exception as exc:  # noqa: BLE001
        _otel_logger = None
        _logger_provider = None
        _export_all_logs = False
        _export_errors = False
        if all_logs:
            _bootstrap_warning("logs", "initialization_failed", exc)
        if errors:
            _bootstrap_warning("errors", "initialization_failed", exc)
        return False, False


def _configure_metrics(settings: Settings, resource: Resource) -> bool:
    global _meter_provider, _meter
    if not settings.observability_metrics_enabled:
        return False
    if not _signal_endpoint_configured("metrics"):
        _bootstrap_warning("metrics", "missing_otlp_endpoint")
        return False
    try:
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(timeout=settings.observability_export_timeout_seconds),
            export_interval_millis=settings.observability_metric_export_interval_ms,
            export_timeout_millis=_timeout_ms(settings),
        )
        provider = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(provider)
        _meter_provider = provider
        _meter = provider.get_meter("verigence.rule_engine")
        return True
    except Exception as exc:  # noqa: BLE001
        _meter_provider = None
        _meter = None
        _bootstrap_warning("metrics", "initialization_failed", exc)
        return False


def _current_correlation_id() -> str | None:
    value = structlog.contextvars.get_contextvars().get("correlation_id")
    return str(value) if value else None


def _httpx_request_hook(span: Any, request: Any) -> None:
    correlation_id = _current_correlation_id()
    if not correlation_id:
        return
    if request.headers is not None:
        request.headers["X-Correlation-ID"] = correlation_id
    if span is not None and span.is_recording():
        span.set_attribute("verigence.correlation_id", correlation_id)


async def _httpx_async_request_hook(span: Any, request: Any) -> None:
    _httpx_request_hook(span, request)


def _configure_traces(app: FastAPI | None, settings: Settings, resource: Resource) -> bool:
    global _tracer_provider
    if not settings.observability_traces_enabled:
        return False
    if not _signal_endpoint_configured("traces"):
        _bootstrap_warning("traces", "missing_otlp_endpoint")
        return False
    try:
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(timeout=settings.observability_export_timeout_seconds),
                max_queue_size=settings.observability_max_queue_size,
                max_export_batch_size=settings.observability_max_export_batch_size,
                schedule_delay_millis=settings.observability_batch_delay_ms,
                export_timeout_millis=_timeout_ms(settings),
            )
        )
        trace.set_tracer_provider(provider)
        _tracer_provider = provider
        if app is not None:
            FastAPIInstrumentor.instrument_app(app, tracer_provider=provider, excluded_urls="/health/live")
        HTTPXClientInstrumentor().instrument(
            tracer_provider=provider,
            request_hook=_httpx_request_hook,
            async_request_hook=_httpx_async_request_hook,
        )
        SQLAlchemyInstrumentor().instrument(tracer_provider=provider)
        return True
    except Exception as exc:  # noqa: BLE001
        _bootstrap_warning("traces", "initialization_failed", exc)
        return False


def configure_observability(app: FastAPI | None, settings: Settings) -> ObservabilityState:
    resource = _resource(settings)
    logs_enabled, errors_enabled = _configure_logs(settings, resource)
    return ObservabilityState(
        logs_enabled=logs_enabled,
        errors_enabled=errors_enabled,
        metrics_enabled=_configure_metrics(settings, resource),
        traces_enabled=_configure_traces(app, settings, resource),
    )


def _is_error_event(event_dict: Mapping[str, Any]) -> bool:
    level = str(event_dict.get("level", "info")).lower()
    return level in {"error", "critical", "exception"} or bool(event_dict.get("error_code"))


def emit_otel_log(event_dict: Mapping[str, Any]) -> None:
    if _otel_logger is None:
        return
    if not _export_all_logs and not (_export_errors and _is_error_event(event_dict)):
        return
    try:
        event_name = str(event_dict.get("event", "rule_engine_event"))
        level = str(event_dict.get("level", "info")).lower()
        attributes: dict[str, Any] = {}
        for key, value in event_dict.items():
            if key not in _SAFE_REMOTE_LOG_ATTRIBUTES or value is None:
                continue
            if isinstance(value, (str, bool, int, float)):
                attributes[key] = value
        trace_id, span_id = current_trace_context()
        if trace_id:
            attributes["trace_id"] = trace_id
        if span_id:
            attributes["span_id"] = span_id
        _otel_logger.emit(
            severity_number=_SEVERITY.get(level, SeverityNumber.INFO),
            severity_text=level.upper(),
            body=event_name,
            event_name=event_name,
            attributes=attributes,
        )
    except Exception:
        return


def _safe_metric_labels(labels: Mapping[str, str]) -> dict[str, str]:
    forbidden = _FORBIDDEN_METRIC_LABELS.intersection(labels)
    if forbidden:
        names = ", ".join(sorted(forbidden))
        raise ValueError(f"High-cardinality metric labels are forbidden: {names}")
    return {str(key): str(value) for key, value in labels.items()}


def record_metric(
    name: str,
    value: float = 1,
    *,
    kind: str = "counter",
    labels: Mapping[str, str] | None = None,
) -> None:
    if _meter is None:
        return
    safe_labels = _safe_metric_labels(labels or {})
    try:
        key = (name, kind)
        instrument = _instruments.get(key)
        if instrument is None:
            if kind == "histogram":
                instrument = _meter.create_histogram(name)
            elif kind == "gauge":
                instrument = _meter.create_gauge(name)
            else:
                instrument = _meter.create_counter(name)
            _instruments[key] = instrument
        if kind == "histogram":
            instrument.record(float(value), safe_labels)
        elif kind == "gauge":
            instrument.set(float(value), safe_labels)
        else:
            instrument.add(float(value), safe_labels)
    except Exception:
        return


def record_event_metrics(event_dict: Mapping[str, Any]) -> None:
    if _meter is None:
        return
    event = str(event_dict.get("event", "rule_engine_event"))
    level = str(event_dict.get("level", "info")).lower()
    record_metric("rule_engine.events", labels={"event": event, "level": level})
    if _is_error_event(event_dict):
        labels = {"event": event}
        code = event_dict.get("error_code")
        if isinstance(code, str) and code:
            labels["error_code"] = code
        record_metric("rule_engine.errors", labels=labels)
    duration = event_dict.get("duration_ms")
    if isinstance(duration, (int, float)):
        record_metric(
            "rule_engine.event.duration_ms",
            float(duration),
            kind="histogram",
            labels={"event": event},
        )


def current_trace_context() -> tuple[str | None, str | None]:
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None, None
    return format(context.trace_id, "032x"), format(context.span_id, "016x")


def attach_correlation_to_current_span(correlation_id: str) -> None:
    span = trace.get_current_span()
    if correlation_id and span.is_recording():
        span.set_attribute("verigence.correlation_id", correlation_id)


def shutdown_observability() -> None:
    global _otel_logger, _logger_provider, _meter_provider, _tracer_provider, _meter
    for provider in (_logger_provider, _meter_provider, _tracer_provider):
        if provider is not None:
            with suppress(Exception):
                provider.shutdown()
    _otel_logger = None
    _logger_provider = None
    _meter_provider = None
    _tracer_provider = None
    _meter = None
    _instruments.clear()
