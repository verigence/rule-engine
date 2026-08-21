"""audit_runs.py — Create, complete, and query audit_runs rows.

All SQL via text() — no ORM.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from verigence.audit.domain.types import AuditRunSummary


async def create_run(
    session: AsyncSession,
    tenant_id: str,
    subject_id: UUID | str | None,
    scope: str,
    trigger_mode: str,
    triggered_by: str | None = None,
    newly_confirmed_doc: str | None = None,
    phases: list[str] | None = None,
) -> UUID:
    """Insert a new audit_run row in PENDING state and return its UUID."""
    row = (
        await session.execute(
            text("""
                INSERT INTO audit.audit_runs
                    (tenant_id, subject_id, audit_scope, trigger_mode,
                     triggered_by, newly_confirmed_doc)
                VALUES (:tid, :sid, :scope, :mode, :actor, :doc)
                RETURNING audit_run_id
            """),
            {
                "tid":   str(tenant_id),
                "sid":   str(subject_id) if subject_id else None,
                "scope": scope,
                "mode":  trigger_mode,
                "actor": triggered_by,
                "doc":   newly_confirmed_doc,
            },
        )
    ).first()
    return UUID(str(row[0]))  # type: ignore[index]


async def complete_run(
    session: AsyncSession,
    run_id: UUID,
    summary: AuditRunSummary,
) -> None:
    """Write final counts, verdict, and completed_at_utc to the run row."""
    import json  # noqa: PLC0415
    await session.execute(
        text("""
            UPDATE audit.audit_runs
            SET total_rules      = :total,
                pass_count       = :passed,
                fail_count       = :failed,
                skipped_count    = :skipped,
                critical_fail    = :critical,
                warning_fail     = :warning,
                info_fail        = :info,
                verdict          = :verdict,
                skipped_detail   = :detail::jsonb,
                completed_at_utc = :now
            WHERE audit_run_id = :run_id
        """),
        {
            "total":    summary.total_rules,
            "passed":   summary.pass_count,
            "failed":   summary.fail_count,
            "skipped":  summary.skipped_count,
            "critical": summary.critical_fail,
            "warning":  summary.warning_fail,
            "info":     summary.info_fail,
            "verdict":  summary.verdict,
            "detail":   json.dumps(summary.skipped_reasons),
            "now":      datetime.now(timezone.utc),
            "run_id":   str(run_id),
        },
    )


async def list_runs(
    session: AsyncSession,
    tenant_id: str,
    subject_id: UUID | str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return recent audit runs for a subject, newest first."""
    rows = (
        await session.execute(
            text("""
                SELECT audit_run_id, audit_scope, trigger_mode, triggered_by,
                       total_rules, pass_count, fail_count, skipped_count,
                       critical_fail, warning_fail, info_fail,
                       verdict, started_at_utc, completed_at_utc
                FROM   audit.audit_runs
                WHERE  tenant_id  = :tid
                  AND  subject_id = :sid
                ORDER  BY started_at_utc DESC
                LIMIT  :lim
            """),
            {"tid": str(tenant_id), "sid": str(subject_id), "lim": limit},
        )
    ).mappings().all()
    return [dict(r) for r in rows]
