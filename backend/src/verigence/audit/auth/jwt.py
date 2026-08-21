"""jwt.py — JWT Bearer token verification.

Same issuer/audience as verigence-di:
  iss = verigence-security
  aud = verigence-platform

Dev/CI mock format: mock.<tenantId>.<actorId>.<ROLE>[.<ROLE>...]
Example: mock.tenant-001.user-007.TENANT_ADMIN

In production, tokens are verified against the JWKS endpoint
(AUDIT_SECURITY_JWKS_URL). In local/dev, the mock format is
accepted when the env is not PRODUCTION.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any

import structlog
from fastapi import Depends, Header, HTTPException
from jose import ExpiredSignatureError, JWTError, jwt

from verigence.audit.settings import get_settings

logger = structlog.get_logger(__name__)

_ISSUER   = "verigence-security"
_AUDIENCE = "verigence-platform"


@dataclass
class Principal:
    tenant_id:   str
    actor_id:    str
    roles:       list[str]
    permissions: list[str] = field(default_factory=list)


def _parse_mock_token(token: str) -> Principal | None:
    """
    Accept mock.<tenantId>.<actorId>.<ROLE>[.<ROLE>...] in non-production.
    Returns None if the token does not match the mock format.
    """
    if not token.startswith("mock."):
        return None
    parts = token.split(".")
    if len(parts) < 4:  # mock + tenant + actor + at least one role
        return None
    _, tenant_id, actor_id, *roles = parts
    return Principal(tenant_id=tenant_id, actor_id=actor_id, roles=roles)


def _verify_jwt(token: str) -> Principal:
    settings = get_settings()

    # Mock tokens — allowed in non-production only
    if not settings.is_production:
        mock = _parse_mock_token(token)
        if mock:
            return mock

    # Real JWT verification
    if not settings.security_jwks_url:
        raise HTTPException(status_code=401, detail="JWKS URL not configured")

    try:
        # Fetch JWKS on demand (cached by python-jose internally after first call)
        from jose.backends import RSAKey  # noqa: PLC0415 (lazy import)
        import httpx  # noqa: PLC0415

        # Fetch JWKS
        resp = httpx.get(settings.security_jwks_url, timeout=5.0)
        resp.raise_for_status()
        jwks = resp.json()

        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        key = next(
            (k for k in jwks.get("keys", []) if k.get("kid") == kid),
            None,
        )
        if not key:
            raise HTTPException(status_code=401, detail="Unknown signing key")

        claims: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=_AUDIENCE,
            issuer=_ISSUER,
        )
        return Principal(
            tenant_id=claims.get("tenantId", ""),
            actor_id=claims.get("sub", ""),
            roles=claims.get("roles", []),
            permissions=claims.get("permissions", []),
        )
    except ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except JWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("jwt_verification_error", exc=str(exc))
        raise HTTPException(status_code=401, detail="Token verification failed")


# ── FastAPI dependency ────────────────────────────────────────────────────────────────

def get_principal(
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """
    FastAPI dependency: extract and verify Bearer token from Authorization header.
    Raises HTTP 401 on failure.
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header missing")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Bearer token required")
    return _verify_jwt(token)


def require_tenant(tenant_id_path: str, principal: Principal) -> None:
    """
    Verify that the JWT tenant matches the tenantId path parameter.
    Raises HTTP 403 on mismatch.
    """
    if principal.tenant_id != tenant_id_path:
        raise HTTPException(
            status_code=403,
            detail="Token tenant does not match requested tenantId",
        )
