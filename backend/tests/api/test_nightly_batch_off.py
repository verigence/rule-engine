"""The nightly batch is off unless AUDIT_BATCH_ENABLED is set (product owner
2026-10-29: Audit Core is the only trigger and the only findings ledger)."""
from __future__ import annotations

from fastapi.testclient import TestClient


def _started(monkeypatch, enabled: str | None) -> list[str]:
    import verigence.audit.scheduler.batch as batch
    from verigence.audit.settings import get_settings

    if enabled is None:
        monkeypatch.delenv("AUDIT_BATCH_ENABLED", raising=False)
    else:
        monkeypatch.setenv("AUDIT_BATCH_ENABLED", enabled)
    get_settings.cache_clear()
    calls: list[str] = []

    class _Scheduler:
        def start(self) -> None:
            calls.append("start")

        def shutdown(self, wait: bool = False) -> None:
            calls.append("shutdown")

    monkeypatch.setattr(batch, "get_batch_scheduler", lambda: _Scheduler())
    from verigence.audit.main import create_app

    with TestClient(create_app()):
        pass
    get_settings.cache_clear()
    return calls


def test_the_nightly_batch_does_not_start_by_default(monkeypatch) -> None:
    assert _started(monkeypatch, None) == []


def test_the_nightly_batch_starts_when_switched_on(monkeypatch) -> None:
    assert _started(monkeypatch, "true") == ["start", "shutdown"]
