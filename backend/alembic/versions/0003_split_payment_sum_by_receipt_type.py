"""0003 — split PAYMENT_SUM_VS_INVOICE into same-type-only receipt sums.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-11

PAYMENT_SUM_VS_INVOICE currently resolves its left operand via the
"_derived"/"payments_total" sentinel (see evaluator._resolve_derived_operand),
which blends dealer_receipt + upi_transaction + upi_screenshot +
bank_statement_extract amounts into one combined total, and never included
payment_receipt (Delivery's own receipt type -- it had no dedicated DI schema
when this operand was written) at all. Two problems, by explicit product
direction:

  1. Delivery-stage receipts were never counted toward this check at all.
  2. The blended sum mixes evidence types together -- receipt sums must stay
     scoped to one document type at a time, same as the duplicate-receipt
     check (verigence-audit-core uc03_duplicate_receipt_detection.py): a
     Booking advance (dealer_receipt) and a Delivery balance (payment_receipt)
     for the same journey are two different real payments, not a total to
     blend into one number.

This parks PAYMENT_SUM_VS_INVOICE (same disposition as 0002's PARK list) and
replaces it with two independent, same-type-only rules using the plain
doc_type+field_key SUM aggregation already supported natively -- no change to
the shared "_derived"/"payments_total" operand, which PAYMENT_SUM_VS_LEDGER
and PAYMENT_MODE_CASH_UNRECEIPTED still use unchanged (out of scope here).

  BOOKING_RECEIPT_SUM_VS_BOOKING_FORM
    SUM(dealer_receipt.amount_paid) vs booking_form.booking_amount_paid
  DELIVERY_RECEIPT_SUM_VS_INVOICE
    SUM(payment_receipt.amount_paid) vs customer_invoice_dms.grand_total_amount

UPI/bank-statement evidence is deliberately left out of both for now rather
than guessed into either side -- a real Journey may or may not have it, and
which "type" it belongs to needs its own product decision, not an assumption
made while fixing this specific rule. DELIVERY_RECEIPT_SUM_VS_INVOICE will
under-count (and may false-flag) a Journey where part of the Delivery-stage
balance was paid by UPI/bank transfer rather than a payment_receipt document
-- a known, named limitation, not a silent gap.
"""
from __future__ import annotations

import json

from alembic import op
from sqlalchemy import text

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_PARK: list[str] = ["PAYMENT_SUM_VS_INVOICE"]

# rule_code -> (category, phases, left_doc, left_field, left_agg,
#               right_doc, right_field, right_agg, comparator, threshold,
#               severity, finding_message)
_NEW_RULES: dict[str, tuple] = {
    "BOOKING_RECEIPT_SUM_VS_BOOKING_FORM": (
        "PRICE", ["BOOKING"],
        "dealer_receipt", "amount_paid", "SUM",
        "booking_form", "booking_amount_paid", "SINGLE",
        "ABS_DIFF_GT", 1000, "CRITICAL",
        "Sum of dealer receipts (₹{left}) ≠ amount declared paid on "
        "the Booking Form (₹{right}). Diff = ₹{diff}.",
    ),
    "DELIVERY_RECEIPT_SUM_VS_INVOICE": (
        "PRICE", ["DELIVERY", "FINANCE"],
        "payment_receipt", "amount_paid", "SUM",
        "customer_invoice_dms", "grand_total_amount", "SINGLE",
        "ABS_DIFF_GT", 1000, "CRITICAL",
        "Sum of Delivery payment receipts (₹{left}) ≠ invoice total "
        "(₹{right}). Diff = ₹{diff}.",
    ),
}


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        text("UPDATE audit.audit_rules SET enabled = false WHERE rule_code = ANY(:codes)"),
        {"codes": _PARK},
    )

    for code, (
        category, phases, ld, lf, la, rd, rf, ra, comparator, threshold, severity, message,
    ) in _NEW_RULES.items():
        conn.execute(
            text(
                """
                INSERT INTO audit.audit_rules
                    (rule_code, category, audit_scope, phases,
                     left_doc_type, left_field_key, left_aggregation,
                     right_doc_type, right_field_key, right_aggregation,
                     comparator, threshold, severity, finding_message,
                     condition_expression, requires_both_docs)
                VALUES
                    (:code, :category, 'WITHIN_CASE', CAST(:phases AS jsonb),
                     :ld, :lf, :la,
                     :rd, :rf, :ra,
                     :comparator, :threshold, :severity, :message,
                     NULL, TRUE)
                ON CONFLICT (rule_code) DO UPDATE SET
                    category = EXCLUDED.category,
                    audit_scope = EXCLUDED.audit_scope,
                    phases = EXCLUDED.phases,
                    left_doc_type = EXCLUDED.left_doc_type,
                    left_field_key = EXCLUDED.left_field_key,
                    left_aggregation = EXCLUDED.left_aggregation,
                    right_doc_type = EXCLUDED.right_doc_type,
                    right_field_key = EXCLUDED.right_field_key,
                    right_aggregation = EXCLUDED.right_aggregation,
                    comparator = EXCLUDED.comparator,
                    threshold = EXCLUDED.threshold,
                    severity = EXCLUDED.severity,
                    finding_message = EXCLUDED.finding_message,
                    condition_expression = EXCLUDED.condition_expression,
                    requires_both_docs = EXCLUDED.requires_both_docs,
                    enabled = true
                """
            ),
            {
                "code": code, "category": category,
                "phases": json.dumps(phases),
                "ld": ld, "lf": lf, "la": la,
                "rd": rd, "rf": rf, "ra": ra,
                "comparator": comparator, "threshold": threshold,
                "severity": severity, "message": message,
            },
        )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text("DELETE FROM audit.audit_rules WHERE rule_code = ANY(:codes)"),
        {"codes": list(_NEW_RULES)},
    )
    conn.execute(
        text("UPDATE audit.audit_rules SET enabled = true WHERE rule_code = ANY(:codes)"),
        {"codes": _PARK},
    )
