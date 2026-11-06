"""test_run_dedup_postgres.py — write-only-on-change against a real Postgres.

Opt-in: runs only when AUDIT_TEST_DB_URL points at a throwaway Postgres
(e.g. postgresql://postgres@127.0.0.1:55432/audit_test). The schema is
brought to head with this repo's own Alembic migrations, so the seeded rule
codes exist for the findings' FK. Each test uses its own tenant id and
deletes its rows afterwards. Never point this at a shared database.

Requests go through the real FastAPI route (POST .../audit/{phase}) with the
real audit session; only the evaluator (run_audit) is stubbed, so the test
controls the result and the DI database is not needed.
"""
from __future__ import annotations

import copy
import os
import subprocess
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

_DB_URL = os.environ.get("AUDIT_TEST_DB_URL", "")
pytestmark = pytest.mark.skipif(not _DB_URL, reason="AUDIT_TEST_DB_URL not set (local Postgres only)")

os.environ.setdefault("AUDIT_SECRET_KEY", "dev-secret-key-change-in-production-must-be-32c")
os.environ.setdefault("AUDIT_DB_URL",    "postgresql+asyncpg://localhost/test")
os.environ.setdefault("AUDIT_DI_DB_URL", "postgresql+asyncpg://localhost/test")

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from verigence.audit.api.v1 import audit as audit_api  # noqa: E402
from verigence.audit.domain.types import AuditFinding, AuditResult, AuditRunSummary  # noqa: E402
from verigence.audit.main import create_app  # noqa: E402
from verigence.audit.repositories.database import get_audit_session, get_di_session  # noqa: E402

_BACKEND = Path(__file__).resolve().parents[2]
_DOC = UUID("11111111-1111-1111-1111-111111111111")
# Real seeded rule codes (FK); X is shared by BOOKING and DELIVERY.
_A = "PRICE_BOOKING_VS_INVOICE"
_X = "CHASSIS_BOOKING_VS_INVOICE"
_D = "DISCOUNT_INVOICE_VS_BOOKING"
_S = "MODEL_VARIANT_BOOKING_VS_INVOICE"


def _async_url(url: str) -> str:
    return url.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture(scope="module")
def migrated() -> None:
    env = {**os.environ, "AUDIT_DB_URL": _DB_URL}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_BACKEND, env=env, check=True, capture_output=True,
    )


def _finding(code: str, result: AuditResult, **kw: Any) -> AuditFinding:
    base: dict[str, Any] = {
        "rule_code": code, "category": "PRICE", "audit_scope": "WITHIN_CASE",
        "result": result, "severity": "WARNING", "left_value": "100",
        "right_value": "100", "left_doc_id": _DOC, "right_doc_id": None,
        "detail": f"{code} detail",
    }
    base.update(kw)
    return AuditFinding(**base)


def _summary(findings: list[AuditFinding]) -> AuditRunSummary:
    skipped = {f.rule_code: f.detail for f in findings if f.result == AuditResult.SKIPPED}
    fails = [f for f in findings if f.result == AuditResult.FAIL]
    return AuditRunSummary(
        audit_run_id=None,
        total_rules=len(findings),
        pass_count=sum(f.result == AuditResult.PASS for f in findings),
        fail_count=len(fails),
        skipped_count=len(skipped),
        critical_fail=sum(f.severity == "CRITICAL" for f in fails),
        warning_fail=sum(f.severity == "WARNING" for f in fails),
        info_fail=sum(f.severity == "INFO" for f in fails),
        verdict=("INSUFFICIENT_DATA" if len(skipped) == len(findings)
                 else "FINDINGS_PRESENT" if fails else "CLEAN"),
        skipped_reasons=skipped,
        findings=findings,
    )


