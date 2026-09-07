"""context_builder.py — Build AuditContext from docintel.document_search_index.

Pattern mirrors verigence-di/application/reconciliation.py exactly:
  - reads indexed_fields as a plain dict[str, Any]
  - _to_float / _to_date / _to_str helpers only
  - absent key → None → comparator returns SKIPPED
  - no canonical_fields queries, no schema registry lookups
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from verigence.audit.domain.types import AuditContext, DocumentContext

logger = structlog.get_logger(__name__)

# ── Type-cast helpers (copy of reconciliation.py pattern) ────────────────────────

def _to_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(str(val).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _to_date(val: Any) -> date | None:
    if val is None:
        return None
    if isinstance(val, date):
        return val
    try:
        return datetime.fromisoformat(str(val)[:10]).date()
    except (ValueError, TypeError):
        return None


def _to_str(val: Any) -> str | None:
    return str(val).strip() if val is not None else None


# ── Default config constants (design doc §16) ────────────────────────────────────

DEFAULT_CONFIG: dict[str, Any] = {
    "config.region_max_discount":          0,        # always flag if set to 0
    "config.cash_limit":                   200_000,  # Section 269ST — statutory
    "config.market_floor_ratio":           0.85,
    "config.min_booking_amount":           5_000,
    "config.max_rc_delay_days":            45,
    "config.max_booking_to_delivery_days": 180,
    "config.min_document_date":            "2020-01-01",
}


# ── Aggregation over multiple documents ──────────────────────────────────────────

def aggregate_field(
    documents: list[DocumentContext],
    doc_type_key: str,
    field_key: str,
    aggregation: str,  # SINGLE | SUM | MAX | MIN | COUNT
    *,
    as_date: bool = False,
) -> Any:
    """Filter docs by type, extract field, apply aggregation. Returns None if no values."""
    typed_docs = [d for d in documents if d.document_type_key == doc_type_key]
    values: list[Any] = []
    for doc in typed_docs:
        raw = doc.indexed_fields.get(field_key)
        if as_date:
            v = _to_date(raw)
        else:
            v = _to_float(raw)
        if v is not None:
            values.append(v)

    if not values:
        return None

    match aggregation:
        case "SINGLE":  return values[0]
        case "SUM":     return sum(values)  # type: ignore[return-value]
        case "MAX":     return max(values)
        case "MIN":     return min(values)
        case "COUNT":   return len(values)
        case _:         return values[0]


def first_doc_of_type(
    documents: list[DocumentContext],
    doc_type_key: str,
) -> DocumentContext | None:
    """Return the first DocumentContext matching doc_type_key, or None."""
    for doc in documents:
        if doc.document_type_key == doc_type_key:
            return doc
    return None


# ── Main builder ──────────────────────────────────────────────────────────────────

_RECONCILIATION_BUCKETS: tuple[tuple[str, str, str, str], ...] = (
    # (bucket, table, key_column, (standard_col, actual_col))
    ("commercial", "auditcore.commercial_lines", "component_key",
     "standard_amount, actual_amount"),
    ("discount", "auditcore.discount_applications", "discount_key",
     "standard_eligible_amount, actual_discount_amount"),
    ("addon", "auditcore.journey_addons", "addon_type_code",
     "standard_amount, actual_amount"),
)


async def _load_reconciliation(
    di_session: AsyncSession,
    tenant_id: str,
    subject_id: UUID | str,
) -> dict[str, dict[str, Any]]:
    """Load Audit Core's standard-vs-actual money projection for the subject.

    subject_id maps to a Journey via docintel.audit_storage_contexts
    (external_context_ref). Best-effort: any failure (Audit Core schema not
    granted to the read-only role, no context row, etc.) returns {} so the
    dependent rules SKIP rather than the audit run failing.
    """
    try:
        journey_ref = (
            await di_session.execute(
                text(
                    """
                    SELECT external_context_ref
                    FROM   docintel.audit_storage_contexts
                    WHERE  tenant_id = :tid AND subject_id = :sid
                    LIMIT  1
                    """
                ),
                {"tid": str(tenant_id), "sid": str(subject_id)},
            )
        ).scalar_one_or_none()
        if not journey_ref:
            return {}

        buckets: dict[str, dict[str, Any]] = {}
        for bucket, table, key_column, amount_columns in _RECONCILIATION_BUCKETS:
            rows = (
                await di_session.execute(
                    # table / columns come only from the fixed _RECONCILIATION_BUCKETS
                    # literal above — no user input in this string.
                    text(
                        f"""
                        SELECT {key_column} AS k, {amount_columns}
                        FROM   {table}
                        WHERE  tenant_id = :tid
                          AND  journey_id = CAST(:jid AS uuid)
                        """
                    ),
                    {"tid": str(tenant_id), "jid": str(journey_ref)},
                )
            ).all()
            entry: dict[str, Any] = {}
            for row in rows:
                entry[str(row[0])] = {
                    "standard": _to_float(row[1]),
                    "actual": _to_float(row[2]),
                }
            buckets[bucket] = entry
        return buckets
    except Exception:
        await di_session.rollback()
        logger.warning(
            "audit_core_reconciliation_load_failed",
            tenant_id=str(tenant_id),
            subject_id=str(subject_id),
        )
        return {}


async def build_audit_context(
    di_session: AsyncSession,
    tenant_id: str,
    subject_id: UUID | str,
    config_overrides: dict[str, Any] | None = None,
) -> AuditContext:
    """
    Load all confirmed documents for a subject from docintel.document_search_index
    (read-only DI connection) and return a populated AuditContext.

    indexed_fields is read as a plain dict — no canonical_fields queries.
    Absent key → None → comparator returns SKIPPED.
    """
    rows = (
        await di_session.execute(
            text("""
                SELECT document_id,
                       document_type_key,
                       indexed_fields
                FROM   docintel.document_search_index
                WHERE  tenant_id  = :tid
                  AND  subject_id = :sid
            """),
            {"tid": str(tenant_id), "sid": str(subject_id)},
        )
    ).mappings().all()

    documents = [
        DocumentContext(
            document_id=UUID(str(row["document_id"])),
            document_type_key=row["document_type_key"],
            indexed_fields=dict(row["indexed_fields"]) if row["indexed_fields"] else {},
        )
        for row in rows
    ]

    config: dict[str, Any] = dict(DEFAULT_CONFIG)
    if config_overrides:
        config.update(config_overrides)

    reconciliation = await _load_reconciliation(di_session, tenant_id, subject_id)

    return AuditContext(
        tenant_id=str(tenant_id),
        subject_id=UUID(str(subject_id)),
        documents=documents,
        config=config,
        reconciliation=reconciliation,
    )
