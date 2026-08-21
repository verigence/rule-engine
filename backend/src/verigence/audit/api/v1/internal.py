"""internal.py — Internal webhook from verigence-di.

POST /internal/trigger
  • Validates X-Webhook-Secret header (no JWT — server-to-server)
  • Returns 202 Accepted immediately
  • Fires incremental audit as asyncio background task
"""
from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from verigence.audit.application.evaluator import load_rules, run_audit
from verigence.audit.repositories.audit_findings import persist_findings
from verigence.audit.repositories.audit_runs import complete_run, create_run
from verigence.audit.repositories.database import audit_session_ctx, di_session_ctx
from verigence.audit.settings import get_settings

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["internal"])


class WebhookPayload(BaseModel):
    tenant_id:    str
    subject_id:   str
    doc_type_key: str


async def _run_incremental(
    tenant_id: str,
    subject_id: str,
    doc_type_key: str,
) -> None:
    """
    Evaluate rules that reference the newly confirmed doc_type_key.
    Runs entirely in the background — never blocks the webhook response.
    """
    try:
        async with di_session_ctx() as di_session, audit_session_ctx() as audit_session:
            all_rules = await load_rules(audit_session, scope="WITHIN_CASE")
            relevant = [
                r for r in all_rules
                if r.left_doc_type == doc_type_key
                or r.right_doc_type == doc_type_key
            ]
            if not relevant:
                logger.info(
                    "webhook_no_rules_for_doc_type",
                    doc_type_key=doc_type_key,
                    tenant_id=tenant_id,
                )
                return

            run_id = await create_run(
                audit_session,
                tenant_id=tenant_id,
                subject_id=UUID(subject_id),
                scope="WITHIN_CASE",
                trigger_mode="EVENT_DRIVEN",
                newly_confirmed_doc=doc_type_key,
            )
            summary = await run_audit(
                di_session, audit_session,
                tenant_id=tenant_id,
                subject_id=UUID(subject_id),
            )
            summary.audit_run_id = run_id
            await persist_findings(
                audit_session,
                tenant_id=tenant_id,
                subject_id=UUID(subject_id),
                run_id=run_id,
                findings=summary.findings,
                scope="WITHIN_CASE",
            )
            await complete_run(audit_session, run_id, summary)
            logger.info(
                "webhook_audit_complete",
                tenant_id=tenant_id,
                subject_id=subject_id,
                doc_type_key=doc_type_key,
                verdict=summary.verdict,
            )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "webhook_audit_failed",
            tenant_id=tenant_id,
            subject_id=subject_id,
            doc_type_key=doc_type_key,
            exc=str(exc),
        )


@router.post("/internal/trigger", status_code=202)
async def trigger_webhook(
    body: WebhookPayload,
    request: Request,
) -> dict[str, Any]:
    """Receive event from verigence-di and schedule incremental evaluation."""
    settings = get_settings()
    secret   = settings.webhook_secret
    if secret and request.headers.get("X-Webhook-Secret") != secret:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    asyncio.create_task(
        _run_incremental(body.tenant_id, body.subject_id, body.doc_type_key)
    )
    return {"accepted": True, "subject_id": body.subject_id}
