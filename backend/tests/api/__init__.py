"""test_health.py — API-level tests for the health endpoint."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from verigence.audit.main import create_app


@pytest.fixture(scope="module")
def client() -> TestClient:
    # Set minimal env vars so Settings validates
    import os  # noqa: PLC0415
    os.environ.setdefault("AUDIT_SECRET_KEY", "dev-secret-key-change-in-production-must-be-32c")
    os.environ.setdefault("AUDIT_DB_URL", "postgresql+asyncpg://localhost/test")
    os.environ.setdefault("AUDIT_DI_DB_URL", "postgresql+asyncpg://localhost/test")
    app = create_app()
    return TestClient(app, raise_server_exceptions=False)


def test_health_live_returns_200(client: TestClient) -> None:
    resp = client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
