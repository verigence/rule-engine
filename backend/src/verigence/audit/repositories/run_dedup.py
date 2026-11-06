"""run_dedup.py — Recognise a WITHIN_CASE run whose result is already on record.

CHANGES, NOT REPEATS (2026-11-07, run churn). Audit Core calls
``POST .../audit/{phase}`` on every control evaluation of a Journey. Each call
used to insert a new ``audit_runs`` row and a fresh set of ``audit_findings``
rows, superseding the previous ones, even when nothing about the subject had
changed: 600-4,400 runs a day, with superseded findings outnumbering current
ones roughly 19 to 1. Same idea as Audit Core's own Execution Log fix
(``uc03_rule_execution_log.py``, 2026-10-29): write only when the result
differs from what is already recorded.

``find_unchanged_run`` returns the ``audit_run_id`` of an earlier run that
recorded exactly the result just computed — the caller then writes nothing
and answers with that run's id — or ``None``, in which case the caller writes
as before. The comparison is deliberately strict; anything it cannot prove
identical is treated as a change. A new result is "unchanged" only when ALL
of these hold:

  (a) Run. The most recent completed run for the same tenant_id, subject_id
      and audit_scope whose stored total_rules, pass_count, fail_count,
      skipped_count, critical_fail, warning_fail, info_fail and verdict all
      equal the new summary, and whose skipped_detail equals the new skipped
      rules exactly (same rule codes, same reason text). Because every
      evaluated rule is either SKIPPED or has a finding, (a) + (b) together
      also mean the same rule set (so the same phase) was evaluated.
  (b) That run's findings. Its audit_findings rows (current or not) are
      exactly the new PASS/FAIL findings: same rule codes, one row each, and
      for each the same result, severity, left_value, right_value,
      left_doc_id, right_doc_id, detail, affected_subjects and audit_scope.
  (c) The subject's current findings. For every new PASS/FAIL finding there
      is exactly one is_current finding for that subject and rule_code, and it
      is identical on the same fields as (b). (A rule shared by two phases
      may have its current row written by the other phase's run; (c) checks
      the value, whichever run wrote it.)

Timestamps, trigger_mode/triggered_by and acknowledgement fields are not part
of the result and are not compared.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from verigence.audit.domain.types import AuditFinding, AuditResult, AuditRunSummary

# (audit_scope, result, severity, left_value, right_value,
#  left_doc_id, right_doc_id, detail, affected_subjects)
FindingSignature = tuple[Any, ...]


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _affected(value: Any) -> tuple[str, ...] | None:
    """Normalise affected_subjects as persist_findings stores it: a JSON list
    of strings, or NULL when empty. Accepts the stored JSONB (list or its JSON
    text) or the domain list[UUID]."""
    if value is None:
        return None
    if isinstance(value, str):
        value = json.loads(value)
    if not value:
        return None
    return tuple(str(v) for v in value)


def signature_of_finding(f: AuditFinding, scope: str) -> FindingSignature:
    """The fields persist_findings writes for a finding, as they would be stored."""
    return (
        scope,
        f.result.value,
        f.severity,
        f.left_value,
        f.right_value,
        str(f.left_doc_id) if f.left_doc_id else None,
        str(f.right_doc_id) if f.right_doc_id else None,
        f.detail,
        _affected([str(s) for s in f.affected_subjects] if f.affected_subjects else None),
    )


def signature_of_row(row: Any) -> FindingSignature:
    """The same fields read back from an audit.audit_findings row."""
    return (
        row["audit_scope"],
        row["result"],
        row["severity"],
        row["left_value"],
        row["right_value"],
        _opt_str(row["left_doc_id"]),
        _opt_str(row["right_doc_id"]),
        row["detail"],
        _affected(row["affected_subjects"]),
    )


def new_finding_signatures(
    summary: AuditRunSummary, scope: str
) -> dict[str, FindingSignature] | None:
    """{rule_code: signature} for the findings persist_findings would insert
    (SKIPPED ones are never stored). None if a rule_code repeats -- not a shape
    the comparison is defined for, so the caller writes."""
    out: dict[str, FindingSignature] = {}
    for f in summary.findings:
        if f.result == AuditResult.SKIPPED:
            continue
        if f.rule_code in out:
            return None
        out[f.rule_code] = signature_of_finding(f, scope)
    return out


def rows_match(rows: list[Any], expected: dict[str, FindingSignature]) -> bool:
    """True when ``rows`` hold exactly one row per expected rule_code, no other
    rule_code, and each row's signature equals the expected one."""
    if len(rows) != len(expected):
        return False
    seen: set[str] = set()
    for row in rows:
        code = row["rule_code"]
        if code in seen or code not in expected:
            return False
        seen.add(code)
        if signature_of_row(row) != expected[code]:
            return False
    return True


