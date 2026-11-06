"""test_run_dedup.py — the "unchanged result" comparison and the write/skip
wiring in _run_and_persist, without a database.

The SQL itself is exercised against a real Postgres in
tests/integration/test_run_dedup_postgres.py (opt-in, AUDIT_TEST_DB_URL).
"""
from __future__ import annotations

import copy
import json
import os
from typing import Any
from uuid import UUID, uuid4

import pytest

os.environ.setdefault("AUDIT_SECRET_KEY", "dev-secret-key-change-in-production-must-be-32c")
os.environ.setdefault("AUDIT_DB_URL",    "postgresql+asyncpg://localhost/test")
os.environ.setdefault("AUDIT_DI_DB_URL", "postgresql+asyncpg://localhost/test")

from verigence.audit.api.v1 import audit as audit_api  # noqa: E402
from verigence.audit.domain.types import AuditFinding, AuditResult, AuditRunSummary  # noqa: E402
from verigence.audit.repositories import run_dedup  # noqa: E402

_DOC = UUID("11111111-1111-1111-1111-111111111111")


def _finding(code: str, result: AuditResult, **kw: Any) -> AuditFinding:
    base: dict[str, Any] = {
        "rule_code": code, "category": "PRICE", "audit_scope": "WITHIN_CASE",
        "result": result, "severity": "WARNING", "left_value": "100",
        "right_value": "100", "left_doc_id": _DOC, "right_doc_id": None,
        "detail": f"{code} detail",
    }
    base.update(kw)
    return AuditFinding(**base)


def _summary(findings: list[AuditFinding]) -> AuditRunSummary:
    skipped = {f.rule_code: f.detail for f in findings if f.result == AuditResult.SKIPPED}
    fails = [f for f in findings if f.result == AuditResult.FAIL]
    return AuditRunSummary(
        audit_run_id=None,
        total_rules=len(findings),
        pass_count=sum(f.result == AuditResult.PASS for f in findings),
        fail_count=len(fails),
        skipped_count=len(skipped),
        critical_fail=sum(f.severity == "CRITICAL" for f in fails),
        warning_fail=sum(f.severity == "WARNING" for f in fails),
        info_fail=sum(f.severity == "INFO" for f in fails),
        verdict="FINDINGS_PRESENT" if fails else "CLEAN",
        skipped_reasons=skipped,
        findings=findings,
    )


def _row(f: AuditFinding, **kw: Any) -> dict[str, Any]:
    """A finding as persist_findings stores it and asyncpg reads it back."""
    row = {
        "rule_code": f.rule_code, "audit_scope": "WITHIN_CASE",
        "result": f.result.value, "severity": f.severity,
        "left_value": f.left_value, "right_value": f.right_value,
        "left_doc_id": f.left_doc_id, "right_doc_id": f.right_doc_id,
        "detail": f.detail,
        "affected_subjects": (json.dumps([str(s) for s in f.affected_subjects])
                              if f.affected_subjects else None),
    }
    row.update(kw)
    return row


# ── The comparison ────────────────────────────────────────────────────────────

def test_skipped_findings_are_not_part_of_the_stored_set() -> None:
    s = _summary([_finding("A", AuditResult.PASS), _finding("B", AuditResult.SKIPPED)])
    assert set(run_dedup.new_finding_signatures(s, "WITHIN_CASE") or {}) == {"A"}


def test_repeated_rule_code_is_never_treated_as_unchanged() -> None:
    s = _summary([_finding("A", AuditResult.PASS), _finding("A", AuditResult.FAIL)])
    assert run_dedup.new_finding_signatures(s, "WITHIN_CASE") is None


def test_identical_rows_match() -> None:
    fa, fb = _finding("A", AuditResult.PASS), _finding("B", AuditResult.FAIL, right_value="90")
    expected = run_dedup.new_finding_signatures(_summary([fa, fb]), "WITHIN_CASE")
    assert expected is not None
    assert run_dedup.rows_match([_row(fb), _row(fa)], expected)


def test_affected_subjects_as_jsonb_list_or_text_both_match() -> None:
    sub = uuid4()
    f = _finding("A", AuditResult.FAIL, affected_subjects=[sub])
    expected = run_dedup.new_finding_signatures(_summary([f]), "WITHIN_CASE")
    assert expected is not None
    assert run_dedup.rows_match([_row(f)], expected)
    assert run_dedup.rows_match([_row(f, affected_subjects=[str(sub)])], expected)


