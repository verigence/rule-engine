"""comparators.py — 10 pure-Python comparator functions.

Each function returns AuditResult.
None on either operand always returns SKIPPED — never raises.

NOT_EQ normalisation (design doc §8):
  - strip whitespace, uppercase
  - remove common salutation/relationship prefixes (Mr./Mrs./S/O/D/O)
  - exact equality after normalisation (no fuzzy match in Phase 1)
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from verigence.audit.domain.types import AuditResult, Comparator

# ── Helpers ──────────────────────────────────────────────────────────────────

_PREFIX_RE = re.compile(
    r"^(MR\.?|MRS\.?|MS\.?|DR\.?|SHRI|SMT|S/O|D/O|W/O|C/O)\s+",
    re.IGNORECASE,
)


def _to_decimal(val: Any) -> Decimal | None:
    """Cast to Decimal; return None on failure."""
    if val is None:
        return None
    try:
        return Decimal(str(val))
    except (InvalidOperation, TypeError):
        return None


def _to_date(val: Any) -> date | None:
    """Cast to date; return None on failure."""
    if val is None:
        return None
    if isinstance(val, date):
        return val
    try:
        return date.fromisoformat(str(val)[:10])
    except (ValueError, TypeError):
        return None


def _normalise_str(val: Any) -> str | None:
    """Normalise string for NOT_EQ comparison."""
    if val is None:
        return None
    s = str(val).strip().upper()
    s = _PREFIX_RE.sub("", s).strip()
    return s


# ── Comparator functions ──────────────────────────────────────────────────────

def compare_abs_diff_gt(
    left: Any, right: Any, threshold: Any
) -> AuditResult:
    """FAIL if |left − right| > threshold."""
    l, r, t = _to_decimal(left), _to_decimal(right), _to_decimal(threshold)
    if l is None or r is None or t is None:
        return AuditResult.SKIPPED
    return AuditResult.FAIL if abs(l - r) > t else AuditResult.PASS


def compare_not_eq(left: Any, right: Any, threshold: Any) -> AuditResult:  # noqa: ARG001
    """FAIL if normalised(left) != normalised(right)."""
    l, r = _normalise_str(left), _normalise_str(right)
    if l is None or r is None:
        return AuditResult.SKIPPED
    return AuditResult.FAIL if l != r else AuditResult.PASS


def compare_gt(left: Any, right: Any, threshold: Any) -> AuditResult:
    """FAIL if left > right + threshold."""
    l, r, t = _to_decimal(left), _to_decimal(right), _to_decimal(threshold)
    if l is None or t is None:
        return AuditResult.SKIPPED
    # right may be None if coming from config key not resolved; treat as 0
    effective_right = r if r is not None else Decimal(0)
    return AuditResult.FAIL if l > effective_right + t else AuditResult.PASS


def compare_lt(left: Any, right: Any, threshold: Any) -> AuditResult:
    """FAIL if left < right − threshold."""
    l, r, t = _to_decimal(left), _to_decimal(right), _to_decimal(threshold)
    if l is None or r is None or t is None:
        return AuditResult.SKIPPED
    return AuditResult.FAIL if l < r - t else AuditResult.PASS


def compare_eq(left: Any, right: Any, threshold: Any) -> AuditResult:
    """FAIL if |left − right| <= threshold (equality check — e.g. booking_amount == 0)."""
    l, r, t = _to_decimal(left), _to_decimal(right), _to_decimal(threshold)
    if l is None or t is None:
        return AuditResult.SKIPPED
    effective_right = r if r is not None else Decimal(0)
    return AuditResult.FAIL if abs(l - effective_right) <= t else AuditResult.PASS


def compare_date_before(left: Any, right: Any, threshold: Any) -> AuditResult:  # noqa: ARG001
    """FAIL if left_date < right_date."""
    l, r = _to_date(left), _to_date(right)
    if l is None or r is None:
        return AuditResult.SKIPPED
    return AuditResult.FAIL if l < r else AuditResult.PASS


def compare_date_diff_gt(left: Any, right: Any, threshold: Any) -> AuditResult:
    """FAIL if (left_date − right_date).days > threshold."""
    l, r, t = _to_date(left), _to_date(right), _to_decimal(threshold)
    if l is None or r is None or t is None:
        return AuditResult.SKIPPED
    diff_days = (l - r).days
    return AuditResult.FAIL if diff_days > int(t) else AuditResult.PASS


def compare_ratio_lt(left: Any, right: Any, threshold: Any) -> AuditResult:
    """FAIL if left / right < threshold."""
    l, r, t = _to_decimal(left), _to_decimal(right), _to_decimal(threshold)
    if l is None or r is None or t is None:
        return AuditResult.SKIPPED
    if r == 0:
        return AuditResult.SKIPPED
    return AuditResult.FAIL if (l / r) < t else AuditResult.PASS


def compare_field_empty(left: Any, right: Any, threshold: Any) -> AuditResult:  # noqa: ARG001
    """FAIL if left is None or empty string."""
    if left is None or str(left).strip() == "":
        return AuditResult.FAIL
    return AuditResult.PASS


def compare_cross_doc_sum_gt(left: Any, right: Any, threshold: Any) -> AuditResult:
    """FAIL if left > right + threshold. Used for cross-case duplicate counts."""
    # In cross-case context, left = count of subjects; right = expected (usually 1)
    return compare_gt(left, right, threshold)


# ── Dispatch ─────────────────────────────────────────────────────────────────────

_DISPATCH: dict[str, Any] = {
    Comparator.ABS_DIFF_GT:      compare_abs_diff_gt,
    Comparator.NOT_EQ:           compare_not_eq,
    Comparator.GT:               compare_gt,
    Comparator.LT:               compare_lt,
    Comparator.EQ:               compare_eq,
    Comparator.DATE_BEFORE:      compare_date_before,
    Comparator.DATE_DIFF_GT:     compare_date_diff_gt,
    Comparator.RATIO_LT:         compare_ratio_lt,
    Comparator.FIELD_EMPTY:      compare_field_empty,
    Comparator.CROSS_DOC_SUM_GT: compare_cross_doc_sum_gt,
}


def evaluate(
    comparator: str | Comparator,
    left: Any,
    right: Any,
    threshold: Any,
) -> AuditResult:
    """Dispatch comparator by name and return AuditResult. Never raises."""
    fn = _DISPATCH.get(comparator)  # type: ignore[arg-type]
    if fn is None:
        return AuditResult.SKIPPED
    try:
        return fn(left, right, threshold)
    except Exception:  # noqa: BLE001
        return AuditResult.SKIPPED
