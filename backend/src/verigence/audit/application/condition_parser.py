"""condition_parser.py — Evaluate condition_expression mini-DSL.

DSL grammar (design doc §7.2):

  atom:
    doc_present:<doc_type_key>
    doc_absent:<doc_type_key>
    field_gt:<doc_type_key>.<field_key>:<threshold>

  compound (space-separated, case-sensitive keywords):
    <atom> AND <atom>   — all atoms must be True
    <atom> OR  <atom>   — any atom must be True

  Returns True  → condition met → rule fires.
           False → condition not met → rule result is SKIPPED.

Pure Python — no I/O, no DB. Operates on AuditContext.
"""
from __future__ import annotations

from verigence.audit.domain.types import AuditContext


def _eval_atom(token: str, context: AuditContext) -> bool:
    """Evaluate a single DSL atom against the given context."""
    token = token.strip()

    if token.startswith("doc_present:"):
        doc_type = token[len("doc_present:"):].strip()
        return any(d.document_type_key == doc_type for d in context.documents)

    if token.startswith("doc_absent:"):
        doc_type = token[len("doc_absent:"):].strip()
        return not any(d.document_type_key == doc_type for d in context.documents)

    if token.startswith("field_gt:"):
        # field_gt:<doc_type>.<field_key>:<threshold>
        rest = token[len("field_gt:"):]
        parts = rest.rsplit(":", 1)
        if len(parts) != 2:
            return False
        doc_field, threshold_str = parts
        dot_idx = doc_field.find(".")
        if dot_idx == -1:
            return False
        doc_type = doc_field[:dot_idx]
        field_key = doc_field[dot_idx + 1:]
        try:
            threshold = float(threshold_str)
        except ValueError:
            return False
        for doc in context.documents:
            if doc.document_type_key == doc_type:
                raw = doc.indexed_fields.get(field_key)
                if raw is None:
                    continue
                try:
                    if float(str(raw).replace(",", "")) > threshold:
                        return True
                except (ValueError, TypeError):
                    continue
        return False

    # Unknown atom — conservative: treat as False
    return False


_KNOWN_ATOM_PREFIXES = ("doc_present:", "doc_absent:", "field_gt:")


def _validate_atom(token: str) -> str | None:
    """Return an error message for one atom, or None if it's well-formed."""
    token = token.strip()
    if not token:
        return "empty atom"
    if not token.startswith(_KNOWN_ATOM_PREFIXES):
        return f"unrecognized atom {token!r} (expected doc_present:/doc_absent:/field_gt:)"

    if token.startswith("field_gt:"):
        rest = token[len("field_gt:"):]
        parts = rest.rsplit(":", 1)
        if len(parts) != 2:
            return f"malformed field_gt atom {token!r} (expected field_gt:<doc_type>.<field_key>:<threshold>)"
        doc_field, threshold_str = parts
        if "." not in doc_field:
            return f"malformed field_gt atom {token!r} (expected <doc_type>.<field_key> before the threshold)"
        try:
            float(threshold_str)
        except ValueError:
            return f"field_gt atom {token!r} has a non-numeric threshold {threshold_str!r}"
        doc_type, field_key = doc_field.split(".", 1)
        if not doc_type or not field_key:
            return f"malformed field_gt atom {token!r} (doc_type and field_key must both be non-empty)"
    else:
        # doc_present:/doc_absent: — everything after the prefix is the doc_type key.
        doc_type = token.split(":", 1)[1].strip()
        if not doc_type:
            return f"malformed atom {token!r} (missing doc_type after the prefix)"

    return None


def validate_condition(expr: str | None) -> list[str]:
    """
    Validate a condition_expression string against the DSL grammar (module
    docstring), without evaluating it against any real context.

    Returns a list of human-readable error messages -- empty means valid.
    An empty/None expression is valid (no precondition).
    """
    if not expr or not expr.strip():
        return []

    expr = expr.strip()
    if " AND " in expr and " OR " in expr:
        return ["condition_expression cannot mix AND and OR -- use only one connective"]

    if " AND " in expr:
        atoms = expr.split(" AND ")
    elif " OR " in expr:
        atoms = expr.split(" OR ")
    else:
        atoms = [expr]

    errors = [error for atom in atoms if (error := _validate_atom(atom)) is not None]
    return errors


def evaluate_condition(expr: str, context: AuditContext) -> bool:
    """
    Evaluate a condition_expression string against an AuditContext.

    Returns True  → condition met, rule should fire.
    Returns False → condition not met, rule result is SKIPPED.
    Empty / None expr → True (no precondition — always fire).
    """
    if not expr or not expr.strip():
        return True

    expr = expr.strip()

    # Compound AND — every atom must be True
    if " AND " in expr:
        return all(_eval_atom(t, context) for t in expr.split(" AND "))

    # Compound OR — any atom must be True
    if " OR " in expr:
        return any(_eval_atom(t, context) for t in expr.split(" OR "))

    # Single atom
    return _eval_atom(expr, context)
