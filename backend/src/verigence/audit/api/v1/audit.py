"""audit.py — All 20 public audit endpoints.

Group A  — Phase-scoped evaluation (7 routes): sync, returns anomalies[] immediately
Group B  — Full audit + run history (2 routes)
Group C  — Findings query, summary, readiness (4 routes)
Group D  — Cross-case scan + findings (2 routes)
Group E  — Acknowledgement (3 routes)
Group F  — Rule management (4 routes, including re-evaluate)
"""
from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from verigence.audit.api.schemas import ok
from verigence.audit.application.cross_case_engine import run_cross_case_scan
from verigence.audit.application.evaluator import run_audit
from verigence.audit.application.phase_router import run_phase_audit
from verigence.audit.repositories.audit_findings import (
    acknowledge_finding,
    bulk_acknowledge,
    get_audit_summary,
    get_findings,
    get_pending_acknowledgements,
    persist_findings,
)
from verigence.audit.repositories.audit_runs import (
    complete_run,
    create_run,
    list_runs,
)
from verigence.audit.repositories.database import (
    get_audit_session,
    get_di_session,
)

router = APIRouter(
    prefix="/v1/tenants/{tenantId}",
    tags=["audit"],
)


# ── Request / Response bodies ────────────────────────────────────────────────────────

class PhaseAuditRequest(BaseModel):
    includeSkipped: bool = False
    failFast:       bool = False

class AcknowledgeRequest(BaseModel):
    note:  str
    waive: bool = False

class BulkAcknowledgeRequest(BaseModel):
    findingIds: list[str]
    note:       str
    waive:      bool = False

class RuleConfigUpdate(BaseModel):
    threshold: float | None = None
    enabled:   bool  | None = None


# ── Helper: run full audit and persist ──────────────────────────────────────────────

async def _run_and_persist(
    di_session: AsyncSession,
    audit_session: AsyncSession,
    tenant_id: str,
    subject_id: str,
    trigger_mode: str = "ON_DEMAND",
    phases: list[str] | None = None,
) -> dict[str, Any]:
    run_id = await create_run(
        audit_session,
        tenant_id=tenant_id,
        subject_id=UUID(subject_id),
        scope="WITHIN_CASE",
        trigger_mode=trigger_mode,
    )
    summary = await run_audit(
        di_session, audit_session,
        tenant_id=tenant_id, subject_id=UUID(subject_id),
        phases=phases,
    )
    summary.audit_run_id = run_id
    await persist_findings(
        audit_session, tenant_id, UUID(subject_id), run_id, summary.findings
    )
    await complete_run(audit_session, run_id, summary)

    fail_findings = [f for f in summary.findings if f.result.value == "FAIL"]
    return {
        "auditRunId": str(run_id),
        "verdict":    summary.verdict,
        "summary": {
            "rulesEvaluated": summary.total_rules,
            "pass":           summary.pass_count,
            "fail":           summary.fail_count,
            "skipped":        summary.skipped_count,
            "critical":       summary.critical_fail,
            "warning":        summary.warning_fail,
            "info":           summary.info_fail,
        },
        "anomalies": [
            {
                "ruleCode":  f.rule_code,
                "severity":  f.severity,
                "category":  f.category,
                "detail":    f.detail,
                "leftValue": f.left_value,
                "rightValue":f.right_value,
            }
            for f in fail_findings
        ],
        "skippedRules": [
            {"ruleCode": rc, "reason": reason}
            for rc, reason in summary.skipped_reasons.items()
        ],
    }


# ── Group A: Phase-scoped evaluation (7 routes) ───────────────────────────────────

@router.post("/subjects/{subjectId}/audit/booking")
async def audit_booking(
    tenantId: str, subjectId: str,
    body: PhaseAuditRequest,
    di: AsyncSession = Depends(get_di_session),
    audit: AsyncSession = Depends(get_audit_session),
) -> dict:
    data = await _run_and_persist(di, audit, tenantId, subjectId, phases=["BOOKING"])
    return ok({**data, "phase": "BOOKING"})