class Harness:
    def __init__(self, client: httpx.AsyncClient, factory: async_sessionmaker[AsyncSession],
                 tenant: str, subject: str) -> None:
        self.client, self.factory, self.tenant, self.subject = client, factory, tenant, subject
        self.next: dict[str, AuditRunSummary] = {}

    async def call(self, phase: str, summary: AuditRunSummary) -> httpx.Response:
        self.next[phase.upper()] = summary
        resp = await self.client.post(
            f"/v1/tenants/{self.tenant}/subjects/{self.subject}/audit/{phase}",
            json={"includeSkipped": False, "failFast": False},
            headers={"Authorization": f"Bearer mock.{self.tenant}.svc-1.SERVICE"},
        )
        assert resp.status_code == 200, resp.text
        return resp

    async def q(self, sql: str) -> list[dict[str, Any]]:
        async with self.factory() as s:
            rows = (await s.execute(text(sql), {"t": self.tenant})).mappings().all()
            return [dict(r) for r in rows]

    async def runs(self) -> list[dict[str, Any]]:
        return await self.q("SELECT * FROM audit.audit_runs WHERE tenant_id=:t ORDER BY started_at_utc, created_at_utc")

    async def findings(self) -> list[dict[str, Any]]:
        return await self.q("SELECT * FROM audit.audit_findings WHERE tenant_id=:t ORDER BY created_at_utc, rule_code")


