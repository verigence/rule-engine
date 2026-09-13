"""test_audit_rules.py — API-level tests for POST /audit/rules (rule authoring).

Uses a fake AsyncSession (no real Postgres) overriding get_audit_session,
matching this repo's existing convention of no DB-backed route tests --
these exercise the route's own validation/dispatch logic (auth, duplicate
rule_code, comparator/severity/phase/condition validation), not the SQL
itself against a live schema.
"""
from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("AUDIT_SECRET_KEY", "dev-secret-key-change-in-production-must-be-32c")
os.environ.setdefault("AUDIT_DB_URL",    "postgresql+asyncpg://localhost/test")
os.environ.setdefault("AUDIT_DI_DB_URL", "postgresql+asyncpg://localhost/test")

from verigence.audit.main import create_app  # noqa: E402
from verigence.audit.repositories.database import get_audit_session  # noqa: E402

_TENANT = "tenant-rules-1"
_AUTH_HEADER = {"Authorization": f"Bearer mock.{_TENANT}.user-1.TENANT_ADMIN"}


class _FakeResult:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self._rows = rows or []

    def first(self) -> Any | None:
        return self._rows[0] if self._rows else None


class _FakeSession:
    """Records every statement it's asked to execute; answers the one
    pre-existence SELECT the route issues, from a caller-seeded set."""

    def __init__(self, existing_rule_codes: set[str] | None = None) -> None:
        self.existing = set(existing_rule_codes or set())
        self.executed: list[tuple[str, dict]] = []

    async def execute(self, stmt: Any, params: dict | None = None) -> _FakeResult:
        sql = str(stmt)
        params = params or {}
        self.executed.append((sql, params))
        if "SELECT 1 FROM audit.audit_rules WHERE rule_code" in sql:
            return _FakeResult([1] if params.get("rc") in self.existing else [])
        if "INSERT INTO audit.audit_rules" in sql:
            self.existing.add(params["rule_code"])
            return _FakeResult()
        raise AssertionError(f"unexpected SQL sent to fake session: {sql}")


@pytest.fixture
def fake_session() -> _FakeSession:
    return _FakeSession()


@pytest.fixture
def client(fake_session: _FakeSession) -> TestClient:
    app = create_app()

    async def _override() -> AsyncGenerator[_FakeSession, None]:
        yield fake_session

    app.dependency_overrides[get_audit_session] = _override
    return TestClient(app, raise_server_exceptions=False)


def _valid_body(**overrides: Any) -> dict:
    body = {
        "ruleCode": "TEST_NEW_RULE",
        "category": "PRICE",
        "comparator": "GT",
        "severity": "WARNING",
        "findingMessage": "Ex-showroom price deviates by {diff}",
    }
    body.update(overrides)
    return body


def test_create_rule_succeeds_with_minimal_valid_body(client: TestClient, fake_session: _FakeSession) -> None:
    resp = client.post(f"/v1/tenants/{_TENANT}/audit/rules", json=_valid_body(), headers=_AUTH_HEADER)
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"] == {"created": "TEST_NEW_RULE"}
    assert "TEST_NEW_RULE" in fake_session.existing


def test_create_rule_rejects_duplicate_rule_code(client: TestClient, fake_session: _FakeSession) -> None:
    fake_session.existing.add("ALREADY_THERE")
    resp = client.post(
        f"/v1/tenants/{_TENANT}/audit/rules",
        json=_valid_body(ruleCode="ALREADY_THERE"),
        headers=_AUTH_HEADER,
    )
    assert resp.status_code == 409
    assert "ALREADY_THERE" in resp.json()["errorMessage"]


def test_create_rule_rejects_unknown_comparator(client: TestClient) -> None:
    resp = client.post(
        f"/v1/tenants/{_TENANT}/audit/rules",
        json=_valid_body(comparator="BOGUS"),
        headers=_AUTH_HEADER,
    )
    assert resp.status_code == 400


def test_create_rule_rejects_unknown_severity(client: TestClient) -> None:
    resp = client.post(
        f"/v1/tenants/{_TENANT}/audit/rules",
        json=_valid_body(severity="URGENT"),
        headers=_AUTH_HEADER,
    )
    assert resp.status_code == 400


def test_create_rule_rejects_unknown_phase(client: TestClient) -> None:
    resp = client.post(
        f"/v1/tenants/{_TENANT}/audit/rules",
        json=_valid_body(phases=["NOT_A_REAL_PHASE"]),
        headers=_AUTH_HEADER,
    )
    assert resp.status_code == 400


def test_create_rule_rejects_malformed_condition_expression(client: TestClient) -> None:
    resp = client.post(
        f"/v1/tenants/{_TENANT}/audit/rules",
        json=_valid_body(conditionExpression="totally_unknown_atom:whatever"),
        headers=_AUTH_HEADER,
    )
    assert resp.status_code == 400


def test_create_rule_accepts_well_formed_condition_expression(client: TestClient) -> None:
    resp = client.post(
        f"/v1/tenants/{_TENANT}/audit/rules",
        json=_valid_body(
            ruleCode="TEST_WITH_CONDITION",
            conditionExpression="doc_present:gate_pass AND doc_absent:ndc",
        ),
        headers=_AUTH_HEADER,
    )
    assert resp.status_code == 201, resp.text


def test_create_rule_rejects_blank_finding_message(client: TestClient) -> None:
    resp = client.post(
        f"/v1/tenants/{_TENANT}/audit/rules",
        json=_valid_body(findingMessage="   "),
        headers=_AUTH_HEADER,
    )
    assert resp.status_code == 400


def test_create_rule_requires_auth(client: TestClient) -> None:
    resp = client.post(f"/v1/tenants/{_TENANT}/audit/rules", json=_valid_body())
    assert resp.status_code == 401


def test_create_rule_rejects_mismatched_tenant(client: TestClient) -> None:
    resp = client.post(
        "/v1/tenants/other-tenant/audit/rules",
        json=_valid_body(),
        headers=_AUTH_HEADER,
    )
    assert resp.status_code == 403


def test_create_rule_defaults_are_applied(client: TestClient, fake_session: _FakeSession) -> None:
    client.post(f"/v1/tenants/{_TENANT}/audit/rules", json=_valid_body(), headers=_AUTH_HEADER)
    insert_calls = [p for sql, p in fake_session.executed if "INSERT INTO audit.audit_rules" in sql]
    assert len(insert_calls) == 1
    params = insert_calls[0]
    assert params["audit_scope"] == "WITHIN_CASE"
    assert params["phases"] == '["FULL"]'
    assert params["left_aggregation"] == "SINGLE"
    assert params["right_aggregation"] == "SINGLE"
    assert params["threshold"] == 0
    assert params["requires_both_docs"] is False
    assert params["enabled"] is True
