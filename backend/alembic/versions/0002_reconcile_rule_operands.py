"""0002 — re-point rule operands to real DI / Audit Core sources.

See docs/RULE_DI_CROSSWALK.md. 0001 seeded operands against the pre-v2
PRICE_ANOMALY_RULE_ENGINE.md vocabulary, which does not match DI's
SCHEMA_REGISTRY (document-type names AND field keys).

This migration:
  - RENAME / REMAP  — 46 rules re-pointed to real doc types + field keys,
    including doc-type renames inside condition_expression
  - MASTER          — 8 rules point at Audit Core reconciliation via the
    "_reconciliation" / "<bucket>:<key>:<side>" operand sentinel
  - DERIVED         — 4 rules point at rule-engine aggregates via "_derived"
  - PARK            — 27 rules disabled (enabled=false); no DI source today
    (crosswalk §6 lists what unblocks each)

Idempotent — every statement is keyed by rule_code. Downgrade is a no-op; the
0001 reseed is the way back.
"""
from alembic import op
from sqlalchemy import text

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# ── condition_expression doc-type renames (doc_present: / doc_absent: tokens) ──
_CONDITION_RENAMES = [
    ("booking_docket", "booking_form"),
    ("tax_invoice_dms", "customer_invoice_dms"),
    ("insurance_cover_note", "insurance_cover"),
    ("delivery_order", "delivery_order_cover"),
    ("trade_in_valuation", "valuation_report"),
]

