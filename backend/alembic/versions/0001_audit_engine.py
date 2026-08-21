"""0001_audit_engine — Create audit schema, three tables, and seed 85 rules.

Revision ID: 0001
Revises:
Create Date: 2026-08-21
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── Schema ────────────────────────────────────────────────────────────────
    conn.execute(text("CREATE SCHEMA IF NOT EXISTS audit"))

    # ── audit_rules ───────────────────────────────────────────────────────────
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS audit.audit_rules (
            rule_code               VARCHAR(80)   NOT NULL,
            category                VARCHAR(40)   NOT NULL,
            audit_scope             VARCHAR(20)   NOT NULL DEFAULT 'WITHIN_CASE',

            phases                  JSONB         NOT NULL DEFAULT '["FULL"]',

            left_doc_type           VARCHAR(80),
            left_field_key          VARCHAR(120),
            left_aggregation        VARCHAR(20)   NOT NULL DEFAULT 'SINGLE',

            right_doc_type          VARCHAR(80),
            right_field_key         VARCHAR(120),
            right_aggregation       VARCHAR(20)   NOT NULL DEFAULT 'SINGLE',
            right_config_key        VARCHAR(120),

            comparator              VARCHAR(40)   NOT NULL,
            threshold               NUMERIC(18,4) NOT NULL DEFAULT 0,

            severity                VARCHAR(20)   NOT NULL,
            finding_message         TEXT          NOT NULL,
            condition_expression    TEXT,
            requires_both_docs      BOOLEAN       NOT NULL DEFAULT FALSE,

            enabled                 BOOLEAN       NOT NULL DEFAULT TRUE,
            created_at_utc          TIMESTAMPTZ   NOT NULL DEFAULT now(),

            CONSTRAINT audit_rules_pkey PRIMARY KEY (rule_code),
            CONSTRAINT audit_rules_scope_check
                CHECK (audit_scope IN ('WITHIN_CASE', 'CROSS_CASE')),
            CONSTRAINT audit_rules_comparator_check
                CHECK (comparator IN ('ABS_DIFF_GT','NOT_EQ','GT','LT','EQ',
                                      'DATE_BEFORE','DATE_DIFF_GT','RATIO_LT',
                                      'FIELD_EMPTY','CROSS_DOC_SUM_GT')),
            CONSTRAINT audit_rules_severity_check
                CHECK (severity IN ('CRITICAL','WARNING','INFO')),
            CONSTRAINT audit_rules_aggregation_left_check
                CHECK (left_aggregation IN ('SINGLE','SUM','MAX','MIN','COUNT')),
            CONSTRAINT audit_rules_aggregation_right_check
                CHECK (right_aggregation IN ('SINGLE','SUM','MAX','MIN','COUNT'))
        )
    """))

    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_audit_rules_scope_enabled ON audit.audit_rules (audit_scope, enabled)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_audit_rules_left_doc ON audit.audit_rules (left_doc_type) WHERE enabled = TRUE"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_audit_rules_category ON audit.audit_rules (category, enabled)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_audit_rules_phases ON audit.audit_rules USING GIN (phases)"))

    # ── audit_runs ────────────────────────────────────────────────────────────
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS audit.audit_runs (
            audit_run_id        UUID         NOT NULL DEFAULT gen_random_uuid(),
            tenant_id           VARCHAR(120) NOT NULL,
            subject_id          UUID,
            audit_scope         VARCHAR(20)  NOT NULL DEFAULT 'WITHIN_CASE',
            trigger_mode        VARCHAR(20)  NOT NULL,
            triggered_by        VARCHAR(120),
            newly_confirmed_doc VARCHAR(80),

            total_rules         INTEGER      NOT NULL DEFAULT 0,
            pass_count          INTEGER      NOT NULL DEFAULT 0,
            fail_count          INTEGER      NOT NULL DEFAULT 0,
            skipped_count       INTEGER      NOT NULL DEFAULT 0,
            critical_fail       INTEGER      NOT NULL DEFAULT 0,
            warning_fail        INTEGER      NOT NULL DEFAULT 0,
            info_fail           INTEGER      NOT NULL DEFAULT 0,

            verdict             VARCHAR(30)  NOT NULL DEFAULT 'PENDING',
            skipped_detail      JSONB,

            started_at_utc      TIMESTAMPTZ  NOT NULL DEFAULT now(),
            completed_at_utc    TIMESTAMPTZ,
            created_at_utc      TIMESTAMPTZ  NOT NULL DEFAULT now(),

            CONSTRAINT audit_runs_pkey PRIMARY KEY (audit_run_id),
            CONSTRAINT audit_runs_scope_check
                CHECK (audit_scope IN ('WITHIN_CASE','CROSS_CASE')),
            CONSTRAINT audit_runs_trigger_check
                CHECK (trigger_mode IN ('EVENT_DRIVEN','ON_DEMAND','SCHEDULED'))
        )
    """))

    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_audit_runs_subject ON audit.audit_runs (tenant_id, subject_id, audit_scope)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_audit_runs_completed ON audit.audit_runs (tenant_id, completed_at_utc DESC)"))

    # ── audit_findings ────────────────────────────────────────────────────────
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS audit.audit_findings (
            finding_id              UUID         NOT NULL DEFAULT gen_random_uuid(),
            tenant_id               VARCHAR(120) NOT NULL,
            subject_id              UUID,
            audit_run_id            UUID         NOT NULL REFERENCES audit.audit_runs(audit_run_id),
            audit_scope             VARCHAR(20)  NOT NULL DEFAULT 'WITHIN_CASE',

            rule_code               VARCHAR(80)  NOT NULL REFERENCES audit.audit_rules(rule_code),
            result                  VARCHAR(20)  NOT NULL,
            severity                VARCHAR(20)  NOT NULL,

            left_value              TEXT,
            right_value             TEXT,
            left_doc_id             UUID,
            right_doc_id            UUID,
            detail                  TEXT         NOT NULL,

            affected_subjects       JSONB,

            superseded_by_run_id    UUID,
            is_current              BOOLEAN      NOT NULL DEFAULT TRUE,

            acknowledgement_state   VARCHAR(20)  NOT NULL DEFAULT 'PENDING',
            acknowledged_by         VARCHAR(120),
            acknowledged_at_utc     TIMESTAMPTZ,
            acknowledgement_note    TEXT,

            evaluated_at_utc        TIMESTAMPTZ  NOT NULL,
            created_at_utc          TIMESTAMPTZ  NOT NULL DEFAULT now(),

            CONSTRAINT audit_findings_pkey PRIMARY KEY (finding_id),
            CONSTRAINT audit_findings_result_check
                CHECK (result IN ('PASS','FAIL')),
            CONSTRAINT audit_findings_severity_check
                CHECK (severity IN ('CRITICAL','WARNING','INFO')),
            CONSTRAINT audit_findings_ack_check
                CHECK (acknowledgement_state IN ('PENDING','ACKNOWLEDGED','WAIVED'))
        )
    """))

    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_audit_findings_subject_current
            ON audit.audit_findings (tenant_id, subject_id, is_current, result)
            WHERE is_current = TRUE
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_audit_findings_run ON audit.audit_findings (audit_run_id)"))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_audit_findings_pending_critical
            ON audit.audit_findings (tenant_id, severity, acknowledgement_state)
            WHERE result = 'FAIL' AND is_current = TRUE AND acknowledgement_state = 'PENDING'
    """))

    # ── Seed: 85 rules ────────────────────────────────────────────────────────
    # Category 1 — Price Chain (9 rules)
    conn.execute(text("""
        INSERT INTO audit.audit_rules
            (rule_code, category, audit_scope, phases,
             left_doc_type, left_field_key, left_aggregation,
             right_doc_type, right_field_key, right_aggregation,
             comparator, threshold, severity, finding_message,
             condition_expression, requires_both_docs)
        VALUES
        ('PRICE_BOOKING_VS_INVOICE','PRICE','WITHIN_CASE','["BOOKING"]',
         'booking_docket','agreed_price','SINGLE',
         'tax_invoice_dms','net_payable','SINGLE',
         'ABS_DIFF_GT',1000,'CRITICAL',
         'Invoice raised for different amount (₹{right}) than what customer agreed to pay (₹{left}). Diff = ₹{diff}.',
         NULL, TRUE),

        ('PRICE_INVOICE_DMS_VS_TALLY','PRICE','WITHIN_CASE','["DELIVERY"]',
         'tax_invoice_dms','net_payable','SINGLE',
         'tax_invoice_tally','net_payable','SINGLE',
         'ABS_DIFF_GT',1000,'CRITICAL',
         'DMS invoice (₹{left}) and Tally invoice (₹{right}) show different sale values — classic books manipulation.',
         NULL, TRUE),

        ('PRICE_INVOICE_VS_LEDGER','PRICE','WITHIN_CASE','["DELIVERY"]',
         'tax_invoice_dms','net_payable','SINGLE',
         'customer_ledger','total_debited','SINGLE',
         'ABS_DIFF_GT',1000,'CRITICAL',
         'Customer ledger debit (₹{right}) ≠ invoice (₹{left}) — money entered into books at different value.',
         NULL, TRUE),

        ('PRICE_LEDGER_VS_DMS','PRICE','WITHIN_CASE','["DELIVERY"]',
         'customer_ledger','closing_balance','SINGLE',
         'tax_invoice_dms','outstanding','SINGLE',
         'ABS_DIFF_GT',1000,'WARNING',
         'DMS outstanding (₹{right}) ≠ ledger closing balance (₹{left}).',
         NULL, TRUE),

        ('PAYMENT_SUM_VS_INVOICE','PRICE','WITHIN_CASE','["DELIVERY","FINANCE"]',
         'payment_receipt_tally','amount','SUM',
         'tax_invoice_dms','net_payable','SINGLE',
         'ABS_DIFF_GT',1000,'CRITICAL',
         'Sum of all payment receipts (₹{left}) ≠ invoice total (₹{right}). Diff = ₹{diff}.',
         NULL, TRUE),

        ('PAYMENT_SUM_VS_LEDGER','PRICE','WITHIN_CASE','["DELIVERY","FINANCE"]',
         'payment_receipt_tally','amount','SUM',
         'customer_ledger','total_credited','SINGLE',
         'ABS_DIFF_GT',1000,'CRITICAL',
         'Receipts issued (₹{left}) differ from what was posted in the ledger (₹{right}).',
         NULL, TRUE),

        ('LOAN_VS_INVOICE_FINANCE','PRICE','WITHIN_CASE','["FINANCE"]',
         'bank_approval_letter','loan_amount','SINGLE',
         'tax_invoice_dms','finance_amount','SINGLE',
         'ABS_DIFF_GT',1000,'CRITICAL',
         'Loan sanctioned (₹{left}) ≠ finance amount on invoice (₹{right}). Diff = ₹{diff}.',
         NULL, TRUE),

        ('LOAN_VS_LEDGER_CREDIT','PRICE','WITHIN_CASE','["FINANCE"]',
         'bank_approval_letter','loan_amount','SINGLE',
         'customer_ledger','loan_credit','SINGLE',
         'ABS_DIFF_GT',1000,'WARNING',
         'Loan amount on sanction letter (₹{left}) ≠ loan credit posted in ledger (₹{right}).',
         NULL, TRUE),

        ('COST_SHEET_TOTAL_VS_INVOICE','PRICE','WITHIN_CASE','["DELIVERY"]',
         'cost_sheet','total_receivable','SINGLE',
         'tax_invoice_dms','net_payable','SINGLE',
         'ABS_DIFF_GT',1000,'CRITICAL',
         'Cost sheet total (₹{left}) ≠ invoice (₹{right}) — dealer''s own reconciliation disagrees.',
         NULL, TRUE)
    """))

    # Category 2 — Discount Chain (7 rules)
    conn.execute(text("""
        INSERT INTO audit.audit_rules
            (rule_code, category, audit_scope, phases,
             left_doc_type, left_field_key, left_aggregation,
             right_doc_type, right_field_key, right_aggregation,
             right_config_key,
             comparator, threshold, severity, finding_message,
             condition_expression, requires_both_docs)
        VALUES
        ('DISCOUNT_EXCEEDS_POLICY','DISCOUNT','WITHIN_CASE','["BOOKING"]',
         'discount_approval_form','approved_discount','SINGLE',
         NULL, NULL, 'SINGLE', 'config.region_max_discount',
         'GT',0,'CRITICAL',
         'Approved discount (₹{left}) exceeds the regional policy maximum (₹{right}).',
         NULL, FALSE),

        ('DISCOUNT_BOOKING_EXCEEDS_APPROVAL','DISCOUNT','WITHIN_CASE','["BOOKING"]',
         'booking_docket','discount_promised','SINGLE',
         'discount_approval_form','approved_discount','SINGLE', NULL,
         'GT',0,'CRITICAL',
         'Booking promised discount (₹{left}) exceeds approved amount (₹{right}).',
         NULL, TRUE)
    """))

    conn.execute(text("""
        INSERT INTO audit.audit_rules
            (rule_code, category, audit_scope, phases,
             left_doc_type, left_field_key, left_aggregation,
             right_doc_type, right_field_key, right_aggregation,
             comparator, threshold, severity, finding_message,
             condition_expression, requires_both_docs)
        VALUES
        ('DISCOUNT_INVOICE_VS_BOOKING','DISCOUNT','WITHIN_CASE','["BOOKING","DELIVERY"]',
         'tax_invoice_dms','discount_amount','SINGLE',
         'booking_docket','discount_promised','SINGLE',
         'ABS_DIFF_GT',500,'WARNING',
         'Discount applied at invoice (₹{left}) differs from booking promise (₹{right}). Diff = ₹{diff}.',
         NULL, TRUE),

        ('DISCOUNT_INVOICE_DMS_VS_TALLY','DISCOUNT','WITHIN_CASE','["DELIVERY"]',
         'tax_invoice_dms','discount_amount','SINGLE',
         'tax_invoice_tally','discount_amount','SINGLE',
         'ABS_DIFF_GT',500,'CRITICAL',
         'DMS and Tally record different discount amounts (₹{left} vs ₹{right}) — dual books.',
         NULL, TRUE),

        ('DISCOUNT_APPROVAL_MISSING','DISCOUNT','WITHIN_CASE','["BOOKING"]',
         'tax_invoice_dms','discount_amount','SINGLE',
         NULL, NULL, 'SINGLE',
         'GT',0,'CRITICAL',
         'Discount of ₹{left} given on invoice but no discount approval form uploaded.',
         'doc_absent:discount_approval_form', FALSE),

        ('DISCOUNT_APPROVAL_UNSIGNED','DISCOUNT','WITHIN_CASE','["BOOKING"]',
         'discount_approval_form','authoriser_signature','SINGLE',
         NULL, NULL, 'SINGLE',
         'FIELD_EMPTY',0,'WARNING',
         'Discount approval form is present but authoriser signature field is empty.',
         NULL, FALSE),

        ('DISCOUNT_HIDDEN_IN_EXCHANGE','DISCOUNT','WITHIN_CASE','["BOOKING","EXCHANGE"]',
         'tax_invoice_dms','discount_amount','SINGLE',
         'discount_approval_form','approved_discount','SINGLE',
         'CROSS_DOC_SUM_GT',0,'CRITICAL',
         'True discount (invoice discount + exchange overvaluation) exceeds approved limit (₹{right}). Combined = ₹{left}.',
         NULL, TRUE)
    """))

    # Category 3 — Accessory Chain (5 rules)
    conn.execute(text("""
        INSERT INTO audit.audit_rules
            (rule_code, category, audit_scope, phases,
             left_doc_type, left_field_key, left_aggregation,
             right_doc_type, right_field_key, right_aggregation,
             comparator, threshold, severity, finding_message,
             condition_expression, requires_both_docs)
        VALUES
        ('ACCESSORY_DMS_VS_TALLY','ACCESSORY','WITHIN_CASE','["DELIVERY"]',
         'accessory_invoice_dms','total_amount','SINGLE',
         'accessory_invoice_tally','total_amount','SINGLE',
         'ABS_DIFF_GT',500,'CRITICAL',
         'DMS accessory invoice (₹{left}) ≠ Tally accessory invoice (₹{right}). Diff = ₹{diff}.',
         NULL, TRUE),

        ('ACCESSORY_VS_COST_SHEET','ACCESSORY','WITHIN_CASE','["DELIVERY"]',
         'accessory_invoice_dms','total_amount','SINGLE',
         'cost_sheet','accessories_total','SINGLE',
         'ABS_DIFF_GT',500,'WARNING',
         'Accessories invoiced (₹{left}) ≠ accessories on cost sheet (₹{right}). Diff = ₹{diff}.',
         NULL, TRUE),

        ('ACCESSORY_VIN_VS_VEHICLE_VIN','ACCESSORY','WITHIN_CASE','["DELIVERY"]',
         'accessory_invoice_dms','chassis_number','SINGLE',
         'tax_invoice_dms','chassis_number','SINGLE',
         'NOT_EQ',0,'WARNING',
         'VIN on accessory invoice ({left}) ≠ VIN on vehicle invoice ({right}).',
         NULL, TRUE),

        ('ACCESSORY_DATE_VS_DELIVERY','ACCESSORY','WITHIN_CASE','["DELIVERY"]',
         'accessory_invoice_dms','invoice_date','SINGLE',
         'gate_pass','gate_date','SINGLE',
         'DATE_BEFORE',0,'WARNING',
         'Accessory invoice date ({left}) is after the vehicle already left the gate ({right}).',
         NULL, TRUE),

        ('ACCESSORY_BOOKING_VS_INVOICE','ACCESSORY','WITHIN_CASE','["BOOKING","DELIVERY"]',
         'booking_docket','accessories_promised','SINGLE',
         'accessory_invoice_dms','total_amount','SINGLE',
         'ABS_DIFF_GT',2000,'WARNING',
         'Accessories invoiced (₹{right}) significantly exceed booking promise (₹{left}). Diff = ₹{diff}.',
         NULL, TRUE)
    """))

    # Category 4 — Insurance Money Chain (4 rules)
    conn.execute(text("""
        INSERT INTO audit.audit_rules
            (rule_code, category, audit_scope, phases,
             left_doc_type, left_field_key, left_aggregation,
             right_doc_type, right_field_key, right_aggregation,
             comparator, threshold, severity, finding_message,
             condition_expression, requires_both_docs)
        VALUES
        ('INSURANCE_PREMIUM_COVER_VS_COST_SHEET','INSURANCE','WITHIN_CASE','["DELIVERY"]',
         'insurance_cover_note','premium_amount','SINGLE',
         'cost_sheet','insurance_premium','SINGLE',
         'ABS_DIFF_GT',500,'WARNING',
         'Insurance premium on cover note (₹{left}) ≠ amount charged on cost sheet (₹{right}). Diff = ₹{diff}.',
         NULL, TRUE),

        ('INSURANCE_PREMIUM_VS_DEBIT_NOTE','INSURANCE','WITHIN_CASE','["DELIVERY"]',
         'insurance_cover_note','premium_amount','SINGLE',
         'debit_note','insurance_amount','SINGLE',
         'ABS_DIFF_GT',500,'CRITICAL',
         'Debit note for insurance (₹{right}) ≠ actual premium on cover note (₹{left}). Diff = ₹{diff}.',
         NULL, TRUE),

        ('INSURANCE_COVER_NOTE_MISSING','INSURANCE','WITHIN_CASE','["DELIVERY"]',
         NULL, NULL, 'SINGLE',
         NULL, NULL, 'SINGLE',
         'FIELD_EMPTY',0,'CRITICAL',
         'Vehicle has left the gate but no insurance cover note on file.',
         'doc_present:gate_pass AND doc_absent:insurance_cover_note', FALSE),

        ('INSURANCE_START_AFTER_DELIVERY','INSURANCE','WITHIN_CASE','["DELIVERY"]',
         'insurance_cover_note','start_date','SINGLE',
         'gate_pass','gate_date','SINGLE',
         'DATE_BEFORE',0,'WARNING',
         'Insurance cover note start date ({left}) is after the vehicle left the gate ({right}).',
         NULL, TRUE)
    """))

    # Category 5 — RTO / Registration Money Chain (3 rules)
    conn.execute(text("""
        INSERT INTO audit.audit_rules
            (rule_code, category, audit_scope, phases,
             left_doc_type, left_field_key, left_aggregation,
             right_doc_type, right_field_key, right_aggregation,
             comparator, threshold, severity, finding_message,
             condition_expression, requires_both_docs)
        VALUES
        ('RTO_COST_SHEET_VS_CHALLAN','RTO','WITHIN_CASE','["DELIVERY"]',
         'cost_sheet','rto_charges','SINGLE',
         'rto_challan','amount_paid','SINGLE',
         'ABS_DIFF_GT',500,'CRITICAL',
         'RTO charges collected (₹{left}) ≠ amount actually paid to RTO (₹{right}). Diff = ₹{diff}.',
         NULL, TRUE),

        ('RTO_DEBIT_NOTE_VS_CHALLAN','RTO','WITHIN_CASE','["DELIVERY"]',
         'debit_note','rto_amount','SINGLE',
         'rto_challan','amount_paid','SINGLE',
         'ABS_DIFF_GT',500,'WARNING',
         'Debit note for RTO (₹{left}) ≠ RTO challan (₹{right}). Diff = ₹{diff}.',
         NULL, TRUE),

        ('RTO_CHALLAN_MISSING','RTO','WITHIN_CASE','["DELIVERY"]',
         NULL, NULL, 'SINGLE',
         NULL, NULL, 'SINGLE',
         'FIELD_EMPTY',0,'WARNING',
         'RC exists but no RTO challan uploaded.',
         'doc_present:registration_certificate AND doc_absent:rto_challan', FALSE)
    """))

    # Category 6 — Vehicle Identity Chain (7 rules)
    conn.execute(text("""
        INSERT INTO audit.audit_rules
            (rule_code, category, audit_scope, phases,
             left_doc_type, left_field_key, left_aggregation,
             right_doc_type, right_field_key, right_aggregation,
             comparator, threshold, severity, finding_message,
             condition_expression, requires_both_docs)
        VALUES
        ('CHASSIS_BOOKING_VS_INVOICE','VEHICLE','WITHIN_CASE','["BOOKING","DELIVERY"]',
         'booking_docket','chassis_number','SINGLE',
         'tax_invoice_dms','chassis_number','SINGLE',
         'NOT_EQ',0,'CRITICAL',
         'VIN on booking ({left}) ≠ VIN on invoice ({right}) — different vehicle delivered.',
         NULL, TRUE),

        ('CHASSIS_INVOICE_VS_RC','VEHICLE','WITHIN_CASE','["DELIVERY"]',
         'tax_invoice_dms','chassis_number','SINGLE',
         'registration_certificate','chassis_number','SINGLE',
         'NOT_EQ',0,'CRITICAL',
         'VIN on invoice ({left}) ≠ VIN on RC ({right}) — registered a different vehicle.',
         NULL, TRUE),

        ('CHASSIS_RC_VS_INSURANCE','VEHICLE','WITHIN_CASE','["DELIVERY"]',
         'registration_certificate','chassis_number','SINGLE',
         'insurance_cover_note','chassis_number','SINGLE',
         'NOT_EQ',0,'WARNING',
         'VIN on RC ({left}) ≠ VIN on insurance ({right}).',
         NULL, TRUE),

        ('CHASSIS_INVOICE_VS_GATE','VEHICLE','WITHIN_CASE','["DELIVERY"]',
         'tax_invoice_dms','chassis_number','SINGLE',
         'gate_pass','chassis_number','SINGLE',
         'NOT_EQ',0,'CRITICAL',
         'VIN on invoice ({left}) ≠ VIN on gate pass ({right}) — a different vehicle physically left the premises.',
         NULL, TRUE),

        ('CHASSIS_INVOICE_VS_DO','VEHICLE','WITHIN_CASE','["DELIVERY"]',
         'tax_invoice_dms','chassis_number','SINGLE',
         'delivery_order','chassis_number','SINGLE',
         'NOT_EQ',0,'WARNING',
         'VIN on invoice ({left}) ≠ VIN on delivery order ({right}).',
         NULL, TRUE),

        ('MODEL_VARIANT_BOOKING_VS_INVOICE','VEHICLE','WITHIN_CASE','["BOOKING","DELIVERY"]',
         'booking_docket','model_variant','SINGLE',
         'tax_invoice_dms','model_variant','SINGLE',
         'NOT_EQ',0,'WARNING',
         'Model/variant promised at booking ({left}) ≠ model/variant on invoice ({right}).',
         NULL, TRUE),

        ('CUSTOMER_NAME_BOOKING_VS_INVOICE','VEHICLE','WITHIN_CASE','["BOOKING","DELIVERY"]',
         'booking_docket','customer_name','SINGLE',
         'tax_invoice_dms','customer_name','SINGLE',
         'NOT_EQ',0,'WARNING',
         'Customer name on booking ({left}) ≠ customer name on invoice ({right}).',
         NULL, TRUE)
    """))

    # Category 7 — Date / Timeline Chain (8 rules)
    conn.execute(text("""
        INSERT INTO audit.audit_rules
            (rule_code, category, audit_scope, phases,
             left_doc_type, left_field_key, left_aggregation,
             right_doc_type, right_field_key, right_aggregation,
             comparator, threshold, severity, finding_message,
             condition_expression, requires_both_docs)
        VALUES
        ('PAYMENT_DATE_BEFORE_BOOKING','DATE','WITHIN_CASE','["BOOKING"]',
         'payment_receipt_tally','payment_date','MIN',
         'booking_docket','booking_date','SINGLE',
         'DATE_BEFORE',0,'WARNING',
         'First payment receipt ({left}) is dated before the booking was made ({right}).',
         NULL, TRUE),

        ('INVOICE_DATE_BEFORE_BOOKING','DATE','WITHIN_CASE','["BOOKING"]',
         'tax_invoice_dms','invoice_date','SINGLE',
         'booking_docket','booking_date','SINGLE',
         'DATE_BEFORE',0,'WARNING',
         'Invoice raised ({left}) before booking was made ({right}).',
         NULL, TRUE),

        ('GATE_DATE_BEFORE_INVOICE','DATE','WITHIN_CASE','["DELIVERY"]',
         'gate_pass','gate_date','SINGLE',
         'tax_invoice_dms','invoice_date','SINGLE',
         'DATE_BEFORE',0,'CRITICAL',
         'Vehicle left the gate ({left}) before the invoice was raised ({right}).',
         NULL, TRUE),

        ('NDC_DATE_AFTER_GATE','DATE','WITHIN_CASE','["DELIVERY"]',
         'ndc','ndc_date','SINGLE',
         'gate_pass','gate_date','SINGLE',
         'DATE_DIFF_GT',0,'WARNING',
         'No Dues Certificate signed ({left}) after vehicle already left ({right}).',
         NULL, TRUE),

        ('DO_DATE_AFTER_GATE','DATE','WITHIN_CASE','["DELIVERY"]',
         'delivery_order','do_date','SINGLE',
         'gate_pass','gate_date','SINGLE',
         'DATE_DIFF_GT',0,'WARNING',
         'Delivery Order date ({left}) is after gate pass date ({right}).',
         NULL, TRUE),

        ('RC_DELAY_EXCESSIVE','DATE','WITHIN_CASE','["DELIVERY"]',
         'registration_certificate','issue_date','SINGLE',
         'gate_pass','gate_date','SINGLE',
         'DATE_DIFF_GT',45,'WARNING',
         'RC issued ({left}) more than 45 days after delivery ({right}). Delay = {diff} days.',
         NULL, TRUE),

        ('BOOKING_TO_DELIVERY_EXCESS','DATE','WITHIN_CASE','["BOOKING","DELIVERY"]',
         'gate_pass','gate_date','SINGLE',
         'booking_docket','booking_date','SINGLE',
         'DATE_DIFF_GT',180,'INFO',
         'Vehicle delivered ({left}) more than 180 days after booking ({right}). Gap = {diff} days.',
         NULL, TRUE),

        ('DMS_DELIVERY_DATE_VS_GATE','DATE','WITHIN_CASE','["DELIVERY"]',
         'tax_invoice_dms','delivery_date','SINGLE',
         'gate_pass','gate_date','SINGLE',
         'DATE_DIFF_GT',1,'WARNING',
         'DMS records delivery on {left} but gate pass is dated {right}. Difference = {diff} days.',
         NULL, TRUE)
    """))

    # Category 8 — Exchange / Trade-in Chain (5 rules)
    conn.execute(text("""
        INSERT INTO audit.audit_rules
            (rule_code, category, audit_scope, phases,
             left_doc_type, left_field_key, left_aggregation,
             right_doc_type, right_field_key, right_aggregation,
             right_config_key,
             comparator, threshold, severity, finding_message,
             condition_expression, requires_both_docs)
        VALUES
        ('EXCHANGE_VALUE_BELOW_MARKET','EXCHANGE','WITHIN_CASE','["EXCHANGE"]',
         'trade_in_valuation','assessed_value','SINGLE',
         NULL, NULL, 'SINGLE', 'config.market_floor_ratio',
         'RATIO_LT',0.85,'CRITICAL',
         'Exchange value (₹{left}) assessed at less than 85% of market benchmark.',
         'doc_present:trade_in_valuation', FALSE),

        ('EXCHANGE_VALUE_VS_INVOICE_CREDIT','EXCHANGE','WITHIN_CASE','["DELIVERY","EXCHANGE"]',
         'trade_in_valuation','assessed_value','SINGLE',
         'tax_invoice_dms','exchange_credit','SINGLE', NULL,
         'ABS_DIFF_GT',1000,'CRITICAL',
         'Exchange value in valuation (₹{left}) ≠ exchange credit on invoice (₹{right}). Diff = ₹{diff}.',
         NULL, TRUE),

        ('EXCHANGE_VALUE_VS_COST_SHEET','EXCHANGE','WITHIN_CASE','["DELIVERY","EXCHANGE"]',
         'trade_in_valuation','assessed_value','SINGLE',
         'cost_sheet','exchange_credit','SINGLE', NULL,
         'ABS_DIFF_GT',1000,'WARNING',
         'Exchange value in valuation (₹{left}) ≠ exchange credit on cost sheet (₹{right}). Diff = ₹{diff}.',
         NULL, TRUE),

        ('EXCHANGE_RC_OWNER_VS_KYC','EXCHANGE','WITHIN_CASE','["BOOKING","EXCHANGE"]',
         'trade_in_rc','owner_name','SINGLE',
         'kyc_aadhaar','name','SINGLE', NULL,
         'NOT_EQ',0,'WARNING',
         'Owner name on trade-in RC ({left}) ≠ buyer KYC name ({right}).',
         NULL, TRUE),

        ('EXCHANGE_WITHOUT_RC','EXCHANGE','WITHIN_CASE','["BOOKING","EXCHANGE"]',
         NULL, NULL, 'SINGLE',
         NULL, NULL, 'SINGLE', NULL,
         'FIELD_EMPTY',0,'WARNING',
         'Exchange valuation exists but no RC for the traded-in vehicle.',
         'doc_present:trade_in_valuation AND doc_absent:trade_in_rc', FALSE)
    """))

    # Category 9 — KYC / Identity Compliance (7 rules)
    conn.execute(text("""
        INSERT INTO audit.audit_rules
            (rule_code, category, audit_scope, phases,
             left_doc_type, left_field_key, left_aggregation,
             right_doc_type, right_field_key, right_aggregation,
             comparator, threshold, severity, finding_message,
             condition_expression, requires_both_docs)
        VALUES
        ('KYC_NAME_VS_BOOKING','KYC','WITHIN_CASE','["BOOKING"]',
         'kyc_aadhaar','name','SINGLE',
         'booking_docket','customer_name','SINGLE',
         'NOT_EQ',0,'WARNING',
         'Aadhaar name ({left}) ≠ booking name ({right}).',
         NULL, TRUE),

        ('KYC_NAME_VS_INVOICE','KYC','WITHIN_CASE','["DELIVERY"]',
         'kyc_aadhaar','name','SINGLE',
         'tax_invoice_dms','customer_name','SINGLE',
         'NOT_EQ',0,'WARNING',
         'Aadhaar name ({left}) ≠ invoice name ({right}).',
         NULL, TRUE),

        ('KYC_NAME_VS_RC','KYC','WITHIN_CASE','["DELIVERY"]',
         'kyc_aadhaar','name','SINGLE',
         'registration_certificate','owner_name','SINGLE',
         'NOT_EQ',0,'WARNING',
         'KYC name ({left}) ≠ RC owner ({right}) — vehicle registered in a different name.',
         NULL, TRUE),

        ('KYC_DOB_AADHAAR_VS_PAN','KYC','WITHIN_CASE','["BOOKING"]',
         'kyc_aadhaar','date_of_birth','SINGLE',
         'kyc_pan','date_of_birth','SINGLE',
         'NOT_EQ',0,'WARNING',
         'DOB on Aadhaar ({left}) ≠ DOB on PAN ({right}).',
         NULL, TRUE),

        ('KYC_AADHAAR_EXPIRED','KYC','WITHIN_CASE','["BOOKING"]',
         'kyc_aadhaar','expiry_date','SINGLE',
         'booking_docket','booking_date','SINGLE',
         'DATE_BEFORE',0,'WARNING',
         'Aadhaar presented (expiry {left}) was expired at the time of booking ({right}).',
         NULL, TRUE),

        ('LOAN_APPLICANT_VS_KYC','KYC','WITHIN_CASE','["FINANCE"]',
         'bank_approval_letter','applicant_name','SINGLE',
         'kyc_aadhaar','name','SINGLE',
         'NOT_EQ',0,'CRITICAL',
         'Loan applicant name ({left}) ≠ customer KYC ({right}).',
         NULL, TRUE),

        ('DELIVERY_KYC_VS_BOOKING_KYC','KYC','WITHIN_CASE','["DELIVERY"]',
         'customer_id_delivery','id_number','SINGLE',
         'kyc_aadhaar','id_number','SINGLE',
         'NOT_EQ',0,'CRITICAL',
         'ID number on delivery-stage KYC ({left}) ≠ booking-stage Aadhaar ({right}).',
         NULL, TRUE)
    """))

    # Category 10 — Third-Party Payment (3 rules)
    conn.execute(text("""
        INSERT INTO audit.audit_rules
            (rule_code, category, audit_scope, phases,
             left_doc_type, left_field_key, left_aggregation,
             right_doc_type, right_field_key, right_aggregation,
             comparator, threshold, severity, finding_message,
             condition_expression, requires_both_docs)
        VALUES
        ('THIRD_PARTY_DECLARATION_MISSING','THIRD_PARTY','WITHIN_CASE','["BOOKING","DELIVERY"]',
         'payment_receipt_tally','payer_name','SINGLE',
         'kyc_aadhaar','name','SINGLE',
         'NOT_EQ',0,'CRITICAL',
         'Payer name on receipt ({left}) ≠ customer name ({right}) but no third-party declaration uploaded.',
         'doc_absent:third_party_declaration', TRUE),

        ('THIRD_PARTY_AMOUNT_VS_RECEIPT','THIRD_PARTY','WITHIN_CASE','["BOOKING","DELIVERY"]',
         'third_party_declaration','declared_amount','SINGLE',
         'payment_receipt_tally','amount','SINGLE',
         'ABS_DIFF_GT',1000,'WARNING',
         'Declared third-party amount (₹{left}) ≠ actual receipt amount (₹{right}). Diff = ₹{diff}.',
         NULL, TRUE),

        ('CASH_ABOVE_2_LAKH','THIRD_PARTY','WITHIN_CASE','["BOOKING","DELIVERY"]',
         'payment_receipt_tally','amount','MAX',
         NULL, NULL, 'SINGLE',
         'GT',0,'CRITICAL',
         'Single cash payment of ₹{left} exceeds ₹2,00,000 — Section 269ST violation.',
         NULL, FALSE)
    """))

    # Category 11 — Corporate Customer Compliance (4 rules)
    conn.execute(text("""
        INSERT INTO audit.audit_rules
            (rule_code, category, audit_scope, phases,
             left_doc_type, left_field_key, left_aggregation,
             right_doc_type, right_field_key, right_aggregation,
             comparator, threshold, severity, finding_message,
             condition_expression, requires_both_docs)
        VALUES
        ('CORPORATE_GSTIN_CERT_VS_INVOICE','CORPORATE','WITHIN_CASE','["DELIVERY","CORPORATE"]',
         'gst_certificate','gstin','SINGLE',
         'tax_invoice_dms','buyer_gstin','SINGLE',
         'NOT_EQ',0,'CRITICAL',
         'GSTIN on GST certificate ({left}) ≠ GSTIN on invoice ({right}).',
         NULL, TRUE),

        ('CORPORATE_NAME_CERT_VS_INVOICE','CORPORATE','WITHIN_CASE','["BOOKING","DELIVERY","CORPORATE"]',
         'gst_certificate','company_name','SINGLE',
         'tax_invoice_dms','customer_name','SINGLE',
         'NOT_EQ',0,'WARNING',
         'Company name on GST certificate ({left}) ≠ company name on invoice ({right}).',
         NULL, TRUE),

        ('CORPORATE_PO_MISSING','CORPORATE','WITHIN_CASE','["BOOKING","CORPORATE"]',
         NULL, NULL, 'SINGLE',
         NULL, NULL, 'SINGLE',
         'FIELD_EMPTY',0,'WARNING',
         'Corporate customer but no Purchase Order on file.',
         'doc_present:gst_certificate AND doc_absent:purchase_order', FALSE),

        ('CORPORATE_PO_AMOUNT_VS_INVOICE','CORPORATE','WITHIN_CASE','["DELIVERY","CORPORATE"]',
         'purchase_order','po_amount','SINGLE',
         'tax_invoice_dms','net_payable','SINGLE',
         'LT',0,'WARNING',
         'Purchase Order amount (₹{left}) is less than the invoice amount (₹{right}).',
         NULL, TRUE)
    """))

    # Category 12 — NDC and Delivery Gating (2 rules)
    conn.execute(text("""
        INSERT INTO audit.audit_rules
            (rule_code, category, audit_scope, phases,
             left_doc_type, left_field_key, left_aggregation,
             right_doc_type, right_field_key, right_aggregation,
             comparator, threshold, severity, finding_message,
             condition_expression, requires_both_docs)
        VALUES
        ('NDC_MISSING','NDC','WITHIN_CASE','["DELIVERY"]',
         NULL, NULL, 'SINGLE',
         NULL, NULL, 'SINGLE',
         'FIELD_EMPTY',0,'CRITICAL',
         'Vehicle delivered but no No Dues Certificate on file.',
         'doc_present:gate_pass AND doc_absent:ndc', FALSE),

        ('NDC_CUSTOMER_VS_KYC','NDC','WITHIN_CASE','["DELIVERY"]',
         'ndc','customer_name','SINGLE',
         'kyc_aadhaar','name','SINGLE',
         'NOT_EQ',0,'WARNING',
         'Name on NDC ({left}) ≠ KYC name ({right}).',
         NULL, TRUE)
    """))

    # Category 13 — Document Completeness (5 rules)
    conn.execute(text("""
        INSERT INTO audit.audit_rules
            (rule_code, category, audit_scope, phases,
             left_doc_type, left_field_key, left_aggregation,
             right_doc_type, right_field_key, right_aggregation,
             comparator, threshold, severity, finding_message,
             condition_expression, requires_both_docs)
        VALUES
        ('BOOKING_DOCKET_MISSING','COMPLETENESS','WITHIN_CASE','["BOOKING"]',
         NULL, NULL, 'SINGLE',
         NULL, NULL, 'SINGLE',
         'FIELD_EMPTY',0,'CRITICAL',
         'No booking contract on file for this subject.',
         'doc_absent:booking_docket', FALSE),

        ('INVOICE_MISSING','COMPLETENESS','WITHIN_CASE','["DELIVERY"]',
         NULL, NULL, 'SINGLE',
         NULL, NULL, 'SINGLE',
         'FIELD_EMPTY',0,'CRITICAL',
         'Vehicle left the premises but no invoice was raised.',
         'doc_present:gate_pass AND doc_absent:tax_invoice_dms', FALSE),

        ('GATE_PASS_MISSING','COMPLETENESS','WITHIN_CASE','["DELIVERY"]',
         NULL, NULL, 'SINGLE',
         NULL, NULL, 'SINGLE',
         'FIELD_EMPTY',0,'WARNING',
         'Vehicle appears to be registered but no gate pass on file.',
         'doc_present:registration_certificate AND doc_absent:gate_pass', FALSE),

        ('CUSTOMER_LEDGER_MISSING','COMPLETENESS','WITHIN_CASE','["DELIVERY","FINANCE"]',
         NULL, NULL, 'SINGLE',
         NULL, NULL, 'SINGLE',
         'FIELD_EMPTY',0,'WARNING',
         'No ledger maintained for this sale.',
         'doc_present:tax_invoice_dms AND doc_absent:customer_ledger', FALSE),

        ('COST_SHEET_MISSING','COMPLETENESS','WITHIN_CASE','["DELIVERY"]',
         NULL, NULL, 'SINGLE',
         NULL, NULL, 'SINGLE',
         'FIELD_EMPTY',0,'WARNING',
         'No cost sheet on file — deal economics cannot be verified.',
         'doc_absent:cost_sheet', FALSE)
    """))

    # Category 14 — Process / Internal Controls (6 rules)
    conn.execute(text("""
        INSERT INTO audit.audit_rules
            (rule_code, category, audit_scope, phases,
             left_doc_type, left_field_key, left_aggregation,
             right_doc_type, right_field_key, right_aggregation,
             comparator, threshold, severity, finding_message,
             condition_expression, requires_both_docs)
        VALUES
        ('GATE_PASS_CHASSIS_EMPTY','PROCESS','WITHIN_CASE','["DELIVERY"]',
         'gate_pass','chassis_number','SINGLE',
         NULL, NULL, 'SINGLE',
         'FIELD_EMPTY',0,'WARNING',
         'Gate pass exists but chassis number field is blank.',
         NULL, FALSE),

        ('BOOKING_AMOUNT_ZERO','PROCESS','WITHIN_CASE','["BOOKING"]',
         'booking_docket','booking_amount_paid','SINGLE',
         NULL, NULL, 'SINGLE',
         'EQ',0,'CRITICAL',
         'Booking docket shows ₹0 as booking amount paid.',
         NULL, FALSE),

        ('INVOICE_GST_ARITHMETIC','PROCESS','WITHIN_CASE','["DELIVERY"]',
         'tax_invoice_dms','gst_amount','SINGLE',
         'tax_invoice_dms','taxable_amount','SINGLE',
         'ABS_DIFF_GT',100,'WARNING',
         'GST amount on invoice (₹{left}) ≠ taxable amount × GST rate. Expected ≈ ₹{right}.',
         NULL, FALSE),

        ('DUPLICATE_RECEIPT_NUMBER','PROCESS','WITHIN_CASE','["BOOKING","DELIVERY"]',
         'payment_receipt_tally','receipt_number','COUNT',
         NULL, NULL, 'SINGLE',
         'GT',1,'WARNING',
         'Two or more receipts within this case have the same receipt number.',
         NULL, FALSE),

        ('DELIVERY_ORDER_MISSING','PROCESS','WITHIN_CASE','["DELIVERY"]',
         NULL, NULL, 'SINGLE',
         NULL, NULL, 'SINGLE',
         'FIELD_EMPTY',0,'WARNING',
         'Vehicle left the gate but no Delivery Order on file.',
         'doc_present:gate_pass AND doc_absent:delivery_order', FALSE),

        ('PAYMENT_MODE_CASH_UNRECEIPTED','PROCESS','WITHIN_CASE','["BOOKING","DELIVERY"]',
         'customer_ledger','cash_credit_total','SINGLE',
         'payment_receipt_tally','amount','SUM',
         'ABS_DIFF_GT',1000,'CRITICAL',
         'Cash credits in ledger (₹{left}) exceed sum of cash receipts (₹{right}). Diff = ₹{diff}.',
         NULL, TRUE)
    """))

    # Category 15 — Tally vs DMS Cross-System (2 rules)
    conn.execute(text("""
        INSERT INTO audit.audit_rules
            (rule_code, category, audit_scope, phases,
             left_doc_type, left_field_key, left_aggregation,
             right_doc_type, right_field_key, right_aggregation,
             comparator, threshold, severity, finding_message,
             condition_expression, requires_both_docs)
        VALUES
        ('TALLY_VEHICLE_INVOICE_VS_DMS','TALLY_DMS','WITHIN_CASE','["DELIVERY"]',
         'tax_invoice_tally','net_payable','SINGLE',
         'tax_invoice_dms','net_payable','SINGLE',
         'ABS_DIFF_GT',1000,'CRITICAL',
         'Vehicle invoice in Tally (₹{left}) ≠ vehicle invoice in DMS (₹{right}) — dual books.',
         NULL, TRUE),

        ('TALLY_ACCESSORY_INVOICE_VS_DMS','TALLY_DMS','WITHIN_CASE','["DELIVERY"]',
         'accessory_invoice_tally','total_amount','SINGLE',
         'accessory_invoice_dms','total_amount','SINGLE',
         'ABS_DIFF_GT',500,'CRITICAL',
         'Accessory invoice in Tally (₹{left}) ≠ accessory invoice in DMS (₹{right}).',
         NULL, TRUE)
    """))

    # Category 16 — Debit Notes (2 rules)
    conn.execute(text("""
        INSERT INTO audit.audit_rules
            (rule_code, category, audit_scope, phases,
             left_doc_type, left_field_key, left_aggregation,
             right_doc_type, right_field_key, right_aggregation,
             comparator, threshold, severity, finding_message,
             condition_expression, requires_both_docs)
        VALUES
        ('DEBIT_NOTE_INSURANCE_VS_COVER_NOTE','DEBIT_NOTE','WITHIN_CASE','["DELIVERY"]',
         'debit_note','insurance_amount','SINGLE',
         'insurance_cover_note','premium_amount','SINGLE',
         'ABS_DIFF_GT',500,'CRITICAL',
         'Debit note for insurance (₹{left}) ≠ premium on cover note (₹{right}). Diff = ₹{diff}.',
         NULL, TRUE),

        ('DEBIT_NOTE_RTO_VS_CHALLAN','DEBIT_NOTE','WITHIN_CASE','["DELIVERY"]',
         'debit_note','rto_amount','SINGLE',
         'rto_challan','amount_paid','SINGLE',
         'ABS_DIFF_GT',200,'CRITICAL',
         'Debit note for RTO (₹{left}) ≠ RTO challan (₹{right}). Diff = ₹{diff}.',
         NULL, TRUE)
    """))

    # Cross-Case / Duplicate Detection (6 rules — D1–D6, scope=CROSS_CASE)
    conn.execute(text("""
        INSERT INTO audit.audit_rules
            (rule_code, category, audit_scope, phases,
             left_doc_type, left_field_key, left_aggregation,
             right_doc_type, right_field_key, right_aggregation,
             comparator, threshold, severity, finding_message,
             condition_expression, requires_both_docs)
        VALUES
        ('DUPLICATE_PAN_ACROSS_BOOKINGS','CROSS_CASE','CROSS_CASE','["FULL"]',
         'kyc_pan','pan_number','SINGLE',
         NULL, NULL, 'SINGLE',
         'CROSS_DOC_SUM_GT',1,'CRITICAL',
         'Same PAN number ({left}) appears in {diff} active bookings — possible double-booking.',
         NULL, FALSE),

        ('DUPLICATE_AADHAAR_ACROSS_BOOKINGS','CROSS_CASE','CROSS_CASE','["FULL"]',
         'kyc_aadhaar','aadhaar_number','SINGLE',
         NULL, NULL, 'SINGLE',
         'CROSS_DOC_SUM_GT',1,'CRITICAL',
         'Same Aadhaar number ({left}) linked to {diff} active deals.',
         NULL, FALSE),

        ('DUPLICATE_CHASSIS_ACROSS_INVOICES','CROSS_CASE','CROSS_CASE','["FULL"]',
         'tax_invoice_dms','chassis_number','SINGLE',
         NULL, NULL, 'SINGLE',
         'CROSS_DOC_SUM_GT',1,'CRITICAL',
         'Same VIN ({left}) invoiced in {diff} different deals.',
         NULL, FALSE),

        ('DUPLICATE_CHASSIS_ACROSS_GATE_PASSES','CROSS_CASE','CROSS_CASE','["FULL"]',
         'gate_pass','chassis_number','SINGLE',
         NULL, NULL, 'SINGLE',
         'CROSS_DOC_SUM_GT',1,'CRITICAL',
         'Same vehicle ({left}) physically exited the premises in {diff} different deals.',
         NULL, FALSE),

        ('DUPLICATE_RECEIPT_ACROSS_CASES','CROSS_CASE','CROSS_CASE','["FULL"]',
         'payment_receipt_tally','receipt_number','SINGLE',
         NULL, NULL, 'SINGLE',
         'CROSS_DOC_SUM_GT',1,'CRITICAL',
         'Same receipt number ({left}) used in {diff} different deals.',
         NULL, FALSE),

        ('DUPLICATE_UTR_ACROSS_CASES','CROSS_CASE','CROSS_CASE','["FULL"]',
         'payment_receipt_tally','utr_number','SINGLE',
         NULL, NULL, 'SINGLE',
         'CROSS_DOC_SUM_GT',1,'CRITICAL',
         'Same bank transaction UTR ({left}) claimed against {diff} different deals.',
         NULL, FALSE)
    """))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("DROP TABLE IF EXISTS audit.audit_findings"))
    conn.execute(text("DROP TABLE IF EXISTS audit.audit_runs"))
    conn.execute(text("DROP TABLE IF EXISTS audit.audit_rules"))
    conn.execute(text("DROP SCHEMA IF EXISTS audit"))