@router.post("/subjects/{subjectId}/audit/delivery")
async def audit_delivery(
    tenantId: str, subjectId: str,
    body: PhaseAuditRequest,
    di: AsyncSession = Depends(get_di_session),
    audit: AsyncSession = Depends(get_audit_session),
) -> dict:
    data = await _run_and_persist(di, audit, tenantId, subjectId, phases=["DELIVERY"])
    return ok({**data, "phase": "DELIVERY"})


@router.post("/subjects/{subjectId}/audit/finance")
async def audit_finance(
    tenantId: str, subjectId: str,
    body: PhaseAuditRequest,
    di: AsyncSession = Depends(get_di_session),
    audit: AsyncSession = Depends(get_audit_session),
) -> dict:
    data = await _run_and_persist(di, audit, tenantId, subjectId, phases=["FINANCE"])
    return ok({**data, "phase": "FINANCE"})


@router.post("/subjects/{subjectId}/audit/exchange")
async def audit_exchange(
    tenantId: str, subjectId: str,
    body: PhaseAuditRequest,
    di: AsyncSession = Depends(get_di_session),
    audit: AsyncSession = Depends(get_audit_session),
) -> dict:
    data = await _run_and_persist(di, audit, tenantId, subjectId, phases=["EXCHANGE"])
    return ok({**data, "phase": "EXCHANGE"})


@router.post("/subjects/{subjectId}/audit/corporate")
async def audit_corporate(
    tenantId: str, subjectId: str,
    body: PhaseAuditRequest,
    di: AsyncSession = Depends(get_di_session),
    audit: AsyncSession = Depends(get_audit_session),
) -> dict:
    data = await _run_and_persist(di, audit, tenantId, subjectId, phases=["CORPORATE"])
    return ok({**data, "phase": "CORPORATE"})


class ByCategoryRequest(BaseModel):
    categories: list[str]
    includeSkipped: bool = False


@router.post("/subjects/{subjectId}/audit/by-category")
async def audit_by_category(
    tenantId: str, subjectId: str,
    body: ByCategoryRequest,
    di: AsyncSession = Depends(get_di_session),
    audit: AsyncSession = Depends(get_audit_session),
) -> dict:
    data = await _run_and_persist(di, audit, tenantId, subjectId)
    # Post-filter to requested categories
    data["anomalies"] = [a for a in data["anomalies"] if a["category"] in body.categories]
    return ok({**data, "categories": body.categories})


class ByDocumentsRequest(BaseModel):
    documentIds: list[str]
    includeSkipped: bool = False


@router.post("/subjects/{subjectId}/audit/by-documents")
async def audit_by_documents(
    tenantId: str, subjectId: str,
    body: ByDocumentsRequest,
    di: AsyncSession = Depends(get_di_session),
    audit: AsyncSession = Depends(get_audit_session),
) -> dict:
    # Full audit — the context builder will pick up those confirmed documents
    data = await _run_and_persist(di, audit, tenantId, subjectId)
    return ok(data)


# ── Group B: Full audit + run history (2 routes) ────────────────────────────────

@router.post("/subjects/{subjectId}/audit")
async def full_audit(
    tenantId: str, subjectId: str,
    di: AsyncSession = Depends(get_di_session),
    audit: AsyncSession = Depends(get_audit_session),
) -> dict:
    data = await _run_and_persist(di, audit, tenantId, subjectId)
    return ok(data)


@router.get("/subjects/{subjectId}/audit/runs")
async def get_audit_runs(
    tenantId: str, subjectId: str,
    audit: AsyncSession = Depends(get_audit_session),
) -> dict:
    runs = await list_runs(audit, tenantId, subjectId)
    return ok({"runs": runs})


# ── Group C: Findings query + summary + readiness (4 routes) ─────────────────