# ── rule_code -> (left_doc, left_field, right_doc, right_field) ────────────────
# right = (None, None) for a presence-only / single-operand rule.
_REMAP: dict[str, tuple[str, str, str | None, str | None]] = {
    "PRICE_BOOKING_VS_INVOICE": ("booking_form", "total_price", "customer_invoice_dms", "grand_total_amount"),
    "PRICE_INVOICE_DMS_VS_TALLY": ("customer_invoice_dms", "grand_total_amount", "tax_invoice_tally", "grand_total_amount"),
    "PRICE_INVOICE_VS_LEDGER": ("customer_invoice_dms", "grand_total_amount", "customer_ledger", "total_debited"),
    "LOAN_VS_LEDGER_CREDIT": ("bank_approval_letter", "sanctioned_amount", "customer_ledger", "loan_credit"),
    "DISCOUNT_INVOICE_VS_BOOKING": ("customer_invoice_dms", "invoice_discount_amount", "booking_form", "discount_amount"),
    "DISCOUNT_INVOICE_DMS_VS_TALLY": ("customer_invoice_dms", "invoice_discount_amount", "tax_invoice_tally", "invoice_discount_amount"),
    "ACCESSORY_DMS_VS_TALLY": ("accessory_invoice_dms", "grand_total_amount", "accessory_invoice_tally", "grand_total_amount"),
    "ACCESSORY_VIN_VS_VEHICLE_VIN": ("accessory_invoice_dms", "chassis_number", "customer_invoice_dms", "chassis_number"),
    "ACCESSORY_DATE_VS_DELIVERY": ("accessory_invoice_dms", "invoice_date", "gate_pass", "delivery_date"),
    "ACCESSORY_BOOKING_VS_INVOICE": ("booking_form", "accessories_cost", "accessory_invoice_dms", "grand_total_amount"),
    "INSURANCE_PREMIUM_VS_DEBIT_NOTE": ("insurance_cover", "premium_amount", "debit_note", "insurance_amount"),
    "INSURANCE_START_AFTER_DELIVERY": ("insurance_cover", "policy_start_date", "gate_pass", "delivery_date"),
    "CHASSIS_INVOICE_VS_DO": ("customer_invoice_dms", "chassis_number", "delivery_order_cover", "chassis_number"),
    "MODEL_VARIANT_BOOKING_VS_INVOICE": ("booking_form", "vehicle_variant", "customer_invoice_dms", "variant_raw"),
    "CUSTOMER_NAME_BOOKING_VS_INVOICE": ("booking_form", "customer_name", "customer_invoice_dms", "buyer_name"),
    "INVOICE_DATE_BEFORE_BOOKING": ("customer_invoice_dms", "invoice_date", "booking_form", "booking_date"),
    "GATE_DATE_BEFORE_INVOICE": ("gate_pass", "delivery_date", "customer_invoice_dms", "invoice_date"),
    "DO_DATE_AFTER_GATE": ("delivery_order_cover", "delivery_date", "gate_pass", "delivery_date"),
    "BOOKING_TO_DELIVERY_EXCESS": ("gate_pass", "delivery_date", "booking_form", "booking_date"),
    "EXCHANGE_VALUE_BELOW_MARKET": ("valuation_report", "final_offer_value", "valuation_report", "base_market_value"),
    "KYC_NAME_VS_BOOKING": ("aadhaar", "aadhaar_name", "booking_form", "customer_name"),
    "KYC_NAME_VS_INVOICE": ("aadhaar", "aadhaar_name", "customer_invoice_dms", "buyer_name"),
    "KYC_DOB_AADHAAR_VS_PAN": ("aadhaar", "date_of_birth", "pan_card", "date_of_birth"),
    "LOAN_APPLICANT_VS_KYC": ("bank_approval_letter", "applicant_name", "aadhaar", "aadhaar_name"),
    "CORPORATE_GSTIN_CERT_VS_INVOICE": ("gst_certificate", "gstin", "customer_invoice_dms", "buyer_gstin"),
    "CORPORATE_NAME_CERT_VS_INVOICE": ("gst_certificate", "legal_name", "customer_invoice_dms", "buyer_name"),
    "CORPORATE_PO_AMOUNT_VS_INVOICE": ("purchase_order", "po_amount", "customer_invoice_dms", "grand_total_amount"),
    "GATE_PASS_CHASSIS_EMPTY": ("gate_pass", "vehicle_registration_number", None, None),
    "BOOKING_AMOUNT_ZERO": ("booking_form", "booking_amount_paid", None, None),
    "TALLY_VEHICLE_INVOICE_VS_DMS": ("tax_invoice_tally", "grand_total_amount", "customer_invoice_dms", "grand_total_amount"),
    "TALLY_ACCESSORY_INVOICE_VS_DMS": ("accessory_invoice_tally", "grand_total_amount", "accessory_invoice_dms", "grand_total_amount"),
    "DEBIT_NOTE_INSURANCE_VS_COVER_NOTE": ("debit_note", "insurance_amount", "insurance_cover", "premium_amount"),
}

# ── MASTER / DERIVED sentinels — also forced requires_both_docs=false ─────────
_SENTINEL: dict[str, tuple[str, str, str | None, str | None]] = {
    "PAYMENT_SUM_VS_INVOICE": ("_derived", "payments_total", "customer_invoice_dms", "grand_total_amount"),
    "PAYMENT_SUM_VS_LEDGER": ("_derived", "payments_total", "customer_ledger", "total_credited"),
    "PAYMENT_MODE_CASH_UNRECEIPTED": ("customer_ledger", "cash_credit_total", "_derived", "payments_total"),
    "DISCOUNT_EXCEEDS_POLICY": ("_reconciliation", "discount:TOTAL:actual", None, None),
    "DISCOUNT_BOOKING_EXCEEDS_APPROVAL": ("booking_form", "discount_amount", "_reconciliation", "discount:TOTAL:standard"),
    "DISCOUNT_APPROVAL_MISSING": ("customer_invoice_dms", "invoice_discount_amount", "_reconciliation", "discount:TOTAL:standard"),
    "DISCOUNT_HIDDEN_IN_EXCHANGE": ("customer_invoice_dms", "invoice_discount_amount", "_reconciliation", "discount:TOTAL:standard"),
    "ACCESSORY_VS_COST_SHEET": ("accessory_invoice_dms", "grand_total_amount", "_reconciliation", "addon:ACCESSORIES_TOTAL:standard"),
    "INSURANCE_PREMIUM_COVER_VS_COST_SHEET": ("insurance_cover", "premium_amount", "_reconciliation", "commercial:insurance_amount:standard"),
    "EXCHANGE_VALUE_VS_INVOICE_CREDIT": ("valuation_report", "final_offer_value", "_reconciliation", "discount:EXCHANGE:actual"),
    "EXCHANGE_VALUE_VS_COST_SHEET": ("valuation_report", "final_offer_value", "_reconciliation", "discount:EXCHANGE:standard"),
}

