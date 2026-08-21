"""test_comparators.py — Unit tests for all 10 comparator functions.

No DB, no network, no async. Pure Python.
"""
from __future__ import annotations

from datetime import date

import pytest

from verigence.audit.domain.comparators import (
    compare_abs_diff_gt,
    compare_cross_doc_sum_gt,
    compare_date_before,
    compare_date_diff_gt,
    compare_eq,
    compare_field_empty,
    compare_gt,
    compare_lt,
    compare_not_eq,
    compare_ratio_lt,
    evaluate,
)
from verigence.audit.domain.types import AuditResult, Comparator


# ── ABS_DIFF_GT ────────────────────────────────────────────────────────────────

def test_abs_diff_gt_fail():
    assert compare_abs_diff_gt(50000, 48000, 1000) == AuditResult.FAIL

def test_abs_diff_gt_pass():
    assert compare_abs_diff_gt(50000, 50000, 1000) == AuditResult.PASS

def test_abs_diff_gt_boundary_pass():
    # exactly at threshold — not strictly greater
    assert compare_abs_diff_gt(50000, 49000, 1000) == AuditResult.PASS

def test_abs_diff_gt_skipped_none_left():
    assert compare_abs_diff_gt(None, 1000, 1000) == AuditResult.SKIPPED

def test_abs_diff_gt_skipped_none_right():
    assert compare_abs_diff_gt(1000, None, 1000) == AuditResult.SKIPPED


# ── NOT_EQ ────────────────────────────────────────────────────────────────────

def test_not_eq_fail_different_names():
    assert compare_not_eq("Ramesh Kumar", "Suresh Kumar", 0) == AuditResult.FAIL

def test_not_eq_pass_same_name():
    assert compare_not_eq("Ramesh Kumar", "Ramesh Kumar", 0) == AuditResult.PASS

def test_not_eq_pass_after_prefix_strip():
    # Mr. prefix removed before comparison
    assert compare_not_eq("Mr. Ramesh Kumar", "Ramesh Kumar", 0) == AuditResult.PASS

def test_not_eq_pass_case_insensitive():
    assert compare_not_eq("ramesh kumar", "RAMESH KUMAR", 0) == AuditResult.PASS

def test_not_eq_skipped_none_left():
    assert compare_not_eq(None, "Ramesh", 0) == AuditResult.SKIPPED

def test_not_eq_fail_vin_mismatch():
    assert compare_not_eq("MA3EWDE1S00123456", "MA3EWDE1S00999999", 0) == AuditResult.FAIL


# ── GT ────────────────────────────────────────────────────────────────────────

def test_gt_fail_exceeds_policy():
    # approved_discount=60000 > region_max_discount=50000 + threshold=0
    assert compare_gt(60000, 50000, 0) == AuditResult.FAIL

def test_gt_pass_within_policy():
    assert compare_gt(49000, 50000, 0) == AuditResult.PASS

def test_gt_skipped_none():
    assert compare_gt(None, 50000, 0) == AuditResult.SKIPPED


# ── LT ────────────────────────────────────────────────────────────────────────

def test_lt_fail_po_below_invoice():
    # po_amount < invoice - 0
    assert compare_lt(400000, 500000, 0) == AuditResult.FAIL

def test_lt_pass_po_equals_invoice():
    assert compare_lt(500000, 500000, 0) == AuditResult.PASS

def test_lt_skipped_none():
    assert compare_lt(None, 500000, 0) == AuditResult.SKIPPED


# ── EQ ────────────────────────────────────────────────────────────────────────

def test_eq_fail_booking_amount_zero():
    # FAIL when left == right (within threshold 0)
    assert compare_eq(0, 0, 0) == AuditResult.FAIL

def test_eq_pass_booking_amount_nonzero():
    assert compare_eq(5000, 0, 0) == AuditResult.PASS

def test_eq_skipped_none():
    assert compare_eq(None, 0, 0) == AuditResult.SKIPPED


# ── DATE_BEFORE ───────────────────────────────────────────────────────────────

