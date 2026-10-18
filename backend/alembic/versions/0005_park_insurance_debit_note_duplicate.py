"""0005 — park the duplicate insurance-vs-debit-note rule.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-18

DEBIT_NOTE_INSURANCE_VS_COVER_NOTE and INSURANCE_PREMIUM_VS_DEBIT_NOTE are
the same check. Seeded in 0001 and repointed (not parked) by 0002, both are
enabled today:

  INSURANCE_PREMIUM_VS_DEBIT_NOTE     INSURANCE   left insurance_cover.premium_amount
                                                  right debit_note.insurance_amount
  DEBIT_NOTE_INSURANCE_VS_COVER_NOTE  DEBIT_NOTE  left debit_note.insurance_amount
                                                  right insurance_cover.premium_amount

Same two documents, same comparator ABS_DIFF_GT, same threshold 500, same
severity CRITICAL, same phase DELIVERY, same requires_both_docs. ABS_DIFF_GT
is symmetric, so the reversed operands evaluate identically -- one premium
mismatch produces two CRITICAL findings and, in audit-core, two
RULE_DISCREPANCY_REVIEW tasks for the PC against the same discrepancy.
Audit-core catalogues both (p2_templates/control_templates.yaml), so neither
is filtered out on that side as a "retired" code.

INSURANCE_PREMIUM_VS_DEBIT_NOTE is kept. Its finding message names the
premium as the reference value -- "Debit note for insurance (Rs {right}) !=
actual premium on cover note (Rs {left})" -- which reads the right way round
for the PC: the cover note is the source of truth and the debit note is what
is checked against it. Its INSURANCE category also groups it with the rest
of the insurance money chain (INSURANCE_PREMIUM_COVER_VS_COST_SHEET,
INSURANCE_START_AFTER_DELIVERY).

DEBIT_NOTE_INSURANCE_VS_COVER_NOTE is parked. Its DEBIT_NOTE category is
left holding only DEBIT_NOTE_RTO_VS_CHALLAN, itself already parked by 0002
alongside its own duplicate RTO_DEBIT_NOTE_VS_CHALLAN -- so the DEBIT_NOTE
category ends up empty in practice. That is the correct outcome: the debit
note is an operand in these checks, not a subject of its own.

Parked rather than deleted, matching 0002/0003/0004's disposition -- fully
reversible (enabled=true). No other rows touched.
"""
from alembic import op
from sqlalchemy import text

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_PARK: list[str] = [
    "DEBIT_NOTE_INSURANCE_VS_COVER_NOTE",
]


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
