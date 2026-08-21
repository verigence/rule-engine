"""test_context_builder.py — Unit tests for AuditContextBuilder.

Mocks the DB session — no real DB required.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from verigence.audit.application.context_builder import (
    DEFAULT_CONFIG,
    aggregate_field,
    build_audit_context,
    first_doc_of_type,
)
from verigence.audit.domain.types import DocumentContext

TENANT = "tenant-001"
SUBJECT = uuid4()
DOC_ID_1 = uuid4()
DOC_ID_2 = uuid4()


def _make_row(doc_id: UUID, doc_type: str, fields: dict) -> MagicMock:
    row = MagicMock()
    row.__getitem__ = lambda self, k: {  # type: ignore[misc]
        "document_id": str(doc_id),
        "document_type_key": doc_type,
        "indexed_fields": fields,
    }[k]
    return row


@pytest.fixture()
def mock_di_session() -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.mappings.return_value.all.return_value = [
        _make_row(DOC_ID_1, "booking_docket", {"agreed_price": "500000", "customer_name": "Ramesh Kumar"}),
        _make_row(DOC_ID_2, "tax_invoice_dms",  {"net_payable": "502000", "customer_name": "Ramesh Kumar"}),
    ]
    session.execute = AsyncMock(return_value=result)
    return session


@pytest.mark.asyncio
async def test_build_context_returns_correct_document_count(mock_di_session):
    ctx = await build_audit_context(mock_di_session, TENANT, SUBJECT)
    assert len(ctx.documents) == 2


@pytest.mark.asyncio
async def test_build_context_populates_tenant_and_subject(mock_di_session):
    ctx = await build_audit_context(mock_di_session, TENANT, SUBJECT)
    assert ctx.tenant_id == TENANT
    assert ctx.subject_id == SUBJECT


@pytest.mark.asyncio
async def test_build_context_uses_default_config(mock_di_session):
    ctx = await build_audit_context(mock_di_session, TENANT, SUBJECT)
    assert ctx.config["config.cash_limit"] == DEFAULT_CONFIG["config.cash_limit"]


@pytest.mark.asyncio
async def test_build_context_applies_config_overrides(mock_di_session):
    ctx = await build_audit_context(
        mock_di_session, TENANT, SUBJECT,
        config_overrides={"config.region_max_discount": 50_000},
    )
    assert ctx.config["config.region_max_discount"] == 50_000


# ── aggregate_field ────────────────────────────────────────────────────────────────

def _docs(*specs: tuple[str, str, float]) -> list[DocumentContext]:
    """Build DocumentContext list from (doc_type, field_key, value) tuples."""
    return [
        DocumentContext(
            document_id=uuid4(),
            document_type_key=dt,
            indexed_fields={fk: str(v)},
        )
        for dt, fk, v in specs
    ]


def test_aggregate_single_returns_first_value():
    docs = _docs(("payment_receipt_tally", "amount", 10000))
    result = aggregate_field(docs, "payment_receipt_tally", "amount", "SINGLE")
    assert result == pytest.approx(10000.0)


def test_aggregate_sum_adds_all_receipts():
    docs = _docs(
        ("payment_receipt_tally", "amount", 10000),
        ("payment_receipt_tally", "amount", 25000),
        ("payment_receipt_tally", "amount", 15000),
    )
    assert aggregate_field(docs, "payment_receipt_tally", "amount", "SUM") == pytest.approx(50000.0)


def test_aggregate_max():
    docs = _docs(
        ("payment_receipt_tally", "amount", 10000),
        ("payment_receipt_tally", "amount", 250000),
    )
    assert aggregate_field(docs, "payment_receipt_tally", "amount", "MAX") == pytest.approx(250000.0)


def test_aggregate_min():
    docs = _docs(
        ("payment_receipt_tally", "amount", 10000),
        ("payment_receipt_tally", "amount", 250000),
    )
    assert aggregate_field(docs, "payment_receipt_tally", "amount", "MIN") == pytest.approx(10000.0)


def test_aggregate_count():
    docs = _docs(
        ("payment_receipt_tally", "amount", 10000),
        ("payment_receipt_tally", "amount", 25000),
    )
    assert aggregate_field(docs, "payment_receipt_tally", "amount", "COUNT") == 2


def test_aggregate_returns_none_when_no_docs_of_type():
    docs = _docs(("booking_docket", "agreed_price", 500000))
    result = aggregate_field(docs, "tax_invoice_dms", "net_payable", "SINGLE")
    assert result is None


def test_aggregate_returns_none_when_field_absent():
    docs = _docs(("booking_docket", "agreed_price", 500000))
    result = aggregate_field(docs, "booking_docket", "nonexistent_field", "SINGLE")
    assert result is None


# ── first_doc_of_type ─────────────────────────────────────────────────────────────

def test_first_doc_of_type_found():
    doc = DocumentContext(document_id=uuid4(), document_type_key="gate_pass", indexed_fields={})
    result = first_doc_of_type([doc], "gate_pass")
    assert result is doc


def test_first_doc_of_type_not_found():
    doc = DocumentContext(document_id=uuid4(), document_type_key="booking_docket", indexed_fields={})
    assert first_doc_of_type([doc], "gate_pass") is None
