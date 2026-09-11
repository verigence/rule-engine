"""test_migration_0003_split_payment_sum.py — static consistency of the
PAYMENT_SUM_VS_INVOICE split.

The migration runs against a real DB at deploy time (no DB in CI), so this
only checks the new rule data is internally sound.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_MIG = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0003_split_payment_sum_by_receipt_type.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("mig_0003", _MIG)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parked_rule_is_not_one_of_the_new_rules() -> None:
    m = _load()
    assert set(m._PARK).isdisjoint(set(m._NEW_RULES))
    assert m._PARK == ["PAYMENT_SUM_VS_INVOICE"]


def test_new_rules_are_same_type_only_no_derived_sentinel() -> None:
    m = _load()
    for code, (
        _category, _phases, ld, _lf, la, rd, _rf, ra, _cmp, _thr, _sev, _msg,
    ) in m._NEW_RULES.items():
        # Neither side is the shared "_derived"/"_reconciliation" sentinel --
        # these are plain per-document-type aggregations, never blended.
        assert ld not in ("_derived", "_reconciliation"), code
        assert rd not in ("_derived", "_reconciliation"), code
        assert la == "SUM", (code, "left side must be a same-type SUM")
        assert ra == "SINGLE", (code, "right side must be a single invoice/form value")


def test_booking_and_delivery_receipts_are_never_compared_to_each_other() -> None:
    m = _load()
    left_doc_types = {ld for _c, (_cat, _ph, ld, *_rest) in m._NEW_RULES.items()}
    # dealer_receipt (Booking) and payment_receipt (Delivery) each anchor
    # exactly one rule -- never appear together as left+right of one rule.
    assert left_doc_types == {"dealer_receipt", "payment_receipt"}
    for code, (_cat, _ph, ld, _lf, _la, rd, _rf, _ra, *_rest) in m._NEW_RULES.items():
        assert rd not in ("dealer_receipt", "payment_receipt"), code
        assert ld != rd, code


def test_rules_carry_the_expected_phase_tags() -> None:
    m = _load()
    assert m._NEW_RULES["BOOKING_RECEIPT_SUM_VS_BOOKING_FORM"][1] == ["BOOKING"]
    assert m._NEW_RULES["DELIVERY_RECEIPT_SUM_VS_INVOICE"][1] == ["DELIVERY", "FINANCE"]


def test_finding_messages_carry_left_right_diff_placeholders() -> None:
    m = _load()
    for code, values in m._NEW_RULES.items():
        message = values[-1]
        assert "{left}" in message, code
        assert "{right}" in message, code
        assert "{diff}" in message, code
