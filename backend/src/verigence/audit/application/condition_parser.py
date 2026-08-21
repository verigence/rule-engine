"""condition_parser.py — Evaluate condition_expression mini-DSL.

DSL grammar (design doc §7.2):

  atom:
    doc_present:<doc_type_key>
    doc_absent:<doc_type_key>
    field_gt:<doc_type_key>.<field_key>:<threshold>

  compound:
    <atom> AND <atom>   (space-separated token AND)
    <atom> OR  <atom>

  Evaluation returns True  → condition met → rule should fire.
                     False → condition not met → rule result is SKIPPED.

Pure Python — no I/O, no DB. Operates on AuditContext.
"""
from __future__ import annotations

from verigence.audit.domain.types import AuditContext


def _eval_atom(token: str, context: AuditContext) -> bool:
    """Evaluate a single DSL atom."""
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

    # Unknown atom — treat as False (conservative)
    return False


def evaluate_condition(expr: str, context: AuditContext) -> bool:
    """
    Evaluate a condition_expression string against an AuditContext.

    Returns True  → condition met, rule should fire.
    Returns False → condition not met, rule result is SKIPPED.
    None / empty expr  → True (no precondition — always fire).
    """
    if not expr or not expr.strip():
        return True

    expr = expr.strip()

    # Compound AND  (all tokens must be True)
    if " AND " in expr:
        return all(_eval_atom(t) for t in expr.split(" AND "))  # type: ignore[call-arg]

    # Compound OR   (any token must be True)
    if " OR " in expr:
        return any(_eval_atom(t) for t in expr.split(" OR "))  # type: ignore[call-arg]

    # Single atom
    return _eval_atom(expr, context)


# Fix: pass context into _eval_atom via closure above — correct the nested calls:
def evaluate_condition(expr: str, context: AuditContext) -> bool:  # noqa: F811
    """Evaluate a condition_expression string against an AuditContext."""
    if not expr or not expr.strip():
        return True

    expr = expr.strip()

    if " AND " in expr:
        return all(_eval_atom(t, context) for t in expr.split(" AND "))

    if " OR " in expr:
        return any(_eval_atom(t, context) for t in expr.split(" OR "))

    return _eval_atom(expr, context)
