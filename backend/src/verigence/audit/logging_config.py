"""logging_config.py — Structured logging pipeline (mirrors DI D27).

Two independent output channels:
  - stdout: always safe; JSON in production/uat, pretty in local/dev
  - Axiom:  async fire-and-forget drain; never in the request path

Call configure_logging() once at process startup — before any log emission.

Configuration (all AUDIT_ prefixed env vars, read from Settings):
  AUDIT_LOG_LEVEL      DEBUG | INFO | WARNING | ERROR  (default: INFO)
  AUDIT_LOG_STDOUT     true | false                    (default: true)
  AUDIT_LOG_AXIOM      true | false                    (default: false)
  AUDIT_AXIOM_TOKEN    Axiom API token                 (required if LOG_AXIOM=true)
  AUDIT_AXIOM_DATASET  Axiom dataset name              (default: verigence-audit)

Axiom drain contract:
  - Background daemon thread; queue capacity 10,000 entries
  - Ships in batches of up to 100 every 2 seconds
  - On Axiom unavailability: stdout unaffected; drain retries with back-off
  - On buffer overflow: oldest entries dropped; axiom_buffer_dropped WARNING
  - No SDK dependency — plain httpx POST to Axiom ingest API
"""
from __future__ import annotations

import json
import logging
import queue
import sys
import threading
import time
from typing import Any

import structlog

# ── Axiom ingest endpoint ─────────────────────────────────────────────────────
_AXIOM_INGEST_URL = "https://api.axiom.co/v1/datasets/{dataset}/ingest"

# ── Axiom drain singleton ─────────────────────────────────────────────────────
_axiom_drain: "_AxiomDrain | None" = None


class _AxiomDrain:
    """Async background drain that ships log events to Axiom.

    Completely out of the request path — callers enqueue and return immediately.
    """

    _BATCH_SIZE    = 100
    _FLUSH_SECS    = 2.0
    _MAX_RETRIES   = 3
    _BACKOFF_BASE  = 1.0   # seconds; doubled per retry

    def __init__(self, token: str, dataset: str, buffer_size: int = 10_000) -> None:
        self._token   = token
        self._dataset = dataset
        self._url     = _AXIOM_INGEST_URL.format(dataset=dataset)
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=buffer_size)
        self._dropped = 0
        self._thread  = threading.Thread(
            target=self._run, name="axiom-drain", daemon=True
        )
        self._stop    = threading.Event()
        self._thread.start()
        # Use stdlib logger to avoid re-entering structlog during drain startup
        logging.getLogger(__name__).info(
            "axiom_drain_started  dataset=%s  endpoint=%s", dataset, self._url
        )

    def enqueue(self, event: dict[str, Any]) -> None:
        """Non-blocking enqueue. Drops silently on overflow."""
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self._dropped += 1
            if self._dropped % 100 == 1:
                # Emit every 100th drop via stdlib to avoid recursion
                logging.getLogger(__name__).warning(
                    "axiom_buffer_dropped  total_dropped=%d", self._dropped
                )

    def stop(self) -> None:
        """Signal drain to flush remaining entries and stop."""
        self._stop.set()
        self._thread.join(timeout=10)

    def _run(self) -> None:
        import httpx
        while not self._stop.is_set() or not self._queue.empty():
            batch: list[dict[str, Any]] = []
            deadline = time.monotonic() + self._FLUSH_SECS
            while time.monotonic() < deadline and len(batch) < self._BATCH_SIZE:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    time.sleep(0.05)
            if not batch:
                continue
            self._ship(httpx, batch)

    def _ship(self, httpx: Any, batch: list[dict[str, Any]]) -> None:
        backoff = self._BACKOFF_BASE
        for attempt in range(1, self._MAX_RETRIES + 1):
            try:
                resp = httpx.post(
                    self._url,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Content-Type":  "application/json",
                    },
                    content=json.dumps(batch),
                    timeout=10.0,
                )
                if resp.status_code < 300:
                    return
                logging.getLogger(__name__).warning(
                    "axiom_ship_failed  attempt=%d  http_status=%d",
                    attempt, resp.status_code,
                )
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "axiom_ship_failed  attempt=%d  error=%s", attempt, exc
                )
            if attempt < self._MAX_RETRIES:
                time.sleep(backoff)
                backoff *= 2


class _AxiomProcessor:
    """structlog processor that enqueues the event dict into the Axiom drain."""

    def __init__(self, drain: _AxiomDrain) -> None:
        self._drain = drain

    def __call__(
        self,
        logger: Any,  # noqa: ANN401
        method: str,
        event_dict: dict[str, Any],
    ) -> dict[str, Any]:
        self._drain.enqueue(dict(event_dict))
        return event_dict


# ── Level filtering ───────────────────────────────────────────────────────────

_LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}


class _LevelFilter:
    """Drop events below the configured minimum level."""

    def __init__(self, min_level: str) -> None:
        self._min = _LEVEL_ORDER.get(min_level.upper(), 20)

    def __call__(
        self,
        logger: Any,  # noqa: ANN401
        method: str,
        event_dict: dict[str, Any],
    ) -> dict[str, Any]:
        level = method.upper()
        if _LEVEL_ORDER.get(level, 20) < self._min:
            raise structlog.DropEvent
        return event_dict


# ── Public API ────────────────────────────────────────────────────────────────

def configure_logging() -> None:
    """Configure the structlog pipeline. Call once at process startup."""
    from verigence.audit.settings import get_settings  # noqa: PLC0415
    settings = get_settings()

    level_str   = settings.log_level.upper()
    use_stdout  = settings.log_stdout
    use_axiom   = settings.log_axiom
    is_dev      = settings.env.value in ("local", "dev")

    # ── Axiom drain setup ─────────────────────────────────────────────────────
    global _axiom_drain
    if use_axiom:
        if not settings.axiom_token:
            logging.getLogger(__name__).warning(
                "AUDIT_LOG_AXIOM=true but AUDIT_AXIOM_TOKEN is not set — Axiom drain disabled"
            )
            use_axiom = False
        else:
            _axiom_drain = _AxiomDrain(
                token=settings.axiom_token,
                dataset=settings.axiom_dataset,
            )

    # ── Shared pre-processors (run before any output) ─────────────────────────
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _LevelFilter(level_str),
    ]

    # ── Output processors ────────────────────────────────────────────────────
    output_processors: list[Any] = list(shared_processors)

    if use_axiom and _axiom_drain is not None:
        output_processors.append(_AxiomProcessor(_axiom_drain))

    if use_stdout:
        if is_dev:
            output_processors.append(structlog.dev.ConsoleRenderer())
        else:
            output_processors.append(structlog.processors.JSONRenderer())
    else:
        # At least keep a no-op renderer so structlog doesn't crash
        output_processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=output_processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            _LEVEL_ORDER.get(level_str, logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout if use_stdout else open(  # noqa: WPS515, SIM115
            "/dev/null", "w"
        )),
        cache_logger_on_first_use=True,
    )

    # Also configure stdlib logging so third-party libs (sqlalchemy, httpx) go
    # through the same level gate
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=_LEVEL_ORDER.get(level_str, logging.INFO),
        force=True,
    )

    # Silence noisy libraries in production
    if not is_dev:
        for noisy in ("sqlalchemy.engine", "httpx", "httpcore", "apscheduler"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    structlog.get_logger(__name__).info(
        "logging_configured",
        log_level=level_str,
        stdout=use_stdout,
        axiom=use_axiom,
        axiom_dataset=settings.axiom_dataset if use_axiom else None,
        env=settings.env.value,
    )
