"""audit_findings.py — Persist, query, and acknowledge audit findings.

Key rules (design doc §7):
  - SKIPPED findings are never inserted
  - Batch INSERT only — never one row at a time
  - Before inserting, mark prior findings for same rule_code + subject_id as is_current=FALSE
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from verigence.audit.domain.types import AuditFinding, AuditResult


async def persist_findings(
    session: AsyncSession,
    tenant_id: str,
    subject_id: UUID | str | None,
    run_id: UUID,
    findings: list[AuditFinding],
    scope: str = "WITHIN_CASE",
) -> None:
    """
    Persist PASS + FAIL findings in a single batch INSERT.
    SKIPPED findings are silently dropped.
    Prior findings for the same rule_codes are superseded (is_current → FALSE).
    """
    # Filter out SKIPPED
    to_insert = [f for f in findings if f.result != AuditResult.SKIPPED]
    if not to_insert:
        return

    rule_codes = [f.rule_code for f in to_insert]

    # Supersede prior current findings for these rules
    if rule_codes:
        placeholders = ", ".join(f":rc{i}" for i in range(len(rule_codes)))
        params: dict[str, Any] = {
            "tid": str(tenant_id),
            "sid": str(subject_id) if subject_id else None,
            "run_id": str(run_id),
        }
        for i, rc in enumerate(rule_codes):
            params[f"rc{i}"] = rc

        await session.execute(
            text(f"""
                UPDATE audit.audit_findings
                SET    is_current          = FALSE,
                       superseded_by_run_id = :run_id
                WHERE  tenant_id  = :tid
                  AND  subject_id {'= :sid' if subject_id else 'IS NULL'}
                  AND  rule_code  IN ({placeholders})
                  AND  is_current = TRUE
            """),
            params,
        )

    # Batch INSERT
    now = datetime.now(timezone.utc)
    values_rows = []
    insert_params: dict[str, Any] = {}

    for idx, f in enumerate(to_insert):
        pfx = f"f{idx}_"
        values_rows.append(
            f"(:{pfx}tid, :{pfx}sid, :{pfx}run, :{pfx}scope, :{pfx}rc, "
            f":{pfx}res, :{pfx}sev, :{pfx}lv, :{pfx}rv, :{pfx}ld, :{pfx}rd, "
            f":{pfx}det, :{pfx}aff, :{pfx}now)"
        )
        import json  # noqa: PLC0415
        insert_params.update({
            f"{pfx}tid":  str(tenant_id),
            f"{pfx}sid":  str(subject_id) if subject_id else None,
            f"{pfx}run":  str(run_id),
            f"{pfx}scope": scope,
            f"{pfx}rc":   f.rule_code,
            f"{pfx}res":  f.result.value,
            f"{pfx}sev":  f.severity,
            f"{pfx}lv":   f.left_value,
            f"{pfx}rv":   f.right_value,
            f"{pfx}ld":   str(f.left_doc_id) if f.left_doc_id else None,
            f"{pfx}rd":   str(f.right_doc_id) if f.right_doc_id else None,
            f"{pfx}det":  f.detail,
            f"{pfx}aff":  json.dumps([str(s) for s in f.affected_subjects]) if f.affected_subjects else None,
            f"{pfx}now":  now,
        })

    await session.execute(
        text(f"""
            INSERT INTO audit.audit_findings
                (tenant_id, subject_id, audit_run_id, audit_scope, rule_code,
                 result, severity, left_value, right_value, left_doc_id, right_doc_id,
                 detail, affected_subjects, evaluated_at_utc)
            VALUES {', '.join(values_rows)}
        """),
        insert_params,
    )


async def get_findings(
    session: AsyncSession,
    tenant_id: str,
    subject_id: UUID | str,
    *,
    result: str | None = None,
    severity: str | None = None,
    category: str | None = None,
    is_current: bool = True,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Query findings with optional filters."""
    sql = """
        SELECT af.finding_id, af.rule_code, af.result, af.severity,
               ar.category, af.audit_scope,
               af.left_value, af.right_value,
               af.left_doc_id, af.right_doc_id,
               af.detail, af.affected_subjects,
               af.acknowledgement_state, af.acknowledged_by, af.acknowledged_at_utc,
               af.is_current, af.evaluated_at_utc
        FROM   audit.audit_findings af
        JOIN   audit.audit_rules ar ON ar.rule_code = af.rule_code
        WHERE  af.tenant_id  = :tid
          AND  af.subject_id = :sid
          AND  af.is_current = :curr
    """
    params: dict[str, Any] = {
        "tid":  str(tenant_id),
        "sid":  str(subject_id),
        "curr": is_current,
    }
    if result:
        sql += " AND af.result = :result"
        params["result"] = result
    if severity:
        sql += " AND af.severity = :severity"
        params["severity"] = severity
    if category:
        sql += " AND ar.category = :category"
        params["category"] = category

    sql += " ORDER BY af.evaluated_at_utc DESC LIMIT :lim"
    params["lim"] = limit

    rows = (await session.execute(text(sql), params)).mappings().all()
    return [dict(r) for r in rows]


