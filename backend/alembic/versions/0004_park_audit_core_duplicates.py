"""0004 — park rules with no real path to ever fire.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-12

Two independent reasons, both resulting in "enabled=true but never
actually evaluated" -- surfaced while verigence-audit-core built a unified
rule registry across both systems (auditcore.rule_definitions) and traced
every current call site into this service.

_PARK_DUPLICATE (2 rules) -- duplicates an existing, already-live
audit-core CODE rule that checks the identical real-world condition:

  BOOKING_DOCKET_MISSING   duplicates audit-core's BK_DOCKET_PRESENT
    Both: "is the Booking Form / docket document on file". Audit-core's
    version runs off the tenant's configured journey_document_requirements
    registry (richer -- respects per-project requirement config) and is
    already live in production.

  DISCOUNT_APPROVAL_MISSING   duplicates audit-core's BK_DISCOUNT_EVIDENCE_MISSING
    Both: "a discount was granted but no approval/evidence document is on
    file". Same reasoning -- audit-core's version is the live, configured
    one.

_PARK_UNREACHABLE (9 rules) -- audit-core (the only caller of this
service) never requests a phase or scope these rules require, verified by
reading both sides directly, not assumed:

  audit-core's RuleEngineClient.evaluate_phase() is only ever called with
  phase="BOOKING" or phase="DELIVERY" (2 real call sites in
  uc03_booking_rule_trigger.py / uc03_delivery_commands.py). This
  service's own evaluator.py's load_rules() only returns a rule whose
  `phases` contains the requested phase (or "FULL") -- so a rule tagged
  only ["FINANCE"] or ["EXCHANGE"] never matches either real request:

    LOAN_VS_LEDGER_CREDIT          phases=["FINANCE"]
    EXCHANGE_VALUE_BELOW_MARKET    phases=["EXCHANGE"]
    LOAN_APPLICANT_VS_KYC          phases=["FINANCE"]

  Separately, evaluator.py's run_audit() hardcodes scope="WITHIN_CASE" --
  every CROSS_CASE rule is excluded regardless of its phase tag, and
  audit-core has no cross-case call site at all (no caller of
  /audit/cross-case-scan exists in that codebase):

    DUPLICATE_PAN_ACROSS_BOOKINGS
    DUPLICATE_AADHAAR_ACROSS_BOOKINGS
    DUPLICATE_CHASSIS_ACROSS_INVOICES
    DUPLICATE_CHASSIS_ACROSS_GATE_PASSES
    DUPLICATE_RECEIPT_ACROSS_CASES
    DUPLICATE_UTR_ACROSS_CASES

  Parked rather than wired up: neither side has an existing, low-risk
  trigger point for FINANCE/EXCHANGE phases (audit-core's journey model
  has no such stage today -- stage_code is BOOKING/DELIVERY/POST_DELIVERY
  only) or for a cross-case materialization model (a finding spanning two
  journeys has no existing pattern -- audit_findings.journey_id is a
  required composite FK to exactly one journey). Both would need new
  product/architecture decisions, not just a missing function call.
  Native, audit-core-side duplicate-booking detection (heuristic name/
  address matching, not exact-identifier CROSS_CASE rules) is being built
  instead, independently of these six rows.

Parked here rather than deleted, matching 0002/0003's disposition --
fully reversible (enabled=true) if either side's mechanism changes.
No other rows touched.
"""
from alembic import op
from sqlalchemy import text

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_PARK_DUPLICATE: list[str] = [
    "BOOKING_DOCKET_MISSING",
    "DISCOUNT_APPROVAL_MISSING",
]

_PARK_UNREACHABLE: list[str] = [
    "LOAN_VS_LEDGER_CREDIT",
    "EXCHANGE_VALUE_BELOW_MARKET",
    "LOAN_APPLICANT_VS_KYC",
    "DUPLICATE_PAN_ACROSS_BOOKINGS",
    "DUPLICATE_AADHAAR_ACROSS_BOOKINGS",
    "DUPLICATE_CHASSIS_ACROSS_INVOICES",
    "DUPLICATE_CHASSIS_ACROSS_GATE_PASSES",
    "DUPLICATE_RECEIPT_ACROSS_CASES",
    "DUPLICATE_UTR_ACROSS_CASES",
]

_PARK: list[str] = _PARK_DUPLICATE + _PARK_UNREACHABLE


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text("UPDATE audit.audit_rules SET enabled = false WHERE rule_code = ANY(:codes)"),
        {"codes": _PARK},
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text("UPDATE audit.audit_rules SET enabled = true WHERE rule_code = ANY(:codes)"),
        {"codes": _PARK},
    )
