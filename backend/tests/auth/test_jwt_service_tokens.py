"""test_jwt_service_tokens.py — ServiceIntegration token acceptance.

Covers the pure paths of auth/jwt.py:
  - _parse_mock_token flags a SERVICE / SERVICE_INTEGRATION mock as a service principal
  - require_tenant bypasses the tenant match for service principals only
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from verigence.audit.auth.jwt import Principal, _parse_mock_token, require_tenant


def test_mock_human_token_is_not_a_service_principal() -> None:
    principal = _parse_mock_token("mock.tenant-001.user-007.TENANT_ADMIN")
    assert principal is not None
    assert principal.tenant_id == "tenant-001"
    assert principal.is_service is False


@pytest.mark.parametrize("role", ["SERVICE", "SERVICE_INTEGRATION"])
def test_mock_service_token_is_flagged(role: str) -> None:
    principal = _parse_mock_token(f"mock.tenant-001.audit-core.{role}")
    assert principal is not None
    assert principal.is_service is True


def test_require_tenant_enforces_match_for_human_principal() -> None:
    human = Principal(tenant_id="tenant-001", actor_id="u", roles=["PC"])
    require_tenant("tenant-001", human)  # no raise
    with pytest.raises(HTTPException) as exc:
        require_tenant("tenant-002", human)
    assert exc.value.status_code == 403


def test_require_tenant_bypassed_for_service_principal() -> None:
    service = Principal(
        tenant_id="", actor_id="audit-core", roles=["SERVICE_INTEGRATION"], is_service=True
    )
    require_tenant("tenant-001", service)  # no raise
    require_tenant("any-other-tenant", service)  # no raise
