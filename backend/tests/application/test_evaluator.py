"""test_evaluator.py — Unit tests for evaluate_rule() and run_audit().

All tests use mock AuditContext — no DB required.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from verigence.audit.application.evaluator import evaluate_rule, _compute_verdict
from verigence.audit.domain.types import (
    AuditContext,
    AuditFinding,
    AuditResult,
    AuditRule,
    DocumentContext,
)


def _rule(**kwargs) -> AuditRule:  # type: ignore[return]
    defaults = dict(
        rule_code="TEST_RULE",
        category="PRICE",
        audit_scope="WITHIN_CASE",
        phases=["DELIVERY"],
        left_doc_type="booking_docket",
        left_field_key="agreed_price",
        left_aggregation="SINGLE",
        right_doc_type="tax_invoice_dms",
        right_field_key="net_payable",
        right_aggregation="SINGLE",
        right_config_key=None,
        comparator="ABS_DIFF_GT",
        threshold=1000.0,
        severity="CRITICAL",
        finding_message="Diff = ₹{diff}.",
        condition_expression=None,
        requires_both_docs=True,
        enabled=True,
    )
    defaults.update(kwargs)
    return AuditRule(**defaults)


def _ctx(
    booking_price: float | None = None,
    invoice_price: float | None = None,
) -> AuditContext:
    docs = []
    if booking_price is not None:
        docs.append(DocumentContext(
            document_id=uuid4(),
            document_type_key="booking_docket",
            indexed_fields={"agreed_price": str(booking_price)},
        ))
    if invoice_price is not None:
        docs.append(DocumentContext(
            document_id=uuid4(),
            document_type_key="tax_invoice_dms",
            indexed_fields={"net_payable": str(invoice_price)},
        ))
    return AuditContext(
        tenant_id="tenant-001",
        subject_id=uuid4(),
        documents=docs,
        config={},
    )


# ── evaluate_rule ────────────────────────────────────────────────────────────────

def test_evaluate_rule_fail_large_diff():
    rule = _rule()
    finding = evaluate_rule(rule, _ctx(booking_price=500000, invoice_price=503000))
    assert finding.result == AuditResult.FAIL


def test_evaluate_rule_pass_small_diff():
    finding = evaluate_rule(_rule(), _ctx(booking_price=500000, invoice_price=500500))
    assert finding.result == AuditResult.PASS


def test_evaluate_rule_skipped_missing_left_doc():
    # booking_docket absent — requires_both_docs=True → SKIPPED
    finding = evaluate_rule(_rule(), _ctx(invoice_price=500000))
    assert finding.result == AuditResult.SKIPPED
    assert "booking_docket" in finding.detail


def test_evaluate_rule_skipped_missing_right_doc():
    finding = evaluate_rule(_rule(), _ctx(booking_price=500000))
    assert finding.result == AuditResult.SKIPPED
    assert "tax_invoice_dms" in finding.detail


def test_evaluate_rule_skipped_condition_not_met():
    rule = _rule(
        requires_both_docs=False,
        condition_expression="doc_present:gate_pass",  # gate_pass not in context
    )
    finding = evaluate_rule(rule, _ctx(booking_price=500000, invoice_price=503000))
    assert finding.result == AuditResult.SKIPPED
    assert "condition not met" in finding.detail


def test_evaluate_rule_field_empty_fires_without_docs():
    rule = _rule(
        left_doc_type="gate_pass",
        left_field_key="chassis_number",
        right_doc_type=None,
        right_field_key=None,
        comparator="FIELD_EMPTY",
        threshold=0.0,
        requires_both_docs=False,
        condition_expression=None,
    )
    # gate_pass present with empty chassis_number → FAIL
    ctx = AuditContext(
        tenant_id="t1", subject_id=uuid4(),
        documents=[
            DocumentContext(
                document_id=uuid4(),
                document_type_key="gate_pass",
                indexed_fields={"chassis_number": ""},  # empty
            )
        ],
        config={},
    )
    finding = evaluate_rule(rule, ctx)
    assert finding.result == AuditResult.FAIL


# ── _compute_verdict ───────────────────────────────────────────────────────────────

def test_verdict_clean():
    assert _compute_verdict(0, 0, 0, 10) == "CLEAN"

def test_verdict_critical_open():
    assert _compute_verdict(2, 1, 0, 10) == "CRITICAL_OPEN"

def test_verdict_findings_present():
    assert _compute_verdict(1, 0, 0, 10) == "FINDINGS_PRESENT"

def test_verdict_insufficient_data_all_skipped():
    assert _compute_verdict(0, 0, 10, 10) == "INSUFFICIENT_DATA"

def test_verdict_insufficient_data_no_rules():
    assert _compute_verdict(0, 0, 0, 0) == "INSUFFICIENT_DATA"
