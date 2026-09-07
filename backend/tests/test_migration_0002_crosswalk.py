"""test_migration_0002_crosswalk.py — static consistency of the operand re-point.

The migration runs against a real DB at deploy time (no DB in CI), so this only
checks the crosswalk data is internally sound.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_MIG = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0002_reconcile_rule_operands.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("mig_0002", _MIG)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_buckets_are_pairwise_disjoint() -> None:
    m = _load()
    remap = set(m._REMAP)
    sentinel = set(m._SENTINEL)
    park = set(m._PARK)
    assert remap.isdisjoint(sentinel)
    assert remap.isdisjoint(park)
    assert sentinel.isdisjoint(park)


def test_sentinels_use_known_prefixes() -> None:
    m = _load()
    for code, (ld, lf, rd, rf) in m._SENTINEL.items():
        for doc, field in ((ld, lf), (rd, rf)):
            if doc in ("_reconciliation", "_derived"):
                assert field, code
            if doc == "_reconciliation":
                assert field.count(":") == 2, (code, field)
                bucket, _key, side = field.split(":")
                assert bucket in ("commercial", "discount", "addon"), (code, field)
                assert side in ("standard", "actual"), (code, field)
            if doc == "_derived":
                assert field == "payments_total" or field.startswith("lineitem:"), (code, field)


def test_park_covers_the_no_source_rules() -> None:
    m = _load()
    park = set(m._PARK)
    # a few that definitively have no DI source today
    for code in (
        "CHASSIS_INVOICE_VS_RC",
        "RC_DELAY_EXCESSIVE",
        "NDC_MISSING",
        "KYC_AADHAAR_EXPIRED",
        "CASH_ABOVE_2_LAKH",
        "COST_SHEET_MISSING",
    ):
        assert code in park


def test_no_remap_still_references_a_legacy_doc_type() -> None:
    m = _load()
    legacy = {
        "tax_invoice_dms", "booking_docket", "kyc_pan", "kyc_aadhaar",
        "insurance_cover_note", "trade_in_valuation", "delivery_order",
        "payment_receipt_tally", "cost_sheet", "discount_approval_form",
        "registration_certificate", "trade_in_rc", "ndc",
    }
    for code, (ld, _lf, rd, _rf) in {**m._REMAP, **m._SENTINEL}.items():
        assert ld not in legacy, (code, ld)
        assert rd not in legacy, (code, rd)
