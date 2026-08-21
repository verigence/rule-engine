"""batch.py — Nightly APScheduler job.

Runs once per night (AUDIT_BATCH_HOUR, default 02:00 UTC):
  1. For each active tenant: find subjects updated since last audit
  2. Run full within-case audit per subject
  3. Run cross-case duplicate scan once per tenant
"""
from __future__ import annotations

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text

from verigence.audit.application.cross_case_engine import run_cross_case_scan
from verigence.audit.application.evaluator import run_audit
from verigence.audit.repositories.audit_findings import persist_findings
from verigence.audit.repositories.audit_runs import complete_run, create_run
from verigence.audit.repositories.database import audit_session_ctx, di_session_ctx
from verigence.audit.settings import get_settings

logger = structlog.get_logger(__name__)


async def _nightly_batch() -> None:
    """Core batch logic. Runs inside the scheduler."""
    logger.info("nightly_batch_start")
    settings = get_settings()

    async with di_session_ctx() as di_session:
        # Find all tenants that have data in document_search_index
        tenant_rows = (
            await di_session.execute(
                text("""
                    SELECT DISTINCT tenant_id
                    FROM docintel.document_search_index
                """)
            )
        ).mappings().all()
        tenant_ids = [r["tenant_id"] for r in tenant_rows]

    for tenant_id in tenant_ids:
        try:
            await _process_tenant(tenant_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("batch_tenant_failed", tenant_id=tenant_id, exc=str(exc))

    logger.info("nightly_batch_complete", tenants_processed=len(tenant_ids))


async def _process_tenant(tenant_id: str) -> None:
    async with di_session_ctx() as di_session, audit_session_ctx() as audit_session:
        # Find subjects updated since their last audit run
        subject_rows = (
            await di_session.execute(
                text("""
                    SELECT DISTINCT dsi.subject_id
                    FROM   docintel.document_search_index dsi
                    WHERE  dsi.tenant_id = :tid
                      AND  dsi.subject_id IS NOT NULL
                      AND  dsi.updated_at_utc > (
                               SELECT COALESCE(MAX(ar.completed_at_utc), '1970-01-01')
                               FROM   audit.audit_runs ar
                               WHERE  ar.tenant_id  = :tid
                                 AND  ar.audit_scope = 'WITHIN_CASE'
                           )
                """),
                {"tid": tenant_id},
            )
        ).mappings().all()

        subject_ids = [r["subject_id"] for r in subject_rows]
        logger.info(
            "batch_tenant_subjects",
            tenant_id=tenant_id,
            subjects_to_audit=len(subject_ids),
        )

        for subject_id in subject_ids:
            try:
                run_id = await create_run(
                    audit_session,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    scope="WITHIN_CASE",
                    trigger_mode="SCHEDULED",
                    triggered_by="nightly_batch",
                )
                summary = await run_audit(
                    di_session, audit_session,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                )
                summary.audit_run_id = run_id
                await persist_findings(
                    audit_session, tenant_id, subject_id, run_id, summary.findings
                )
                await complete_run(audit_session, run_id, summary)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "batch_subject_failed",
                    tenant_id=tenant_id,
                    subject_id=str(subject_id),
                    exc=str(exc),
                )

        # Cross-case scan — once per tenant per night
        try:
            await run_cross_case_scan(di_session, audit_session, tenant_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("batch_cross_case_failed", tenant_id=tenant_id, exc=str(exc))


def get_batch_scheduler() -> AsyncIOScheduler:
    """Create and configure the APScheduler instance (not started yet)."""
    settings = get_settings()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _nightly_batch,
        trigger=CronTrigger(hour=settings.batch_hour, minute=0, timezone="UTC"),
        id="nightly_audit_batch",
        name="Nightly Audit Batch",
        replace_existing=True,
        misfire_grace_time=3600,  # allow up to 1h late start
    )
    return scheduler
