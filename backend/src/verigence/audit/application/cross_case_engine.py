"""cross_case_engine.py — Run D1–D6 cross-case duplicate detection rules.

Each rule does a tenant-wide GROUP BY on docintel.document_search_index,
finds field values shared across > 1 distinct subject, and creates a
CROSS_CASE AuditFinding per duplicate group.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from verigence.audit.domain.types import (
    AuditFinding,
    AuditResult,
    AuditRunSummary,
)
from verigence.audit.repositories.audit_findings import persist_findings
from verigence.audit.repositories.audit_runs import complete_run, create_run

logger = structlog.get_logger(__name__)

# Rule definitions for the 6 cross-case scans
_CROSS_CASE_RULES: list[dict[str, Any]] = [
    {
        "rule_code": "DUPLICATE_PAN_ACROSS_BOOKINGS",
        "doc_type":  "kyc_pan",
        "field_key": "pan_number",
        "severity":  "CRITICAL",
        "message":   "Same PAN {val} appears in {cnt} active bookings.",
        "category":  "CROSS_CASE",
    },
    {
        "rule_code": "DUPLICATE_AADHAAR_ACROSS_BOOKINGS",
        "doc_type":  "kyc_aadhaar",
        "field_key": "aadhaar_number",
        "severity":  "CRITICAL",
        "message":   "Same Aadhaar {val} linked to {cnt} active deals.",
        "category":  "CROSS_CASE",
    },
    {
        "rule_code": "DUPLICATE_CHASSIS_ACROSS_INVOICES",
        "doc_type":  "tax_invoice_dms",
        "field_key": "chassis_number",
        "severity":  "CRITICAL",
        "message":   "Same VIN {val} invoiced in {cnt} different deals.",
        "category":  "CROSS_CASE",
    },
    {
        "rule_code": "DUPLICATE_CHASSIS_ACROSS_GATE_PASSES",
        "doc_type":  "gate_pass",
        "field_key": "chassis_number",
        "severity":  "CRITICAL",
        "message":   "Same vehicle {val} exited the premises in {cnt} deals.",
        "category":  "CROSS_CASE",
    },
    {
        "rule_code": "DUPLICATE_RECEIPT_ACROSS_CASES",
        "doc_type":  "payment_receipt_tally",
        "field_key": "receipt_number",
        "severity":  "CRITICAL",
        "message":   "Same receipt number {val} used in {cnt} different deals.",
        "category":  "CROSS_CASE",
    },
    {
        "rule_code": "DUPLICATE_UTR_ACROSS_CASES",
        "doc_type":  "payment_receipt_tally",
        "field_key": "utr_number",
        "severity":  "CRITICAL",
        "message":   "Same UTR {val} claimed against {cnt} different deals.",
        "category":  "CROSS_CASE",
    },
]

# Template SQL — finds field values duplicated across > 1 subject
_DUPLICATE_SQL = """
    SELECT indexed_fields->>:field_key AS value,
           array_agg(DISTINCT subject_id::text) AS subject_ids,
           COUNT(DISTINCT subject_id) AS cnt
    FROM   docintel.document_search_index
    WHERE  tenant_id         = :tid
      AND  document_type_key = :doc_type
      AND  indexed_fields->>:field_key IS NOT NULL
      AND  indexed_fields->>:field_key != ''
    GROUP  BY 1
    HAVING COUNT(DISTINCT subject_id) > 1
"""


async def run_cross_case_scan(
    di_session: AsyncSession,
    audit_session: AsyncSession,
    tenant_id: str,
    rule_codes: list[str] | None = None,
) -> AuditRunSummary:
    """
    Run all 6 cross-case duplicate detection rules (or a subset).
    Creates one CROSS_CASE audit_run and persists findings.
    """
    run_id = await create_run(
        audit_session,
        tenant_id=tenant_id,
        subject_id=None,
        scope="CROSS_CASE",
        trigger_mode="SCHEDULED",
        triggered_by="cross_case_engine",
    )

    findings: list[AuditFinding] = []
    rules_to_run = [
        r for r in _CROSS_CASE_RULES
        if rule_codes is None or r["rule_code"] in rule_codes
    ]

    for rule_def in rules_to_run:
        rows = (
            await di_session.execute(
                text(_DUPLICATE_SQL),
                {
                    "tid":       str(tenant_id),
                    "doc_type":  rule_def["doc_type"],
                    "field_key": rule_def["field_key"],
                },
            )
        ).mappings().all()

        for row in rows:
            val = row["value"]
            cnt = int(row["cnt"])
            subject_ids = [UUID(s) for s in row["subject_ids"]]
            detail = (
                rule_def["message"]
                .replace("{val}", str(val))
                .replace("{cnt}", str(cnt))
            )
            findings.append(AuditFinding(
                rule_code=rule_def["rule_code"],
                category=rule_def["category"],
                audit_scope="CROSS_CASE",
                result=AuditResult.FAIL,
                severity=rule_def["severity"],
                left_value=str(val),
                right_value=str(cnt),
                left_doc_id=None,
                right_doc_id=None,
                detail=detail,
                affected_subjects=subject_ids,
            ))

    await persist_findings(
        audit_session,
        tenant_id=tenant_id,
        subject_id=None,
        run_id=run_id,
        findings=findings,
        scope="CROSS_CASE",
    )

    pass_count = 0
    fail_count = len(findings)
    critical_fail = sum(1 for f in findings if f.severity == "CRITICAL")

    verdict = "CLEAN" if fail_count == 0 else "CRITICAL_OPEN"
    summary = AuditRunSummary(
        audit_run_id=run_id,
        total_rules=len(rules_to_run),
        pass_count=pass_count,
        fail_count=fail_count,
        skipped_count=0,
        critical_fail=critical_fail,
        warning_fail=0,
        info_fail=0,
        verdict=verdict,
        skipped_reasons={},
        findings=findings,
    )
    await complete_run(audit_session, run_id, summary)

    logger.info(
        "cross_case_scan_complete",
        tenant_id=tenant_id,
        rules_run=len(rules_to_run),
        duplicates_found=fail_count,
        verdict=verdict,
    )
    return summary
