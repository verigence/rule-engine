"""Structured logging pipeline for Rule Engine.

Local stdout and remote OTLP/Axiom export are independent. Remote export is
non-blocking/fail-open and receives only allow-listed fields from observability.py.
"""
from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from verigence.audit.observability import emit_otel_log, record_event_metrics

_LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


class _LevelFilter:
    def __init__(self, min_level: str) -> None:
        self._min = _LEVEL_ORDER.get(min_level.upper(), 20)

    def __call__(self, logger: Any, method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        del logger
        level = str(event_dict.get("level", method)).upper()
        if _LEVEL_ORDER.get(level, 20) < self._min:
            raise structlog.DropEvent
        return event_dict


class _ObservabilityProcessor:
    """Forward one already-structured event to enabled remote signals."""

    def __call__(self, logger: Any, method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        del logger, method
        emit_otel_log(event_dict)
        record_event_metrics(event_dict)
        return event_dict


def configure_logging() -> None:
    """Configure safe structured local logging; remote telemetry stays fail-open."""
    from verigence.audit.settings import get_settings  # noqa: PLC0415

    settings = get_settings()
    level_str = settings.log_level.upper()
    use_stdout = settings.log_stdout
    is_dev = settings.env.value in ("local", "dev")

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _LevelFilter(level_str),
        _ObservabilityProcessor(),
    ]
    if use_stdout:
        processors.append(
            structlog.dev.ConsoleRenderer() if is_dev else structlog.processors.JSONRenderer()
        )
    else:
        processors.append(structlog.processors.JSONRenderer())

    stream = sys.stdout if use_stdout else open("/dev/null", "w")  # noqa: SIM115
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            _LEVEL_ORDER.get(level_str, logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=stream),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=_LEVEL_ORDER.get(level_str, logging.INFO),
        force=True,
    )
    if not is_dev:
        for noisy in ("sqlalchemy.engine", "httpx", "httpcore", "apscheduler"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