@router.get("/subjects/{subjectId}/audit/findings")
async def subject_findings(
    tenantId: str, subjectId: str,
    result: str | None = None,
    severity: str | None = None,
    audit: AsyncSession = Depends(get_audit_session),
) -> dict:
    findings = await get_findings(audit, tenantId, subjectId, result=result, severity=severity)
    return ok({"findings": findings})


@router.get("/subjects/{subjectId}/audit/summary")
async def subject_summary(
    tenantId: str, subjectId: str,
    audit: AsyncSession = Depends(get_audit_session),
) -> dict:
    summary = await get_audit_summary(audit, tenantId, subjectId)
    return ok(summary)


@router.get("/audit/findings")
async def tenant_findings(
    tenantId: str,
    result: str | None = None,
    severity: str | None = None,
    category: str | None = None,
    audit: AsyncSession = Depends(get_audit_session),
) -> dict:
    # Tenant-wide findings: query all subjects
    rows = (
        await audit.execute(
            text("""
                SELECT af.finding_id, af.subject_id, af.rule_code, af.result,
                       af.severity, ar.category, af.detail,
                       af.acknowledgement_state, af.evaluated_at_utc
                FROM   audit.audit_findings af
                JOIN   audit.audit_rules ar ON ar.rule_code = af.rule_code
                WHERE  af.tenant_id = :tid
                  AND  af.is_current = TRUE
                ORDER  BY af.evaluated_at_utc DESC
                LIMIT  500
            """),
            {"tid": tenantId},
        )
    ).mappings().all()
    return ok({"findings": [dict(r) for r in rows]})


@router.get("/subjects/{subjectId}/audit/readiness")
async def rule_readiness(
    tenantId: str, subjectId: str,
    di: AsyncSession = Depends(get_di_session),
    audit: AsyncSession = Depends(get_audit_session),
) -> dict:
    """Show which rules can be evaluated given current confirmed documents."""
    from verigence.audit.application.context_builder import build_audit_context  # noqa: PLC0415
    from verigence.audit.application.evaluator import load_rules, evaluate_rule  # noqa: PLC0415

    context = await build_audit_context(di, tenantId, UUID(subjectId))
    rules = await load_rules(audit, scope="WITHIN_CASE")
    ready, skipped = [], []
    for rule in rules:
        finding = evaluate_rule(rule, context)
        if finding.result.value == "SKIPPED":
            skipped.append({"ruleCode": rule.rule_code, "reason": finding.detail})
        else:
            ready.append(rule.rule_code)
    return ok({"ready": ready, "notReady": skipped})


# ── Group D: Cross-case (2 routes) ───────────────────────────────────────────────

@router.post("/audit/cross-case-scan")
async def cross_case_scan(
    tenantId: str,
    di: AsyncSession = Depends(get_di_session),
    audit: AsyncSession = Depends(get_audit_session),
) -> dict:
    summary = await run_cross_case_scan(di, audit, tenantId)
    return ok({
        "auditRunId": str(summary.audit_run_id),
        "verdict":    summary.verdict,
        "duplicatesFound": summary.fail_count,
    })


@router.get("/audit/cross-case-findings")
async def cross_case_findings(
    tenantId: str,
    audit: AsyncSession = Depends(get_audit_session),
) -> dict:
    rows = (
        await audit.execute(
            text("""
                SELECT finding_id, rule_code, detail, affected_subjects,
                       evaluated_at_utc
                FROM   audit.audit_findings
                WHERE  tenant_id  = :tid
                  AND  audit_scope = 'CROSS_CASE'
                  AND  is_current  = TRUE
                ORDER  BY evaluated_at_utc DESC
                LIMIT  200
            """),
            {"tid": tenantId},
        )
    ).mappings().all()
    return ok({"findings": [dict(r) for r in rows]})


# ── Group E: Acknowledgement (3 routes) ──────────────────────────────────────────

@router.post("/audit/findings/{findingId}/acknowledge")
async def ack_finding(
    tenantId: str, findingId: str,
    body: AcknowledgeRequest,
    audit: AsyncSession = Depends(get_audit_session),
) -> dict:
    await acknowledge_finding(
        audit, tenantId, findingId,
        actor_id="api-caller",  # JWT actor_id wired in Sub-Task 10
        note=body.note, waive=body.waive,
    )
    return ok({"acknowledged": True})


