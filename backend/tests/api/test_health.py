"""test_health.py — API-level tests for the health endpoint."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client() -> TestClient:
    os.environ.setdefault("AUDIT_SECRET_KEY", "dev-secret-key-change-in-production-must-be-32c")
    os.environ.setdefault("AUDIT_DB_URL",    "postgresql+asyncpg://localhost/test")
    os.environ.setdefault("AUDIT_DI_DB_URL", "postgresql+asyncpg://localhost/test")
    from verigence.audit.main import create_app  # noqa: PLC0415
    app = create_app()
    return TestClient(app, raise_server_exceptions=False)


def test_health_live_returns_200(client: TestClient) -> None:
    resp = client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_live_no_auth_required(client: TestClient) -> None:
    """Health endpoint must be publicly accessible — no Bearer token needed."""
    resp = client.get("/health/live", headers={})
    assert resp.status_code == 200
