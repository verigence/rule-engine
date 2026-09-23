"""tests/test_no_unescaped_jsonb_cast_bind_params.py

Regression guard for a bug pattern this codebase has already hit twice:
a raw SQL bind parameter immediately followed by Postgres's `::` cast
operator (e.g. `:detail::jsonb`) is not recognized as a bind parameter at
all by SQLAlchemy's text() parser -- it is left as literal `:detail` in the
compiled SQL, which asyncpg then rejects with a syntax error. See
application/evaluator.py's own detailed comment (already fixed there) for
the full diagnosis. repositories/audit_runs.py's complete_run() and
api/v1/audit.py's rule-create endpoint both had the identical pattern,
confirmed live via real Railway logs (2026-09-23) breaking every
evaluate_phase-driven audit run completion -- fixed to CAST(:name AS jsonb)
instead. This test greps the whole source tree so a third occurrence can't
ship silently again.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_BAD_PATTERN = re.compile(r":[a-zA-Z_][a-zA-Z0-9_]*::")
_SRC_ROOT = Path(__file__).resolve().parents[1] / "src"


@pytest.mark.no_docker
def test_no_source_file_binds_a_named_parameter_directly_into_a_pg_cast() -> None:
    offenders: list[str] = []
    for path in _SRC_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if _BAD_PATTERN.search(line):
                offenders.append(f"{path.relative_to(_SRC_ROOT)}:{line_no}: {stripped}")
    assert offenders == [], (
        "Found `:name::type` -- SQLAlchemy's text() bind-parameter parser does not "
        "recognize a name immediately followed by `::` and silently leaves it as "
        "literal text, which asyncpg then rejects. Use CAST(:name AS type) instead. "
        f"Offending lines:\n" + "\n".join(offenders)
    )