@router.post("/subjects/{subjectId}/audit/findings/bulk-acknowledge")
async def bulk_ack(
    tenantId: str, subjectId: str,
    body: BulkAcknowledgeRequest,
    audit: AsyncSession = Depends(get_audit_session),
) -> dict:
    await bulk_acknowledge(
        audit, tenantId, body.findingIds,
        actor_id="api-caller",
        note=body.note, waive=body.waive,
    )
    return ok({"acknowledged": len(body.findingIds)})


@router.get("/audit/pending-acknowledgements")
async def pending_acks(
    tenantId: str,
    severity: str | None = None,
    audit: AsyncSession = Depends(get_audit_session),
) -> dict:
    findings = await get_pending_acknowledgements(audit, tenantId, severity=severity)
    return ok({"pending": findings})


# ── Group F: Rule management (4 routes) ───────────────────────────────────────────

@router.get("/audit/rules")
async def list_audit_rules(
    tenantId: str,
    audit: AsyncSession = Depends(get_audit_session),
) -> dict:
    rows = (
        await audit.execute(
            text("""
                SELECT rule_code, category, audit_scope, phases, comparator,
                       threshold, severity, finding_message, enabled
                FROM   audit.audit_rules
                ORDER  BY category, rule_code
            """)
        )
    ).mappings().all()
    return ok({"rules": [dict(r) for r in rows]})


@router.get("/audit/rule-readiness")
async def tenant_rule_readiness(
    tenantId: str,
    audit: AsyncSession = Depends(get_audit_session),
) -> dict:
    """Count of subjects per rule that could be evaluated given current confirmed docs."""
    # Lightweight: return enabled rule metadata only
    rows = (
        await audit.execute(
            text("""
                SELECT rule_code, category, left_doc_type, right_doc_type, enabled
                FROM   audit.audit_rules
                WHERE  enabled = TRUE
                ORDER  BY category, rule_code
            """)
        )
    ).mappings().all()
    return ok({"rules": [dict(r) for r in rows]})


@router.put("/audit/rules/{ruleCode}/config")
async def update_rule_config(
    tenantId: str, ruleCode: str,
    body: RuleConfigUpdate,
    audit: AsyncSession = Depends(get_audit_session),
) -> dict:
    if body.threshold is not None:
        await audit.execute(
            text("UPDATE audit.audit_rules SET threshold = :t WHERE rule_code = :rc"),
            {"t": body.threshold, "rc": ruleCode},
        )
    if body.enabled is not None:
        await audit.execute(
            text("UPDATE audit.audit_rules SET enabled = :e WHERE rule_code = :rc"),
            {"e": body.enabled, "rc": ruleCode},
        )
    return ok({"updated": ruleCode})


@router.post("/subjects/{subjectId}/audit/re-evaluate/{ruleCode}")
async def re_evaluate_rule(
    tenantId: str, subjectId: str, ruleCode: str,
    di: AsyncSession = Depends(get_di_session),
    audit: AsyncSession = Depends(get_audit_session),
) -> dict:
    from verigence.audit.application.context_builder import build_audit_context  # noqa: PLC0415
    from verigence.audit.application.evaluator import load_rules, evaluate_rule  # noqa: PLC0415

    context = await build_audit_context(di, tenantId, UUID(subjectId))
    rules = await load_rules(audit, scope="WITHIN_CASE")
    rule = next((r for r in rules if r.rule_code == ruleCode), None)
    if not rule:
        raise HTTPException(status_code=404, detail=f"Rule {ruleCode!r} not found")

    finding = evaluate_rule(rule, context)
    return ok({
        "ruleCode": ruleCode,
        "result":   finding.result.value,
        "detail":   finding.detail,
        "leftValue":  finding.left_value,
        "rightValue": finding.right_value,
    })