@pytest.fixture
async def h(migrated: None, monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[Harness, None]:
    engine = create_async_engine(_async_url(_DB_URL))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    tenant = f"t-dedup-{uuid4().hex[:12]}"
    app = create_app()

    async def audit_session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    async def di_session() -> AsyncGenerator[None, None]:
        yield None

    app.dependency_overrides[get_audit_session] = audit_session
    app.dependency_overrides[get_di_session] = di_session

    harness: Harness
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    harness = Harness(client, factory, tenant, str(uuid4()))

    async def fake_run_audit(*_a: Any, phases: list[str] | None = None, **_k: Any) -> AuditRunSummary:
        assert phases and len(phases) == 1
        return copy.deepcopy(harness.next[phases[0]])

    monkeypatch.setattr(audit_api, "run_audit", fake_run_audit)
    try:
        yield harness
    finally:
        await client.aclose()
        async with factory() as s:
            await s.execute(text("DELETE FROM audit.audit_findings WHERE tenant_id=:t"), {"t": tenant})
            await s.execute(text("DELETE FROM audit.audit_runs WHERE tenant_id=:t"), {"t": tenant})
            await s.commit()
        await engine.dispose()


def _booking(**a_kw: Any) -> AuditRunSummary:
    return _summary([
        _finding(_A, AuditResult.FAIL, **{"right_value": "90", **a_kw}),
        _finding(_X, AuditResult.PASS),
        _finding(_S, AuditResult.SKIPPED, detail="no invoice yet"),
    ])


async def test_first_call_writes_as_before(h: Harness) -> None:
    resp = await h.call("booking", _booking())
    body = resp.json()["data"]

    runs = await h.runs()
    assert len(runs) == 1
    run = runs[0]
    assert body["auditRunId"] == str(run["audit_run_id"])
    assert (run["total_rules"], run["pass_count"], run["fail_count"], run["skipped_count"],
            run["warning_fail"], run["verdict"]) == (3, 1, 1, 1, 1, "FINDINGS_PRESENT")
    assert run["skipped_detail"] == {_S: "no invoice yet"}
    assert run["completed_at_utc"] is not None

    findings = await h.findings()
    assert sorted(f["rule_code"] for f in findings) == sorted([_A, _X])
    assert all(f["is_current"] and f["superseded_by_run_id"] is None for f in findings)
    assert all(f["audit_run_id"] == run["audit_run_id"] for f in findings)

    assert body == {
        "auditRunId": str(run["audit_run_id"]),
        "verdict": "FINDINGS_PRESENT",
        "summary": {"rulesEvaluated": 3, "pass": 1, "fail": 1, "skipped": 1,
                    "critical": 0, "warning": 1, "info": 0},
        "anomalies": [{"ruleCode": _A, "severity": "WARNING", "category": "PRICE",
                       "detail": f"{_A} detail", "leftValue": "100", "rightValue": "90"}],
        "skippedRules": [{"ruleCode": _S, "reason": "no invoice yet"}],
        "phase": "BOOKING",
    }


async def test_identical_second_call_writes_nothing_and_returns_same_bytes(h: Harness) -> None:
    first = await h.call("booking", _booking())
    runs_before, findings_before = await h.runs(), await h.findings()

    second = await h.call("booking", _booking())
    third = await h.call("booking", _booking())

    assert second.content == first.content
    assert third.content == first.content
    assert await h.runs() == runs_before
    assert await h.findings() == findings_before


async def test_changed_result_writes_new_run_and_supersedes_as_before(h: Harness) -> None:
    first = await h.call("booking", _booking())
    old_run = first.json()["data"]["auditRunId"]

    second = await h.call("booking", _booking(right_value="80"))
    new_run = second.json()["data"]["auditRunId"]
    assert new_run != old_run
    assert second.json()["data"]["anomalies"][0]["rightValue"] == "80"

    runs = await h.runs()
    assert [str(r["audit_run_id"]) for r in runs] == [old_run, new_run]
    findings = await h.findings()
    old = [f for f in findings if str(f["audit_run_id"]) == old_run]
    new = [f for f in findings if str(f["audit_run_id"]) == new_run]
    # Every rule of the new run is re-written and the old rows are superseded
    # by it -- including the PASS rule whose value did not move (as before).
    assert sorted(f["rule_code"] for f in new) == sorted([_A, _X])
    assert all(f["is_current"] for f in new)
    assert all(not f["is_current"] and str(f["superseded_by_run_id"]) == new_run for f in old)


async def test_reverting_to_an_older_result_is_a_change(h: Harness) -> None:
    await h.call("booking", _booking())
    await h.call("booking", _booking(right_value="80"))
    third = await h.call("booking", _booking())
    assert len(await h.runs()) == 3
    current = [f for f in await h.findings() if f["is_current"]]
    assert {f["rule_code"]: str(f["audit_run_id"]) for f in current} == {
        _A: third.json()["data"]["auditRunId"], _X: third.json()["data"]["auditRunId"]}


async def test_changed_skip_reason_or_severity_is_a_change(h: Harness) -> None:
    await h.call("booking", _booking())
    s = _booking()
    s.skipped_reasons[_S] = "different reason"
    s.findings[2].detail = "different reason"
    await h.call("booking", s)
    assert len(await h.runs()) == 2
    await h.call("booking", _booking(severity="INFO"))
    assert len(await h.runs()) == 3


async def test_shared_rule_does_not_ping_pong_between_phases(h: Harness) -> None:
    delivery = _summary([_finding(_X, AuditResult.PASS), _finding(_D, AuditResult.PASS)])
    b1 = await h.call("booking", _booking())
    d1 = await h.call("delivery", delivery)
    # DELIVERY re-wrote the shared rule _X with the same value, superseding
    # BOOKING's row for it. Repeats of either phase still write nothing.
    b2 = await h.call("booking", _booking())
    d2 = await h.call("delivery", delivery)
    assert b2.content == b1.content
    assert d2.content == d1.content
    assert len(await h.runs()) == 2


async def test_all_skipped_repeat_writes_nothing(h: Harness) -> None:
    s = _summary([_finding(_S, AuditResult.SKIPPED, detail="no documents")])
    first = await h.call("booking", s)
    second = await h.call("booking", s)
    assert second.content == first.content
    assert len(await h.runs()) == 1
    assert await h.findings() == []


async def test_other_subject_or_tenant_is_never_reused(h: Harness) -> None:
    await h.call("booking", _booking())
    h.subject = str(uuid4())
    await h.call("booking", _booking())
    assert len(await h.runs()) == 2
