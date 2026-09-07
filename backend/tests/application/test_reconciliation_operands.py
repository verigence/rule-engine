"""test_reconciliation_operands.py — Audit Core reconciliation + derived operands.

Covers:
  - context_builder._load_reconciliation (mocked session, no real DB)
  - evaluator._resolve_reconciliation_operand / _resolve_derived_operand (pure)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from verigence.audit.application import context_builder
from verigence.audit.application.evaluator import (
    _resolve_derived_operand,
    _resolve_reconciliation_operand,
    resolve_operand,
)
from verigence.audit.domain.types import AuditContext, DocumentContext

TENANT = "tenant-001"
SUBJECT = uuid4()


def _ctx(*, documents=None, reconciliation=None) -> AuditContext:
    return AuditContext(
        tenant_id=TENANT,
        subject_id=SUBJECT,
        documents=documents or [],
        config={},
        reconciliation=reconciliation or {},
    )


# ── _resolve_reconciliation_operand ──────────────────────────────────────────────

def test_reconciliation_operand_reads_bucket_key_side() -> None:
    ctx = _ctx(reconciliation={
        "discount": {"CORPORATE": {"standard": 30000.0, "actual": 40000.0}},
        "addon": {"EXTENDED_WARRANTY": {"standard": None, "actual": 18000.0}},
    })
    assert _resolve_reconciliation_operand(ctx, "discount:CORPORATE:actual") == 40000.0
    assert _resolve_reconciliation_operand(ctx, "discount:CORPORATE:standard") == 30000.0
    assert _resolve_reconciliation_operand(ctx, "addon:EXTENDED_WARRANTY:standard") is None
    assert _resolve_reconciliation_operand(ctx, "discount:LOYALTY:actual") is None
    assert _resolve_reconciliation_operand(ctx, "bad-format") is None


def test_reconciliation_operand_via_resolve_operand() -> None:
    ctx = _ctx(reconciliation={"commercial": {"ex_showroom_price": {"standard": 900000.0, "actual": 905000.0}}})
    value, source = resolve_operand(
        ctx, "_reconciliation", "commercial:ex_showroom_price:standard", "SINGLE",
    )
    assert value == 900000.0
    assert source is None


# ── _resolve_derived_operand ─────────────────────────────────────────────────────

def test_derived_payments_total_sums_all_payment_evidence() -> None:
    ctx = _ctx(documents=[
        DocumentContext(uuid4(), "dealer_receipt", {"amount_paid": "10000"}),
        DocumentContext(uuid4(), "upi_transaction", {"amount_paid": "25,000"}),
        DocumentContext(uuid4(), "bank_statement_extract", {"credit_amount": "500000"}),
        DocumentContext(uuid4(), "booking_form", {"total_price": "999999"}),  # ignored
    ])
    assert _resolve_derived_operand(ctx, "payments_total") == pytest.approx(535000.0)


def test_derived_payments_total_none_when_no_payment_evidence() -> None:
    ctx = _ctx(documents=[DocumentContext(uuid4(), "booking_form", {"total_price": "1"})])
    assert _resolve_derived_operand(ctx, "payments_total") is None


def test_derived_lineitem_sums_matching_invoice_category() -> None:
    ctx = _ctx(documents=[
        DocumentContext(uuid4(), "customer_invoice_dms", {"line_items": [
            {"description_raw": "Extended Warranty 3yr", "line_category": "EXTENDED_WARRANTY", "net_amount": "18000"},
            {"description_raw": "Floor mats", "line_category": "ACCESSORY_GENUINE", "net_amount": "3000"},
            {"description_raw": "EW top-up", "line_category": "extended_warranty", "net_amount": "2000"},
        ]}),
        DocumentContext(uuid4(), "booking_form", {"line_items": [{"line_category": "EXTENDED_WARRANTY", "net_amount": "99"}]}),
    ])
    assert _resolve_derived_operand(ctx, "lineitem:EXTENDED_WARRANTY") == pytest.approx(20000.0)
    assert _resolve_derived_operand(ctx, "lineitem:ACCESSORY_GENUINE") == pytest.approx(3000.0)
    assert _resolve_derived_operand(ctx, "lineitem:RSA") is None


# ── _journey_id_from_context_ref ─────────────────────────────────────────────────

def test_journey_id_parsed_from_audit_core_context_ref() -> None:
    journey = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    customer = "11111111-2222-3333-4444-555555555555"
    ref = f"audit-{journey}-{customer}"
    assert context_builder._journey_id_from_context_ref(ref) == journey


def test_journey_id_from_context_ref_rejects_non_audit_core_shapes() -> None:
    assert context_builder._journey_id_from_context_ref("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee") is None
    assert context_builder._journey_id_from_context_ref("audit-not-a-uuid-x") is None
    assert context_builder._journey_id_from_context_ref("") is None


# ── _load_reconciliation ─────────────────────────────────────────────────────────

_CONTEXT_REF = (
    "audit-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    "-11111111-2222-3333-4444-555555555555"
)


def _scalar_result(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _rows_result(rows):
    r = MagicMock()
    r.all.return_value = rows
    return r


@pytest.mark.asyncio
async def test_load_reconciliation_builds_all_three_buckets() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[
        _scalar_result(_CONTEXT_REF),                            # audit-storage-context ref
        _rows_result([("ex_showroom_price", 900000, 905000)]),   # commercial_lines
        _rows_result([("CORPORATE", None, 40000)]),              # discount_applications
        _rows_result([("EXTENDED_WARRANTY", None, 18000)]),      # journey_addons
    ])

    result = await context_builder._load_reconciliation(session, TENANT, SUBJECT)

    assert result["commercial"]["ex_showroom_price"] == {"standard": 900000.0, "actual": 905000.0}
    assert result["discount"]["CORPORATE"] == {"standard": None, "actual": 40000.0}
    assert result["addon"]["EXTENDED_WARRANTY"] == {"standard": None, "actual": 18000.0}


@pytest.mark.asyncio
async def test_load_reconciliation_returns_empty_when_no_context_row() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_scalar_result(None))
    assert await context_builder._load_reconciliation(session, TENANT, SUBJECT) == {}


@pytest.mark.asyncio
async def test_load_reconciliation_returns_empty_when_context_ref_unparseable() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_scalar_result("legacy-ref-without-journey"))
    assert await context_builder._load_reconciliation(session, TENANT, SUBJECT) == {}


@pytest.mark.asyncio
async def test_load_reconciliation_degrades_on_error() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=RuntimeError("permission denied for schema auditcore"))
    result = await context_builder._load_reconciliation(session, TENANT, SUBJECT)
    assert result == {}
    session.rollback.assert_awaited()
