"""audit.py — All 21 public audit endpoints.

Group A  — Phase-scoped evaluation (7 routes): sync, returns anomalies[] immediately
Group B  — Full audit + run history (2 routes)
Group C  — Findings query, summary, readiness (4 routes)
Group D  — Cross-case scan + findings (2 routes)
Group E  — Acknowledgement (3 routes)
Group F  — Rule management (5 routes: create, list, readiness, config update, re-evaluate)

All routes require a valid Bearer JWT (see auth/jwt.py).
Tenant in JWT must match tenantId path parameter.
"""
from __future__ import annotations

import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationInfo, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from verigence.audit.api.schemas import ok
from verigence.audit.application.condition_parser import validate_condition
from verigence.audit.application.cross_case_engine import run_cross_case_scan
from verigence.audit.application.evaluator import run_audit
from verigence.audit.auth.jwt import Principal, get_principal, require_tenant
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

router = APIRouter(prefix="/v1/tenants/{tenantId}", tags=["audit"])

# Reusable type alias for the auth dependency
Auth = Annotated[Principal, Depends(get_principal)]


# ── Request bodies ────────────────────────────────────────────────────────────────

class PhaseAuditRequest(BaseModel):
    includeSkipped: bool = False
    failFast:       bool = False

class ByCategoryRequest(BaseModel):
    categories:     list[str]
    includeSkipped: bool = False

class ByDocumentsRequest(BaseModel):
    documentIds:    list[str]
    includeSkipped: bool = False

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


# audit.audit_rules' own CHECK constraints (migration 0001_audit_engine.py) --
# validated here too so a bad value 422s at creation time instead of either
# a raw DB constraint-violation 500, or (for phases, which has no DB CHECK at
# all) silently persisting a rule that can never be evaluated because
# evaluator.py/phase_router.py don't recognize the phase name.
_VALID_AUDIT_SCOPES  = {"WITHIN_CASE", "CROSS_CASE"}
_VALID_COMPARATORS   = {
    "ABS_DIFF_GT", "NOT_EQ", "GT", "LT", "EQ", "DATE_BEFORE",
    "DATE_DIFF_GT", "RATIO_LT", "FIELD_EMPTY", "CROSS_DOC_SUM_GT",
}
_VALID_SEVERITIES    = {"CRITICAL", "WARNING", "INFO"}
_VALID_AGGREGATIONS  = {"SINGLE", "SUM", "MAX", "MIN", "COUNT"}
_VALID_PHASES        = {"BOOKING", "DELIVERY", "FINANCE", "EXCHANGE", "CORPORATE", "FULL"}


