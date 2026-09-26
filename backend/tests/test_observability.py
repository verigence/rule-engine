from typing import Any

import pytest

from verigence.audit import observability
from verigence.audit.settings import Settings


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"secret_key": "x" * 32}
    values.update(overrides)
    return Settings(**values)


def test_observability_capabilities_are_off_by_default() -> None:
    settings = _settings()

    assert settings.observability_logs_enabled is False
    assert settings.observability_errors_enabled is False
    assert settings.observability_metrics_enabled is False
    assert settings.observability_traces_enabled is False

    state = observability.configure_observability(None, settings)
    assert state.logs_enabled is False
    assert state.errors_enabled is False
    assert state.metrics_enabled is False
    assert state.traces_enabled is False


def test_trace_switch_is_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(
        observability_logs_enabled=True,
        observability_errors_enabled=True,
        observability_metrics_enabled=True,
        observability_traces_enabled=False,
    )
    monkeypatch.setattr(observability, "_configure_logs", lambda *_args: (True, True))
    monkeypatch.setattr(observability, "_configure_metrics", lambda *_args: True)

    trace_called = False

    def _trace_probe(*_args: Any) -> bool:
        nonlocal trace_called
        trace_called = True
        return False

    monkeypatch.setattr(observability, "_configure_traces", _trace_probe)
    state = observability.configure_observability(None, settings)

    assert trace_called is True
    assert state.logs_enabled is True
    assert state.errors_enabled is True
    assert state.metrics_enabled is True
    assert state.traces_enabled is False


def test_errors_only_exports_only_error_events(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted: list[dict[str, Any]] = []

    class _Logger:
        def emit(self, **kwargs: Any) -> None:
            emitted.append(kwargs)

    monkeypatch.setattr(observability, "_otel_logger", _Logger())
    monkeypatch.setattr(observability, "_export_all_logs", False)
    monkeypatch.setattr(observability, "_export_errors", True)

    observability.emit_otel_log({"event": "rule_evaluated", "level": "info"})
    observability.emit_otel_log(
        {
            "event": "rule_engine_failure",
            "level": "error",
            "error_code": "RULE-SYS-500",
            "correlation_id": "corr-1",
            "raw_input": "must-not-be-exported",
        }
    )

    assert len(emitted) == 1
    assert emitted[0]["body"] == "rule_engine_failure"
    assert emitted[0]["attributes"]["error_code"] == "RULE-SYS-500"
    assert "raw_input" not in emitted[0]["attributes"]


def test_metric_labels_reject_business_identifiers() -> None:
    with pytest.raises(ValueError, match="High-cardinality metric labels"):
        observability._safe_metric_labels({"route": "/v1/audit", "journey_id": "j-1"})


def test_observability_batch_size_cannot_exceed_queue_size() -> None:
    with pytest.raises(ValueError, match="MAX_EXPORT_BATCH_SIZE"):
        _settings(
            observability_max_queue_size=100,
            observability_max_export_batch_size=101,
        )
