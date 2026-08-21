"""phase_router.py — Map API phase name → run_audit() with phase filter.

Design doc §18 defines which rules belong to each phase.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from verigence.audit.application.evaluator import run_audit
from verigence.audit.domain.types import AuditRunSummary

# Phase name → list of phase tags stored in audit_rules.phases JSONB
PHASE_MAP: dict[str, list[str]] = {
    "BOOKING":   ["BOOKING"],
    "DELIVERY":  ["DELIVERY"],
    "FINANCE":   ["FINANCE"],
    "EXCHANGE":  ["EXCHANGE"],
    "CORPORATE": ["CORPORATE"],
    "FULL":      ["FULL"],  # evaluator also includes FULL-tagged rules for every phase
}


async def run_phase_audit(
    di_session: AsyncSession,
    audit_session: AsyncSession,
    tenant_id: str,
    subject_id: UUID | str,
    phase: str,
    config_overrides: dict[str, Any] | None = None,
) -> AuditRunSummary:
    """
    Evaluate only the rules that belong to the given phase.
    Phase name must be one of the keys in PHASE_MAP.
    """
    phase_upper = phase.upper()
    phases = PHASE_MAP.get(phase_upper, [phase_upper])
    return await run_audit(
        di_session, audit_session,
        tenant_id=tenant_id,
        subject_id=subject_id,
        phases=phases,
        config_overrides=config_overrides,
    )