class RuleCreate(BaseModel):
    ruleCode:            str
    category:            str
    auditScope:          str = "WITHIN_CASE"
    phases:              list[str] = ["FULL"]  # noqa: RUF012
    leftDocType:         str | None = None
    leftFieldKey:        str | None = None
    leftAggregation:     str = "SINGLE"
    rightDocType:        str | None = None
    rightFieldKey:       str | None = None
    rightAggregation:    str = "SINGLE"
    rightConfigKey:      str | None = None
    comparator:          str
    threshold:           float = 0
    severity:            str
    findingMessage:      str
    conditionExpression: str | None = None
    requiresBothDocs:    bool = False
    enabled:             bool = True

    @field_validator("ruleCode", "category", "findingMessage")
    @classmethod
    def _not_blank(cls, v: str, info: ValidationInfo) -> str:
        if not v or not v.strip():
            raise ValueError(f"{info.field_name} must not be blank")
        return v.strip()

    @field_validator("auditScope")
    @classmethod
    def _valid_audit_scope(cls, v: str) -> str:
        if v not in _VALID_AUDIT_SCOPES:
            raise ValueError(f"auditScope must be one of {sorted(_VALID_AUDIT_SCOPES)}")
        return v

    @field_validator("comparator")
    @classmethod
    def _valid_comparator(cls, v: str) -> str:
        if v not in _VALID_COMPARATORS:
            raise ValueError(f"comparator must be one of {sorted(_VALID_COMPARATORS)}")
        return v

    @field_validator("severity")
    @classmethod
    def _valid_severity(cls, v: str) -> str:
        if v not in _VALID_SEVERITIES:
            raise ValueError(f"severity must be one of {sorted(_VALID_SEVERITIES)}")
        return v

    @field_validator("leftAggregation", "rightAggregation")
    @classmethod
    def _valid_aggregation(cls, v: str, info: ValidationInfo) -> str:
        if v not in _VALID_AGGREGATIONS:
            raise ValueError(f"{info.field_name} must be one of {sorted(_VALID_AGGREGATIONS)}")
        return v

    @field_validator("phases")
    @classmethod
    def _valid_phases(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("phases must not be empty")
        unknown = [p for p in v if p not in _VALID_PHASES]
        if unknown:
            raise ValueError(f"unknown phase(s) {unknown} -- must be one of {sorted(_VALID_PHASES)}")
        return v

    @field_validator("conditionExpression")
    @classmethod
    def _valid_condition_expression(cls, v: str | None) -> str | None:
        errors = validate_condition(v)
        if errors:
            raise ValueError("; ".join(errors))
        return v


# ── Internal helper ────────────────────────────────────────────────────────────────

async def _run_and_persist(
    di_session: AsyncSession,
    audit_session: AsyncSession,
    tenant_id: str,
    subject_id: str,
    trigger_mode: str = "ON_DEMAND",
    phases: list[str] | None = None,
) -> dict[str, Any]:
    """Run a full (or phase-filtered) audit, persist results, return API-ready dict."""
    run_id = await create_run(
        audit_session,
        tenant_id=tenant_id,
        subject_id=UUID(subject_id),
        scope="WITHIN_CASE",
        trigger_mode=trigger_mode,
    )
    summary = await run_audit(
        di_session, audit_session,
        tenant_id=tenant_id,
        subject_id=UUID(subject_id),
        phases=phases,
    )
    summary.audit_run_id = run_id
    await persist_findings(
        audit_session, tenant_id, UUID(subject_id), run_id, summary.findings
    )
    await complete_run(audit_session, run_id, summary)

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
                "ruleCode":   f.rule_code,
                "severity":   f.severity,
                "category":   f.category,
                "detail":     f.detail,
                "leftValue":  f.left_value,
                "rightValue": f.right_value,
            }
            for f in summary.findings
            if f.result.value == "FAIL"
        ],
        "skippedRules": [
            {"ruleCode": rc, "reason": reason}
            for rc, reason in summary.skipped_reasons.items()
        ],
    }


# ── Group A: Phase-scoped evaluation (7 routes) ─────────────────────────────────

@router.post("/subjects/{subjectId}/audit/booking")
async def audit_booking(
    tenantId: str, subjectId: str,
    body: PhaseAuditRequest,
    principal: Auth,
    di:    AsyncSession = Depends(get_di_session),
    audit: AsyncSession = Depends(get_audit_session),
) -> dict[str, Any]:
    require_tenant(tenantId, principal)
    data = await _run_and_persist(di, audit, tenantId, subjectId, phases=["BOOKING"])
    return ok({**data, "phase": "BOOKING"})


@router.post("/subjects/{subjectId}/audit/delivery")
async def audit_delivery(
    tenantId: str, subjectId: str,
    body: PhaseAuditRequest,
    principal: Auth,
    di:    AsyncSession = Depends(get_di_session),
    audit: AsyncSession = Depends(get_audit_session),
) -> dict[str, Any]:
    require_tenant(tenantId, principal)
    data = await _run_and_persist(di, audit, tenantId, subjectId, phases=["DELIVERY"])
    return ok({**data, "phase": "DELIVERY"})


