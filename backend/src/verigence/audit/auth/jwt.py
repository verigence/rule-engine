"""jwt.py — JWT Bearer token verification.

Two token shapes are accepted, both issued by verigence-security and
verified against the same JWKS endpoint (AUDIT_SECURITY_JWKS_URL):

  * Human / platform tokens — iss=verigence-security, aud=verigence-platform,
    carry tenantId + roles. require_tenant() enforces the tenant match.

  * ServiceIntegration tokens — iss=verigence-security, aud=audit,
    actor_type=SERVICE_INTEGRATION, no tenant claim. Minted by a trusted
    module (Audit Core) to call the phase-audit API. These are trusted for
    any tenant; the tenantId path parameter is authoritative — the same
    trust model verigence-di applies to its service callers.

Dev/CI mock format: mock.<tenantId>.<actorId>.<ROLE>[.<ROLE>...]
Example: mock.tenant-001.user-007.TENANT_ADMIN
A mock token whose role list contains SERVICE or SERVICE_INTEGRATION is
treated as a service principal. The mock format is accepted only when the
env is not PRODUCTION.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any

import structlog
from fastapi import Header, HTTPException
from jose import ExpiredSignatureError, JWTError, jwt

from verigence.audit.settings import get_settings

logger = structlog.get_logger(__name__)

_ISSUER   = "verigence-security"
_AUDIENCE = "verigence-platform"

# ServiceIntegration tokens minted for the rule-engine carry this audience and
# actor_type. They have no tenant claim — the request path tenant is trusted.
_SERVICE_AUDIENCE    = "audit"
_SERVICE_ACTOR_TYPE  = "SERVICE_INTEGRATION"
_SERVICE_MOCK_ROLES  = {"SERVICE", "SERVICE_INTEGRATION"}


@dataclass
class Principal:
    tenant_id:   str
    actor_id:    str
    roles:       list[str]
    permissions: list[str] = field(default_factory=list)
    is_service:  bool = False


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
    return Principal(
        tenant_id=tenant_id,
        actor_id=actor_id,
        roles=roles,
        is_service=bool(_SERVICE_MOCK_ROLES.intersection(roles)),
    )


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
        import httpx  # noqa: PLC0415 (lazy import)

        # Fetch JWKS on demand (cached by python-jose internally after first call)
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

        # A ServiceIntegration token carries aud=audit; every other token is a
        # human/platform token (aud=verigence-platform). Pick the audience to
        # verify against from the unverified claim, then decode with signature,
        # issuer and audience all enforced.
        unverified_aud = jwt.get_unverified_claims(token).get("aud")
        is_service_token = unverified_aud == _SERVICE_AUDIENCE
        claims: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=_SERVICE_AUDIENCE if is_service_token else _AUDIENCE,
            issuer=_ISSUER,
        )
        if is_service_token:
            if claims.get("actor_type") != _SERVICE_ACTOR_TYPE:
                raise HTTPException(
                    status_code=401,
                    detail="Service token actor_type is not SERVICE_INTEGRATION",
                )
            return Principal(
                tenant_id="",  # no tenant claim — the request path is authoritative
                actor_id=claims.get("sub", ""),
                roles=[_SERVICE_ACTOR_TYPE],
                permissions=[],
                is_service=True,
            )
        return Principal(
            tenant_id=claims.get("tenantId", ""),
            actor_id=claims.get("sub", ""),
            roles=claims.get("roles", []),
            permissions=claims.get("permissions", []),
        )
    except HTTPException:
        raise
    except ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail="Token expired") from exc
    except JWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("jwt_verification_error", exc=str(exc))
        raise HTTPException(status_code=401, detail="Token verification failed") from exc


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

    Service principals (ServiceIntegration tokens) carry no tenant claim and are
    trusted for every tenant — the path parameter is authoritative, matching the
    trust model verigence-di applies to its own service callers.
    """
    if principal.is_service:
        return
    if principal.tenant_id != tenant_id_path:
        raise HTTPException(
            status_code=403,
            detail="Token tenant does not match requested tenantId",
        )
