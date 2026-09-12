"""test_migration_0004_park_unreachable.py — static consistency of the
0004 park list.

The migration runs against a real DB at deploy time (no DB in CI), so this
only checks the park data is internally sound.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_MIG = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0004_park_audit_core_duplicates.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("mig_0004", _MIG)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_park_is_the_union_of_the_two_reasons() -> None:
    m = _load()
    assert set(m._PARK) == set(m._PARK_DUPLICATE) | set(m._PARK_UNREACHABLE)
    assert set(m._PARK_DUPLICATE).isdisjoint(m._PARK_UNREACHABLE)


def test_park_duplicate_is_exactly_the_two_confirmed_rows() -> None:
    m = _load()
    assert set(m._PARK_DUPLICATE) == {"BOOKING_DOCKET_MISSING", "DISCOUNT_APPROVAL_MISSING"}


def test_park_unreachable_is_exactly_the_nine_confirmed_rows() -> None:
    m = _load()
    assert set(m._PARK_UNREACHABLE) == {
        "LOAN_VS_LEDGER_CREDIT",
        "EXCHANGE_VALUE_BELOW_MARKET",
        "LOAN_APPLICANT_VS_KYC",
        "DUPLICATE_PAN_ACROSS_BOOKINGS",
        "DUPLICATE_AADHAAR_ACROSS_BOOKINGS",
        "DUPLICATE_CHASSIS_ACROSS_INVOICES",
        "DUPLICATE_CHASSIS_ACROSS_GATE_PASSES",
        "DUPLICATE_RECEIPT_ACROSS_CASES",
        "DUPLICATE_UTR_ACROSS_CASES",
    }


def test_upgrade_and_downgrade_both_reference_the_shared_park_list() -> None:
    # Both functions read the module-level _PARK name (not a locally
    # redefined literal) -- confirms upgrade/downgrade can't silently drift
    # to different sets over time.
    source = _MIG.read_text()
    upgrade_body = source[source.index("def upgrade"):source.index("def downgrade")]
    downgrade_body = source[source.index("def downgrade"):]
    assert '{"codes": _PARK}' in upgrade_body
    assert '{"codes": _PARK}' in downgrade_body