@router.post("/subjects/{subjectId}/audit/finance")
async def audit_finance(
    tenantId: str, subjectId: str,
    body: PhaseAuditRequest,
    principal: Auth,
    di:    AsyncSession = Depends(get_di_session),
    audit: AsyncSession = Depends(get_audit_session),
) -> dict[str, Any]:
    require_tenant(tenantId, principal)
    data = await _run_and_persist(di, audit, tenantId, subjectId, phases=["FINANCE"])
    return ok({**data, "phase": "FINANCE"})


@router.post("/subjects/{subjectId}/audit/exchange")
async def audit_exchange(
    tenantId: str, subjectId: str,
    body: PhaseAuditRequest,
    principal: Auth,
    di:    AsyncSession = Depends(get_di_session),
    audit: AsyncSession = Depends(get_audit_session),
) -> dict[str, Any]:
    require_tenant(tenantId, principal)
    data = await _run_and_persist(di, audit, tenantId, subjectId, phases=["EXCHANGE"])
    return ok({**data, "phase": "EXCHANGE"})


@router.post("/subjects/{subjectId}/audit/corporate")
async def audit_corporate(
    tenantId: str, subjectId: str,
    body: PhaseAuditRequest,
    principal: Auth,
    di:    AsyncSession = Depends(get_di_session),
    audit: AsyncSession = Depends(get_audit_session),
) -> dict[str, Any]:
    require_tenant(tenantId, principal)
    data = await _run_and_persist(di, audit, tenantId, subjectId, phases=["CORPORATE"])
    return ok({**data, "phase": "CORPORATE"})


@router.post("/subjects/{subjectId}/audit/by-category")
async def audit_by_category(
    tenantId: str, subjectId: str,
    body: ByCategoryRequest,
    principal: Auth,
    di:    AsyncSession = Depends(get_di_session),
    audit: AsyncSession = Depends(get_audit_session),
) -> dict[str, Any]:
    require_tenant(tenantId, principal)
    data = await _run_and_persist(di, audit, tenantId, subjectId)
    data["anomalies"] = [a for a in data["anomalies"] if a["category"] in body.categories]
    return ok({**data, "categories": body.categories})


@router.post("/subjects/{subjectId}/audit/by-documents")
async def audit_by_documents(
    tenantId: str, subjectId: str,
    body: ByDocumentsRequest,
    principal: Auth,
    di:    AsyncSession = Depends(get_di_session),
    audit: AsyncSession = Depends(get_audit_session),
) -> dict[str, Any]:
    require_tenant(tenantId, principal)
    data = await _run_and_persist(di, audit, tenantId, subjectId)
    return ok(data)


# ── Group B: Full audit + run history (2 routes) ────────────────────────────────

@router.post("/subjects/{subjectId}/audit")
async def full_audit(
    tenantId: str, subjectId: str,
    principal: Auth,
    di:    AsyncSession = Depends(get_di_session),
    audit: AsyncSession = Depends(get_audit_session),
) -> dict[str, Any]:
    require_tenant(tenantId, principal)
    data = await _run_and_persist(di, audit, tenantId, subjectId)
    return ok(data)


@router.get("/subjects/{subjectId}/audit/runs")
async def get_audit_runs(
    tenantId: str, subjectId: str,
    principal: Auth,
    audit: AsyncSession = Depends(get_audit_session),
) -> dict[str, Any]:
    require_tenant(tenantId, principal)
    runs = await list_runs(audit, tenantId, subjectId)
    return ok({"runs": runs})


# ── Group C: Findings query + summary + readiness (4 routes) ─────────────────

@router.get("/subjects/{subjectId}/audit/findings")
async def subject_findings(
    tenantId: str, subjectId: str,
    principal: Auth,
    result:   str | None = None,
    severity: str | None = None,
    audit: AsyncSession = Depends(get_audit_session),
) -> dict[str, Any]:
    require_tenant(tenantId, principal)
    findings = await get_findings(audit, tenantId, subjectId, result=result, severity=severity)
    return ok({"findings": findings})