_FINDING_COLUMNS = """
    rule_code, audit_scope, result, severity, left_value, right_value,
    left_doc_id, right_doc_id, detail, affected_subjects
"""


async def find_unchanged_run(
    session: AsyncSession,
    tenant_id: str,
    subject_id: UUID | str,
    summary: AuditRunSummary,
    scope: str = "WITHIN_CASE",
) -> UUID | None:
    """The id of an earlier run that recorded exactly this result (see the
    module note for the rule), or None when the result has to be written."""
    expected = new_finding_signatures(summary, scope)
    if expected is None:
        return None

    # (a) the most recent completed run with the same totals, verdict and skips.
    candidate = (
        await session.execute(
            text("""
                SELECT audit_run_id
                FROM   audit.audit_runs
                WHERE  tenant_id        = :tid
                  AND  subject_id       = :sid
                  AND  audit_scope      = :scope
                  AND  completed_at_utc IS NOT NULL
                  AND  total_rules      = :total
                  AND  pass_count       = :passed
                  AND  fail_count       = :failed
                  AND  skipped_count    = :skipped
                  AND  critical_fail    = :critical
                  AND  warning_fail     = :warning
                  AND  info_fail        = :info
                  AND  verdict          = :verdict
                  AND  skipped_detail   = CAST(:detail AS jsonb)
                ORDER  BY started_at_utc DESC, created_at_utc DESC
                LIMIT  1
            """),
            {
                "tid":      str(tenant_id),
                "sid":      str(subject_id),
                "scope":    scope,
                "total":    summary.total_rules,
                "passed":   summary.pass_count,
                "failed":   summary.fail_count,
                "skipped":  summary.skipped_count,
                "critical": summary.critical_fail,
                "warning":  summary.warning_fail,
                "info":     summary.info_fail,
                "verdict":  summary.verdict,
                "detail":   json.dumps(summary.skipped_reasons),
            },
        )
    ).first()
    if candidate is None:
        return None
    run_id = UUID(str(candidate[0]))

    # (b) that run's own findings are exactly the new ones.
    run_rows = (
        await session.execute(
            text(f"""
                SELECT {_FINDING_COLUMNS}
                FROM   audit.audit_findings
                WHERE  audit_run_id = :run_id
            """),
            {"run_id": str(run_id)},
        )
    ).mappings().all()
    if not rows_match(list(run_rows), expected):
        return None

    # (c) the subject's current findings for these rules hold the same values.
    if expected:
        codes = sorted(expected)
        placeholders = ", ".join(f":rc{i}" for i in range(len(codes)))
        params: dict[str, Any] = {"tid": str(tenant_id), "sid": str(subject_id)}
        params.update({f"rc{i}": code for i, code in enumerate(codes)})
        current_rows = (
            await session.execute(
                text(f"""
                    SELECT {_FINDING_COLUMNS}
                    FROM   audit.audit_findings
                    WHERE  tenant_id  = :tid
                      AND  subject_id = :sid
                      AND  is_current = TRUE
                      AND  rule_code IN ({placeholders})
                """),
                params,
            )
        ).mappings().all()
        if not rows_match(list(current_rows), expected):
            return None

    return run_id
