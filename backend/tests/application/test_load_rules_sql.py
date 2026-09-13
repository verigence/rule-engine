"""test_load_rules_sql.py — Regression test for a real SQLAlchemy text()
bind-parameter parsing bug in load_rules()'s phase filter.

`:phase_0::jsonb` (no space before the cast) is silently NOT recognized by
SQLAlchemy's text() bind-parameter parser as a bind parameter at all -- it
leaves the literal `:phase_0` in the compiled SQL, which asyncpg then
rejects outright ("syntax error at or near \":\""). This broke every
phase-scoped evaluate_phase call (i.e. every real call this service
receives in production) since the phase filter was introduced.

These tests capture the exact statement/params load_rules() hands to
AsyncSession.execute() via a fake session, then compile that statement with
SQLAlchemy's own postgresql dialect and assert every parameter load_rules
claims to bind is actually recognized as a bind parameter -- not just that
Python itself didn't raise (it never did; the bug only manifests once
asyncpg receives the compiled SQL over the wire).
"""
from __future__ import annotations

from typing import Any

from verigence.audit.application.evaluator import load_rules


class _FakeResult:
    def mappings(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return []


class _FakeSession:
    def __init__(self) -> None:
        self.captured_statement: Any = None
        self.captured_params: dict[str, Any] = {}

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        self.captured_statement = statement
        self.captured_params = params or {}
        return _FakeResult()


def _compiled_bind_names(statement: Any) -> set[str]:
    from sqlalchemy.dialects import postgresql

    return set(statement.compile(dialect=postgresql.dialect()).params.keys())


async def test_load_rules_two_phases_binds_every_phase_param() -> None:
    session = _FakeSession()
    await load_rules(session, scope="WITHIN_CASE", phases=["BOOKING", "DELIVERY"])

    detected = _compiled_bind_names(session.captured_statement)

    # Every key load_rules put in the params dict must be a bind parameter
    # SQLAlchemy actually recognizes -- not silently dropped and left as
    # literal, unresolved text in the compiled SQL.
    assert set(session.captured_params.keys()) == detected
    assert "phase_0" in detected
    assert "phase_1" in detected

    # The exact shape of the original bug: with the postgresql dialect (the
    # one actually used at runtime, asyncpg), a correctly-bound parameter
    # compiles to a positional `$N` placeholder -- a literal `:phase_N`
    # surviving into this compiled SQL means it was never recognized as a
    # bind parameter at all, and would have gone to the driver as-is.
    from sqlalchemy.dialects import postgresql

    compiled_sql = str(session.captured_statement.compile(dialect=postgresql.dialect()))
    assert ":phase_0" not in compiled_sql
    assert ":phase_1" not in compiled_sql


async def test_load_rules_single_phase_binds_correctly() -> None:
    session = _FakeSession()
    await load_rules(session, scope="WITHIN_CASE", phases=["BOOKING"])

    assert _compiled_bind_names(session.captured_statement) == {"scope", "phase_0"}
    assert session.captured_params == {"scope": "WITHIN_CASE", "phase_0": '["BOOKING"]'}


async def test_load_rules_without_phases_has_no_phase_param() -> None:
    session = _FakeSession()
    await load_rules(session, scope="WITHIN_CASE")

    assert session.captured_params == {"scope": "WITHIN_CASE"}
    assert "phase_0" not in str(session.captured_statement)