@router.get("/subjects/{subjectId}/audit/summary")
async def subject_summary(
    tenantId: str, subjectId: str,
    principal: Auth,
    audit: AsyncSession = Depends(get_audit_session),
) -> dict[str, Any]:
    require_tenant(tenantId, principal)
    summary = await get_audit_summary(audit, tenantId, subjectId)
    return ok(summary)


@router.get("/audit/findings")
async def tenant_findings(
    tenantId: str,
    principal: Auth,
    result:   str | None = None,
    severity: str | None = None,
    audit: AsyncSession = Depends(get_audit_session),
) -> dict[str, Any]:
    require_tenant(tenantId, principal)
    rows = (
        await audit.execute(
            text("""
                SELECT af.finding_id, af.subject_id, af.rule_code, af.result,
                       af.severity, ar.category, af.detail,
                       af.acknowledgement_state, af.evaluated_at_utc
                FROM   audit.audit_findings af
                JOIN   audit.audit_rules ar ON ar.rule_code = af.rule_code
                WHERE  af.tenant_id  = :tid
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
    principal: Auth,
    di:    AsyncSession = Depends(get_di_session),
    audit: AsyncSession = Depends(get_audit_session),
) -> dict[str, Any]:
    from verigence.audit.application.context_builder import build_audit_context  # noqa: PLC0415
    from verigence.audit.application.evaluator import evaluate_rule, load_rules  # noqa: PLC0415
    require_tenant(tenantId, principal)
    context = await build_audit_context(di, tenantId, UUID(subjectId))
    rules   = await load_rules(audit, scope="WITHIN_CASE")
    ready, not_ready = [], []
    for rule in rules:
        finding = evaluate_rule(rule, context)
        if finding.result.value == "SKIPPED":
            not_ready.append({"ruleCode": rule.rule_code, "reason": finding.detail})
        else:
            ready.append(rule.rule_code)
    return ok({"ready": ready, "notReady": not_ready})


# ── Group D: Cross-case (2 routes) ───────────────────────────────────────────────

@router.post("/audit/cross-case-scan")
async def cross_case_scan(
    tenantId: str,
    principal: Auth,
    di:    AsyncSession = Depends(get_di_session),
    audit: AsyncSession = Depends(get_audit_session),
) -> dict[str, Any]:
    require_tenant(tenantId, principal)
    summary = await run_cross_case_scan(di, audit, tenantId)
    return ok({
        "auditRunId":     str(summary.audit_run_id),
        "verdict":        summary.verdict,
        "duplicatesFound": summary.fail_count,
    })


@router.get("/audit/cross-case-findings")
async def cross_case_findings(
    tenantId: str,
    principal: Auth,
    audit: AsyncSession = Depends(get_audit_session),
) -> dict[str, Any]:
    require_tenant(tenantId, principal)
    rows = (
        await audit.execute(
            text("""
                SELECT finding_id, rule_code, detail, affected_subjects, evaluated_at_utc
                FROM   audit.audit_findings
                WHERE  tenant_id   = :tid
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
    principal: Auth,
    audit: AsyncSession = Depends(get_audit_session),
) -> dict[str, Any]:
    require_tenant(tenantId, principal)
    await acknowledge_finding(
        audit, tenantId, findingId,
        actor_id=principal.actor_id,
        note=body.note, waive=body.waive,
    )
    return ok({"acknowledged": True})


@router.post("/subjects/{subjectId}/audit/findings/bulk-acknowledge")
async def bulk_ack(
    tenantId: str, subjectId: str,
    body: BulkAcknowledgeRequest,
    principal: Auth,
    audit: AsyncSession = Depends(get_audit_session),
) -> dict[str, Any]:
    require_tenant(tenantId, principal)
    finding_ids: list[UUID | str] = list(body.findingIds)
    await bulk_acknowledge(
        audit, tenantId, finding_ids,
        actor_id=principal.actor_id,
        note=body.note, waive=body.waive,
    )
    return ok({"acknowledged": len(body.findingIds)})


@router.get("/audit/pending-acknowledgements")
async def pending_acks(
    tenantId: str,
    principal: Auth,
    severity: str | None = None,
    audit: AsyncSession = Depends(get_audit_session),
) -> dict[str, Any]:
    require_tenant(tenantId, principal)
    findings = await get_pending_acknowledgements(audit, tenantId, severity=severity)
    return ok({"pending": findings})


# ── Group F: Rule management (5 routes) ───────────────────────────────────────────

@router.post("/audit/rules", status_code=201)
async def create_audit_rule(
    tenantId: str,
    body: RuleCreate,
    principal: Auth,
    audit: AsyncSession = Depends(get_audit_session),
) -> dict[str, Any]:
    require_tenant(tenantId, principal)

    existing = (
        await audit.execute(
            text("SELECT 1 FROM audit.audit_rules WHERE rule_code = :rc"),
            {"rc": body.ruleCode},
        )
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Rule {body.ruleCode!r} already exists")

    await audit.execute(
        text("""
            INSERT INTO audit.audit_rules (
                rule_code, category, audit_scope, phases,
                left_doc_type, left_field_key, left_aggregation,
                right_doc_type, right_field_key, right_aggregation, right_config_key,
                comparator, threshold, severity, finding_message,
                condition_expression, requires_both_docs, enabled
            ) VALUES (
                :rule_code, :category, :audit_scope, :phases::jsonb,
                :left_doc_type, :left_field_key, :left_aggregation,
                :right_doc_type, :right_field_key, :right_aggregation, :right_config_key,
                :comparator, :threshold, :severity, :finding_message,
                :condition_expression, :requires_both_docs, :enabled
            )
        """),
        {
            "rule_code": body.ruleCode,
            "category": body.category,
            "audit_scope": body.auditScope,
            "phases": json.dumps(body.phases),
            "left_doc_type": body.leftDocType,
            "left_field_key": body.leftFieldKey,
            "left_aggregation": body.leftAggregation,
            "right_doc_type": body.rightDocType,
            "right_field_key": body.rightFieldKey,
            "right_aggregation": body.rightAggregation,
            "right_config_key": body.rightConfigKey,
            "comparator": body.comparator,
            "threshold": body.threshold,
            "severity": body.severity,
            "finding_message": body.findingMessage,
            "condition_expression": body.conditionExpression,
            "requires_both_docs": body.requiresBothDocs,
            "enabled": body.enabled,
        },
    )
    return ok({"created": body.ruleCode})


@router.get("/audit/rules")
async def list_audit_rules(
    tenantId: str,
    principal: Auth,
    audit: AsyncSession = Depends(get_audit_session),
) -> dict[str, Any]:
    require_tenant(tenantId, principal)
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
    principal: Auth,
    audit: AsyncSession = Depends(get_audit_session),
) -> dict[str, Any]:
    require_tenant(tenantId, principal)
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
    principal: Auth,
    audit: AsyncSession = Depends(get_audit_session),
) -> dict[str, Any]:
    require_tenant(tenantId, principal)
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
    principal: Auth,
    di:    AsyncSession = Depends(get_di_session),
    audit: AsyncSession = Depends(get_audit_session),
) -> dict[str, Any]:
    from verigence.audit.application.context_builder import build_audit_context  # noqa: PLC0415
    from verigence.audit.application.evaluator import evaluate_rule, load_rules  # noqa: PLC0415
    require_tenant(tenantId, principal)
    context = await build_audit_context(di, tenantId, UUID(subjectId))
    rules   = await load_rules(audit, scope="WITHIN_CASE")
    rule    = next((r for r in rules if r.rule_code == ruleCode), None)
    if not rule:
        raise HTTPException(status_code=404, detail=f"Rule {ruleCode!r} not found")
    finding = evaluate_rule(rule, context)
    return ok({
        "ruleCode":  ruleCode,
        "result":    finding.result.value,
        "detail":    finding.detail,
        "leftValue":  finding.left_value,
        "rightValue": finding.right_value,
    })
