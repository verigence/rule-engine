"""evaluator.py — Load rules, evaluate each, return AuditRunSummary.

Pure Python after the initial DB loads — no FastAPI, no HTTP.
Can be unit-tested by passing a mock AuditContext with no DB.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from verigence.audit.application.condition_parser import evaluate_condition
from verigence.audit.application.context_builder import (
    _to_str,
    aggregate_field,
    build_audit_context,
)
from verigence.audit.domain.comparators import evaluate as run_comparator
from verigence.audit.domain.types import (
    AuditContext,
    AuditFinding,
    AuditResult,
    AuditRule,
    AuditRunSummary,
)

logger = structlog.get_logger(__name__)


# ── Rule loading ────────────────────────────────────────────────────────────────

async def load_rules(
    audit_session: AsyncSession,
    scope: str = "WITHIN_CASE",
    phases: list[str] | None = None,
) -> list[AuditRule]:
    """
    Load enabled rules from audit.audit_rules.

    When phases is given, returns rules where:
      phases @> '["<phase>"]'  OR  phases @> '["FULL"]'
    i.e. rules tagged for that phase, plus rules tagged FULL (run in every phase).
    """
    base_sql = """
        SELECT rule_code, category, audit_scope, phases,
               left_doc_type, left_field_key, left_aggregation,
               right_doc_type, right_field_key, right_aggregation, right_config_key,
               comparator, threshold,
               severity, finding_message, condition_expression,
               requires_both_docs, enabled
        FROM   audit.audit_rules
        WHERE  enabled = true
          AND  audit_scope = :scope
    """
    params: dict[str, Any] = {"scope": scope}

    if phases:
        # Build one JSONB containment clause per requested phase, unioned with FULL.
        # e.g.  AND (phases @> '["DELIVERY"]'::jsonb OR phases @> '["FULL"]'::jsonb)
        phase_clauses = " OR ".join(
            f"phases @> :phase_{i}::jsonb" for i in range(len(phases))
        )
        base_sql += f" AND ({phase_clauses} OR phases @> '[\"FULL\"]'::jsonb)"
        for i, phase in enumerate(phases):
            params[f"phase_{i}"] = json.dumps([phase])

    rows = (await audit_session.execute(text(base_sql), params)).mappings().all()

    return [
        AuditRule(
            rule_code=row["rule_code"],
            category=row["category"],
            audit_scope=row["audit_scope"],
            phases=(
                row["phases"]
                if isinstance(row["phases"], list)
                else json.loads(row["phases"])
            ),
            left_doc_type=row["left_doc_type"],
            left_field_key=row["left_field_key"],
            left_aggregation=row["left_aggregation"] or "SINGLE",
            right_doc_type=row["right_doc_type"],
            right_field_key=row["right_field_key"],
            right_aggregation=row["right_aggregation"] or "SINGLE",
            right_config_key=row["right_config_key"],
            comparator=row["comparator"],
            threshold=float(row["threshold"]),
            severity=row["severity"],
            finding_message=row["finding_message"],
            condition_expression=row["condition_expression"],
            requires_both_docs=bool(row["requires_both_docs"]),
            enabled=bool(row["enabled"]),
        )
        for row in rows
    ]


# ── Operand resolution ───────────────────────────────────────────────────────────

def resolve_operand(
    context: AuditContext,
    doc_type: str | None,
    field_key: str | None,
    aggregation: str,
    config_key: str | None = None,
) -> tuple[Any, UUID | None]:
    """
    Resolve a rule operand to a (value, source_doc_id) pair.
    Returns (None, None) when the operand cannot be resolved.

    Priority:
      1. config_key → context.config lookup
      2. doc_type + field_key → aggregate_field (numeric, then date, then string)
    """
    if config_key:
        return context.config.get(config_key), None

    if not doc_type or not field_key:
        return None, None

    # Numeric aggregation
    val = aggregate_field(context.documents, doc_type, field_key, aggregation, as_date=False)
    # Date aggregation fallback
    if val is None:
        val = aggregate_field(context.documents, doc_type, field_key, aggregation, as_date=True)
    # Raw string fallback (for NOT_EQ on text fields)
    if val is None:
        typed_docs = [d for d in context.documents if d.document_type_key == doc_type]
        if typed_docs:
            val = _to_str(typed_docs[0].indexed_fields.get(field_key))

    # Source doc ID
    typed_docs = [d for d in context.documents if d.document_type_key == doc_type]
    source_id: UUID | None = typed_docs[0].document_id if typed_docs else None

    return val, source_id


# ── Message rendering ──────────────────────────────────────────────────────────────

def _render_message(template: str, left: Any, right: Any) -> str:
    """Fill {left}, {right}, {diff} placeholders in finding_message."""
    left_str  = str(left)  if left  is not None else ""
    right_str = str(right) if right is not None else ""
    try:
        diff_str = str(round(abs(float(str(left or 0)) - float(str(right or 0))), 2))
    except (ValueError, TypeError):
        diff_str = ""
    return (
        template
        .replace("{left}",  left_str)
        .replace("{right}", right_str)
        .replace("{diff}",  diff_str)
    )


# ── Single-rule evaluation ─────────────────────────────────────────────────────────

def _skipped(rule: AuditRule, reason: str) -> AuditFinding:
    """Convenience: build a SKIPPED finding."""
    return AuditFinding(
        rule_code=rule.rule_code,
        category=rule.category,
        audit_scope=rule.audit_scope,
        result=AuditResult.SKIPPED,
        severity=rule.severity,
        left_value=None, right_value=None,
        left_doc_id=None, right_doc_id=None,
        detail=reason,
    )


def evaluate_rule(rule: AuditRule, context: AuditContext) -> AuditFinding:
    """
    Evaluate a single rule against an AuditContext.
    Returns PASS, FAIL, or SKIPPED.
    SKIPPED findings are NOT persisted — only counted in the run summary.
    """
    # Step 1: condition_expression pre-check
    if rule.condition_expression:
        if not evaluate_condition(rule.condition_expression, context):
            return _skipped(rule, f"condition not met: {rule.condition_expression}")

    # Step 2: requires_both_docs — both document types must exist in context
    if rule.requires_both_docs:
        if rule.left_doc_type and not any(
            d.document_type_key == rule.left_doc_type for d in context.documents
        ):
            return _skipped(rule, f"{rule.left_doc_type} not present for this subject")
        if rule.right_doc_type and not any(
            d.document_type_key == rule.right_doc_type for d in context.documents
        ):
            return _skipped(rule, f"{rule.right_doc_type} not present for this subject")

    # Step 3: resolve operands
    left_val,  left_doc_id  = resolve_operand(
        context, rule.left_doc_type,  rule.left_field_key,
        rule.left_aggregation,  config_key=None,
    )
    right_val, right_doc_id = resolve_operand(
        context, rule.right_doc_type, rule.right_field_key,
        rule.right_aggregation, config_key=rule.right_config_key,
    )

    # Step 4: run comparator
    result = run_comparator(rule.comparator, left_val, right_val, rule.threshold)

    # Step 5: build finding
    if result == AuditResult.SKIPPED:
        detail = f"Operand not resolved — left={left_val!r}, right={right_val!r}"
    else:
        detail = _render_message(rule.finding_message, left_val, right_val)

    return AuditFinding(
        rule_code=rule.rule_code,
        category=rule.category,
        audit_scope=rule.audit_scope,
        result=result,
        severity=rule.severity,
        left_value=str(left_val)   if left_val  is not None else None,
        right_value=str(right_val) if right_val is not None else None,
        left_doc_id=left_doc_id,
        right_doc_id=right_doc_id,
        detail=detail,
    )


# ── Verdict ───────────────────────────────────────────────────────────────────────────

def _compute_verdict(
    fail_count: int, critical_fail: int, skipped_count: int, total_rules: int
) -> str:
    if total_rules == 0 or skipped_count == total_rules:
        return "INSUFFICIENT_DATA"
    if critical_fail > 0:
        return "CRITICAL_OPEN"
    if fail_count > 0:
        return "FINDINGS_PRESENT"
    return "CLEAN"


# ── Full audit run ─────────────────────────────────────────────────────────────────

async def run_audit(
    di_session: AsyncSession,
    audit_session: AsyncSession,
    tenant_id: str,
    subject_id: UUID | str,
    phases: list[str] | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> AuditRunSummary:
    """
    Full within-case audit for one subject.
    Returns AuditRunSummary with findings[] populated.
    Does NOT persist — caller is responsible for persistence.
    """
    context = await build_audit_context(di_session, tenant_id, subject_id, config_overrides)
    rules   = await load_rules(audit_session, scope="WITHIN_CASE", phases=phases)

    findings:        list[AuditFinding]  = []
    skipped_reasons: dict[str, str]     = {}
    pass_count = fail_count = skipped_count = 0
    critical_fail = warning_fail = info_fail = 0

    for rule in rules:
        finding = evaluate_rule(rule, context)
        findings.append(finding)

        match finding.result:
            case AuditResult.PASS:
                pass_count += 1
            case AuditResult.FAIL:
                fail_count += 1
                if finding.severity == "CRITICAL":
                    critical_fail += 1
                elif finding.severity == "WARNING":
                    warning_fail += 1
                else:
                    info_fail += 1
            case AuditResult.SKIPPED:
                skipped_count += 1
                skipped_reasons[rule.rule_code] = finding.detail

    verdict = _compute_verdict(fail_count, critical_fail, skipped_count, len(rules))

    logger.info(
        "audit_run_complete",
        tenant_id=tenant_id,
        subject_id=str(subject_id),
        total=len(rules),
        passed=pass_count,
        failed=fail_count,
        skipped=skipped_count,
        verdict=verdict,
    )

    return AuditRunSummary(
        audit_run_id=None,  # set by caller after DB persistence
        total_rules=len(rules),
        pass_count=pass_count,
        fail_count=fail_count,
        skipped_count=skipped_count,
        critical_fail=critical_fail,
        warning_fail=warning_fail,
        info_fail=info_fail,
        verdict=verdict,
        skipped_reasons=skipped_reasons,
        findings=findings,
    )