def test_date_before_fail_invoice_before_booking():
    assert compare_date_before("2026-08-01", "2026-08-10", 0) == AuditResult.FAIL

def test_date_before_pass_correct_order():
    assert compare_date_before("2026-08-15", "2026-08-10", 0) == AuditResult.PASS

def test_date_before_pass_same_date():
    assert compare_date_before("2026-08-10", "2026-08-10", 0) == AuditResult.PASS

def test_date_before_skipped_none():
    assert compare_date_before(None, "2026-08-10", 0) == AuditResult.SKIPPED

def test_date_before_accepts_date_objects():
    assert compare_date_before(date(2026, 8, 1), date(2026, 8, 10), 0) == AuditResult.FAIL


# ── DATE_DIFF_GT ──────────────────────────────────────────────────────────────

def test_date_diff_gt_fail_rc_too_late():
    # RC issued 60 days after gate pass — threshold 45
    assert compare_date_diff_gt("2026-10-15", "2026-08-16", 45) == AuditResult.FAIL

def test_date_diff_gt_pass_within_threshold():
    assert compare_date_diff_gt("2026-09-15", "2026-08-16", 45) == AuditResult.PASS

def test_date_diff_gt_skipped_none():
    assert compare_date_diff_gt(None, "2026-08-16", 45) == AuditResult.SKIPPED


# ── RATIO_LT ──────────────────────────────────────────────────────────────────

def test_ratio_lt_fail_exchange_undervalued():
    # assessed 60000, benchmark 100000: ratio 0.6 < 0.85
    assert compare_ratio_lt(60000, 100000, 0.85) == AuditResult.FAIL

def test_ratio_lt_pass_fair_value():
    # assessed 90000, benchmark 100000: ratio 0.9 >= 0.85
    assert compare_ratio_lt(90000, 100000, 0.85) == AuditResult.PASS

def test_ratio_lt_skipped_none():
    assert compare_ratio_lt(None, 100000, 0.85) == AuditResult.SKIPPED

def test_ratio_lt_skipped_zero_denominator():
    assert compare_ratio_lt(60000, 0, 0.85) == AuditResult.SKIPPED


# ── FIELD_EMPTY ───────────────────────────────────────────────────────────────

def test_field_empty_fail_none():
    assert compare_field_empty(None, None, 0) == AuditResult.FAIL

def test_field_empty_fail_empty_string():
    assert compare_field_empty("", None, 0) == AuditResult.FAIL

def test_field_empty_fail_whitespace():
    assert compare_field_empty("   ", None, 0) == AuditResult.FAIL

def test_field_empty_pass_has_value():
    assert compare_field_empty("MA3EWDE1S00123456", None, 0) == AuditResult.PASS


# ── CROSS_DOC_SUM_GT ──────────────────────────────────────────────────────────

def test_cross_doc_sum_gt_fail_duplicate():
    # 3 subjects share same PAN — count=3 > threshold=1
    assert compare_cross_doc_sum_gt(3, 0, 1) == AuditResult.FAIL

def test_cross_doc_sum_gt_pass_unique():
    assert compare_cross_doc_sum_gt(1, 0, 1) == AuditResult.PASS

def test_cross_doc_sum_gt_skipped_none():
    assert compare_cross_doc_sum_gt(None, 0, 1) == AuditResult.SKIPPED


# ── evaluate() dispatch ───────────────────────────────────────────────────────

def test_evaluate_dispatches_correctly():
    result = evaluate(Comparator.ABS_DIFF_GT, 50000, 48000, 1000)
    assert result == AuditResult.FAIL

def test_evaluate_unknown_comparator_returns_skipped():
    result = evaluate("UNKNOWN_COMPARATOR", 100, 200, 0)
    assert result == AuditResult.SKIPPED

def test_evaluate_handles_exception_gracefully():
    # Force an exception inside comparator by passing absurd type — should not raise
    result = evaluate(Comparator.RATIO_LT, object(), object(), object())
    assert result == AuditResult.SKIPPED
