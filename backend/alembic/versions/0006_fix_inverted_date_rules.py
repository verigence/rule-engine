"""0006 — two date rules fired the wrong way round.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-14

Product owner 2026-11-14 ("PC closes the task but it is not getting closed").

  INSURANCE_START_AFTER_DELIVERY  left insurance_cover.policy_start_date
                                  right gate_pass.delivery_date
                                  "Insurance cover note start date ({left}) is
                                   after the vehicle left the gate ({right})."
  ACCESSORY_DATE_VS_DELIVERY      left accessory_invoice_dms.invoice_date
                                  right gate_pass.delivery_date
                                  "Accessory invoice date ({left}) is after the
                                   vehicle already left the gate ({right})."

Both used DATE_BEFORE, which fails when left is BEFORE right: an insurance
cover starting the day before delivery (the normal case) and an accessory
invoice raised before delivery (the normal case) were findings, while the
case the message describes passed. Live UTK-SR-261104-004: cover from
2026-11-02, gate pass 2026-11-03 -> "start date (2026-11-02) is after the
vehicle left the gate (2026-11-03)".

DATE_DIFF_GT with threshold 0 fails when left is after right by more than 0
days -- exactly what the messages say (DO_DATE_AFTER_GATE already uses it).
Operands, messages and severities are unchanged.
"""
from alembic import op
from sqlalchemy import text

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_CODES: list[str] = ["INSURANCE_START_AFTER_DELIVERY", "ACCESSORY_DATE_VS_DELIVERY"]


def upgrade() -> None:
    op.get_bind().execute(
        text("UPDATE audit.audit_rules SET comparator = 'DATE_DIFF_GT', threshold = 0 "
             "WHERE rule_code = ANY(:codes) AND comparator = 'DATE_BEFORE'"),
        {"codes": _CODES},
    )


def downgrade() -> None:
    op.get_bind().execute(
        text("UPDATE audit.audit_rules SET comparator = 'DATE_BEFORE', threshold = 0 "
             "WHERE rule_code = ANY(:codes) AND comparator = 'DATE_DIFF_GT'"),
        {"codes": _CODES},
    )