@pytest.mark.parametrize(
    "change",
    [
        {"result": "FAIL"},
        {"severity": "CRITICAL"},
        {"left_value": "101"},
        {"right_value": None},
        {"left_doc_id": UUID("22222222-2222-2222-2222-222222222222")},
        {"right_doc_id": _DOC},
        {"detail": "other text"},
        {"affected_subjects": json.dumps(["x"])},
        {"audit_scope": "CROSS_CASE"},
        {"rule_code": "Z"},
    ],
)
def test_any_stored_field_difference_is_a_change(change: dict[str, Any]) -> None:
    f = _finding("A", AuditResult.PASS)
    expected = run_dedup.new_finding_signatures(_summary([f]), "WITHIN_CASE")
    assert expected is not None
    assert not run_dedup.rows_match([_row(f, **change)], expected)


def test_missing_extra_or_duplicate_rows_are_a_change() -> None:
    fa, fb = _finding("A", AuditResult.PASS), _finding("B", AuditResult.PASS)
    expected = run_dedup.new_finding_signatures(_summary([fa, fb]), "WITHIN_CASE")
    assert expected is not None
    assert not run_dedup.rows_match([_row(fa)], expected)
    assert not run_dedup.rows_match([_row(fa), _row(fb), _row(_finding("C", AuditResult.PASS))], expected)
    assert not run_dedup.rows_match([_row(fa), _row(fa)], expected)


def test_empty_result_matches_empty_rows() -> None:
    s = _summary([_finding("A", AuditResult.SKIPPED)])
    expected = run_dedup.new_finding_signatures(s, "WITHIN_CASE")
    assert expected == {}
    assert run_dedup.rows_match([], expected)


# ── _run_and_persist wiring ──────────────────────────────────────────────────

class _Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.created = UUID("33333333-3333-3333-3333-333333333333")


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> tuple[_Recorder, dict[str, Any]]:
    rec = _Recorder()
    state: dict[str, Any] = {"unchanged": None, "summary": None}

    async def fake_run_audit(*_a: Any, **_k: Any) -> AuditRunSummary:
        return copy.deepcopy(state["summary"])

    async def fake_find(*_a: Any, **_k: Any) -> UUID | None:
        rec.calls.append("find")
        return state["unchanged"]

    async def fake_create(*_a: Any, **_k: Any) -> UUID:
        rec.calls.append("create_run")
        return rec.created

    async def fake_persist(*_a: Any, **_k: Any) -> None:
        rec.calls.append("persist_findings")

    async def fake_complete(*_a: Any, **_k: Any) -> None:
        rec.calls.append("complete_run")

    monkeypatch.setattr(audit_api, "run_audit", fake_run_audit)
    monkeypatch.setattr(audit_api, "find_unchanged_run", fake_find)
    monkeypatch.setattr(audit_api, "create_run", fake_create)
    monkeypatch.setattr(audit_api, "persist_findings", fake_persist)
    monkeypatch.setattr(audit_api, "complete_run", fake_complete)
    return rec, state


_SUBJECT = "44444444-4444-4444-4444-444444444444"


async def test_changed_result_writes_run_findings_and_completion(
    wired: tuple[_Recorder, dict[str, Any]],
) -> None:
    rec, state = wired
    state["summary"] = _summary([_finding("A", AuditResult.FAIL, right_value="90")])
    data = await audit_api._run_and_persist(None, None, "t1", _SUBJECT, phases=["BOOKING"])  # type: ignore[arg-type]
    assert rec.calls == ["find", "create_run", "persist_findings", "complete_run"]
    assert data["auditRunId"] == str(rec.created)


async def test_unchanged_result_writes_nothing_and_answers_the_same(
    wired: tuple[_Recorder, dict[str, Any]],
) -> None:
    rec, state = wired
    state["summary"] = _summary([
        _finding("A", AuditResult.FAIL, right_value="90"),
        _finding("B", AuditResult.PASS),
        _finding("C", AuditResult.SKIPPED, detail="no invoice"),
    ])
    written = await audit_api._run_and_persist(None, None, "t1", _SUBJECT, phases=["BOOKING"])  # type: ignore[arg-type]

    rec.calls.clear()
    state["unchanged"] = rec.created
    repeated = await audit_api._run_and_persist(None, None, "t1", _SUBJECT, phases=["BOOKING"])  # type: ignore[arg-type]

    assert rec.calls == ["find"]
    assert json.dumps(repeated, sort_keys=False) == json.dumps(written, sort_keys=False)
    assert repeated["skippedRules"] == [{"ruleCode": "C", "reason": "no invoice"}]
    assert [a["ruleCode"] for a in repeated["anomalies"]] == ["A"]