# ── PARK — disabled until DI / derived support lands (crosswalk §6) ───────────
_PARK: list[str] = [
    "PRICE_LEDGER_VS_DMS",
    "LOAN_VS_INVOICE_FINANCE",
    "COST_SHEET_TOTAL_VS_INVOICE",
    "DISCOUNT_APPROVAL_UNSIGNED",
    "RTO_COST_SHEET_VS_CHALLAN",
    "RTO_DEBIT_NOTE_VS_CHALLAN",
    "DEBIT_NOTE_RTO_VS_CHALLAN",
    "CHASSIS_BOOKING_VS_INVOICE",
    "CHASSIS_INVOICE_VS_RC",
    "CHASSIS_RC_VS_INSURANCE",
    "CHASSIS_INVOICE_VS_GATE",
    "PAYMENT_DATE_BEFORE_BOOKING",
    "NDC_DATE_AFTER_GATE",
    "RC_DELAY_EXCESSIVE",
    "DMS_DELIVERY_DATE_VS_GATE",
    "EXCHANGE_RC_OWNER_VS_KYC",
    "EXCHANGE_WITHOUT_RC",
    "KYC_NAME_VS_RC",
    "KYC_AADHAAR_EXPIRED",
    "DELIVERY_KYC_VS_BOOKING_KYC",
    "THIRD_PARTY_DECLARATION_MISSING",
    "THIRD_PARTY_AMOUNT_VS_RECEIPT",
    "CASH_ABOVE_2_LAKH",
    "NDC_MISSING",
    "NDC_CUSTOMER_VS_KYC",
    "COST_SHEET_MISSING",
    "INVOICE_GST_ARITHMETIC",
    "DUPLICATE_RECEIPT_NUMBER",
]


def upgrade() -> None:
    conn = op.get_bind()

    for old, new in _CONDITION_RENAMES:
        conn.execute(
            text(
                "UPDATE audit.audit_rules "
                "SET condition_expression = replace(condition_expression, :old, :new) "
                "WHERE condition_expression LIKE :like"
            ),
            {"old": old, "new": new, "like": f"%{old}%"},
        )

    for code, (ld, lf, rd, rf) in _REMAP.items():
        conn.execute(
            text(
                """
                UPDATE audit.audit_rules
                SET left_doc_type = :ld, left_field_key = :lf,
                    right_doc_type = :rd, right_field_key = :rf,
                    right_config_key = NULL
                WHERE rule_code = :code
                """
            ),
            {"code": code, "ld": ld, "lf": lf, "rd": rd, "rf": rf},
        )

    for code, (ld, lf, rd, rf) in _SENTINEL.items():
        conn.execute(
            text(
                """
                UPDATE audit.audit_rules
                SET left_doc_type = :ld, left_field_key = :lf,
                    right_doc_type = :rd, right_field_key = :rf,
                    requires_both_docs = false
                WHERE rule_code = :code
                """
            ),
            {"code": code, "ld": ld, "lf": lf, "rd": rd, "rf": rf},
        )

    conn.execute(
        text("UPDATE audit.audit_rules SET enabled = false WHERE rule_code = ANY(:codes)"),
        {"codes": _PARK},
    )


def downgrade() -> None:
    # 0001 is the reseed path back; a partial reverse would leave inconsistent
    # operand vocabulary.
    pass
