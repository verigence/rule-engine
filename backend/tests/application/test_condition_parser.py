"""test_condition_parser.py — Unit tests for the condition_expression DSL."""
from __future__ import annotations

from uuid import uuid4

from verigence.audit.application.condition_parser import evaluate_condition, validate_condition
from verigence.audit.domain.types import AuditContext, DocumentContext


def _ctx(*doc_types_and_fields: tuple[str, dict]) -> AuditContext:
    """Build a minimal AuditContext with the given (doc_type, indexed_fields) pairs."""
    docs = [
        DocumentContext(
            document_id=uuid4(),
            document_type_key=dt,
            indexed_fields=fields,
        )
        for dt, fields in doc_types_and_fields
    ]
    return AuditContext(tenant_id="t1", subject_id=uuid4(), documents=docs, config={})


# ── doc_present ────────────────────────────────────────────────────────────────

def test_doc_present_true():
    ctx = _ctx(("gate_pass", {}))
    assert evaluate_condition("doc_present:gate_pass", ctx) is True

def test_doc_present_false():
    ctx = _ctx(("booking_docket", {}))
    assert evaluate_condition("doc_present:gate_pass", ctx) is False


# ── doc_absent ────────────────────────────────────────────────────────────────

def test_doc_absent_true():
    ctx = _ctx(("booking_docket", {}))
    assert evaluate_condition("doc_absent:gate_pass", ctx) is True

def test_doc_absent_false():
    ctx = _ctx(("gate_pass", {}))
    assert evaluate_condition("doc_absent:gate_pass", ctx) is False


# ── field_gt ─────────────────────────────────────────────────────────────────

def test_field_gt_true():
    ctx = _ctx(("tax_invoice_dms", {"discount_amount": "50000"}))
    assert evaluate_condition("field_gt:tax_invoice_dms.discount_amount:0", ctx) is True

def test_field_gt_false_zero_value():
    ctx = _ctx(("tax_invoice_dms", {"discount_amount": "0"}))
    assert evaluate_condition("field_gt:tax_invoice_dms.discount_amount:0", ctx) is False

def test_field_gt_false_field_absent():
    ctx = _ctx(("tax_invoice_dms", {}))
    assert evaluate_condition("field_gt:tax_invoice_dms.discount_amount:0", ctx) is False


# ── AND compound ───────────────────────────────────────────────────────────────

def test_and_both_true():
    ctx = _ctx(("gate_pass", {}), ("tax_invoice_dms", {}))
    assert evaluate_condition("doc_present:gate_pass AND doc_present:tax_invoice_dms", ctx) is True

def test_and_one_false():
    ctx = _ctx(("gate_pass", {}))
    assert evaluate_condition("doc_present:gate_pass AND doc_absent:gate_pass", ctx) is False


# ── OR compound ────────────────────────────────────────────────────────────────

def test_or_first_true():
    ctx = _ctx(("gate_pass", {}))
    assert evaluate_condition("doc_present:gate_pass OR doc_present:ndc", ctx) is True

def test_or_both_false():
    ctx = _ctx(("booking_docket", {}))
    assert evaluate_condition("doc_present:gate_pass OR doc_present:ndc", ctx) is False


# ── Edge cases ────────────────────────────────────────────────────────────────

def test_empty_expr_always_true():
    ctx = _ctx()
    assert evaluate_condition("", ctx) is True
    assert evaluate_condition("   ", ctx) is True

def test_unknown_atom_is_false():
    ctx = _ctx(("gate_pass", {}))
    assert evaluate_condition("unknown_atom:whatever", ctx) is False


# ── validate_condition (authoring-time checks, no context needed) ──────────────

def test_validate_empty_and_none_are_valid():
    assert validate_condition(None) == []
    assert validate_condition("") == []
    assert validate_condition("   ") == []

def test_validate_single_atoms_are_valid():
    assert validate_condition("doc_present:gate_pass") == []
    assert validate_condition("doc_absent:gate_pass") == []
    assert validate_condition("field_gt:tax_invoice_dms.discount_amount:0") == []

def test_validate_and_or_compounds_are_valid():
    assert validate_condition("doc_present:gate_pass AND doc_absent:ndc") == []
    assert validate_condition("doc_present:gate_pass OR doc_present:ndc") == []

def test_validate_rejects_mixed_and_or():
    errors = validate_condition("doc_present:gate_pass AND doc_absent:ndc OR doc_present:rc")
    assert len(errors) == 1
    assert "mix AND and OR" in errors[0]

def test_validate_rejects_unrecognized_prefix():
    errors = validate_condition("something_else:whatever")
    assert len(errors) == 1
    assert "unrecognized atom" in errors[0]

def test_validate_rejects_field_gt_missing_threshold():
    errors = validate_condition("field_gt:tax_invoice_dms.discount_amount")
    assert len(errors) == 1
    assert "malformed field_gt atom" in errors[0]

def test_validate_rejects_field_gt_non_numeric_threshold():
    errors = validate_condition("field_gt:tax_invoice_dms.discount_amount:abc")
    assert len(errors) == 1
    assert "non-numeric threshold" in errors[0]

def test_validate_rejects_field_gt_missing_dot():
    errors = validate_condition("field_gt:tax_invoice_dms:0")
    assert len(errors) == 1
    assert "malformed field_gt atom" in errors[0]

def test_validate_rejects_doc_present_missing_doc_type():
    errors = validate_condition("doc_present:")
    assert len(errors) == 1
    assert "missing doc_type" in errors[0]

def test_validate_reports_one_error_per_bad_atom():
    errors = validate_condition("doc_present: AND field_gt:x:abc")
    assert len(errors) == 2