async def get_audit_summary(
    session: AsyncSession,
    tenant_id: str,
    subject_id: UUID | str,
) -> dict[str, Any]:
    """Aggregate count of current findings by result and severity."""
    row = (
        await session.execute(
            text("""
                SELECT
                    COUNT(*)                                           AS total,
                    COUNT(*) FILTER (WHERE result='FAIL')             AS fail_count,
                    COUNT(*) FILTER (WHERE result='PASS')             AS pass_count,
                    COUNT(*) FILTER (WHERE result='FAIL' AND severity='CRITICAL'
                                       AND acknowledgement_state='PENDING') AS critical_pending,
                    COUNT(*) FILTER (WHERE result='FAIL' AND severity='WARNING') AS warning_fail,
                    COUNT(*) FILTER (WHERE result='FAIL' AND severity='INFO')    AS info_fail
                FROM audit.audit_findings
                WHERE tenant_id  = :tid
                  AND subject_id = :sid
                  AND is_current = TRUE
            """),
            {"tid": str(tenant_id), "sid": str(subject_id)},
        )
    ).mappings().first()
    return dict(row) if row else {}


async def acknowledge_finding(
    session: AsyncSession,
    tenant_id: str,
    finding_id: UUID | str,
    actor_id: str,
    note: str,
    waive: bool = False,
) -> None:
    state = "WAIVED" if waive else "ACKNOWLEDGED"
    await session.execute(
        text("""
            UPDATE audit.audit_findings
            SET    acknowledgement_state  = :state,
                   acknowledged_by        = :actor,
                   acknowledged_at_utc    = :now,
                   acknowledgement_note   = :note
            WHERE  finding_id = :fid
              AND  tenant_id  = :tid
        """),
        {
            "state": state,
            "actor": actor_id,
            "now":   datetime.now(timezone.utc),
            "note":  note,
            "fid":   str(finding_id),
            "tid":   str(tenant_id),
        },
    )


async def bulk_acknowledge(
    session: AsyncSession,
    tenant_id: str,
    finding_ids: list[UUID | str],
    actor_id: str,
    note: str,
    waive: bool = False,
) -> None:
    state = "WAIVED" if waive else "ACKNOWLEDGED"
    placeholders = ", ".join(f":fid{i}" for i in range(len(finding_ids)))
    params: dict[str, Any] = {
        "state": state,
        "actor": actor_id,
        "now":   datetime.now(timezone.utc),
        "note":  note,
        "tid":   str(tenant_id),
    }
    for i, fid in enumerate(finding_ids):
        params[f"fid{i}"] = str(fid)

    await session.execute(
        text(f"""
            UPDATE audit.audit_findings
            SET    acknowledgement_state  = :state,
                   acknowledged_by        = :actor,
                   acknowledged_at_utc    = :now,
                   acknowledgement_note   = :note
            WHERE  tenant_id   = :tid
              AND  finding_id  IN ({placeholders})
        """),
        params,
    )


async def get_pending_acknowledgements(
    session: AsyncSession,
    tenant_id: str,
    *,
    severity: str | None = None,
    older_than_hours: int | None = None,
) -> list[dict[str, Any]]:
    sql = """
        SELECT af.finding_id, af.subject_id, af.rule_code, af.severity,
               af.detail, af.evaluated_at_utc
        FROM   audit.audit_findings af
        WHERE  af.tenant_id           = :tid
          AND  af.result              = 'FAIL'
          AND  af.is_current          = TRUE
          AND  af.acknowledgement_state = 'PENDING'
    """
    params: dict[str, Any] = {"tid": str(tenant_id)}
    if severity:
        sql += " AND af.severity = :severity"
        params["severity"] = severity
    if older_than_hours is not None:
        sql += " AND af.evaluated_at_utc < NOW() - INTERVAL ':hours hours'"
        params["hours"] = older_than_hours
    sql += " ORDER BY af.severity DESC, af.evaluated_at_utc ASC LIMIT 500"

    rows = (await session.execute(text(sql), params)).mappings().all()
    return [dict(r) for r in rows]
