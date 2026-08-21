# verigence-audit — Implementation Plan

**Design reference:** `PRICE_ANOMALY_RULE_ENGINE.md` v5.1 (all section numbers below refer to that document)
**Target repo:** `verigence/rule-engine` (GitHub repo — separate Railway service)
**DI baseline repo:** `verigence/verigence-di` (zero changes — see Sub-Task 0 note below)
**Python:** 3.12 (same as DI)
**Framework:** FastAPI + SQLAlchemy async + Alembic (same stack as DI)
**Logging:** `structlog` — identical pipeline to DI's `logging_config.py`
**Build:** `hatchling` via `pyproject.toml` — identical to DI
**Deploy:** Railway — own `Dockerfile` + `railway.toml`, same pattern as DI

## Hard constraints (re-read before every sub-task)

1. **Zero changes to DI's DB schema** — the audit service creates its own `audit` schema on the same PostgreSQL instance. No `ALTER TABLE` on any `docintel.*` table.
2. **Zero changes to verigence-di** — the audit service is fully independent. The DI webhook integration (Sub-Task 0) is deferred until the team is ready to activate real-time triggering. The nightly batch scheduler (Sub-Task 9) covers all audit needs in the interim.
3. **No shared code imports** — `verigence-audit` does not `pip install verigence-di`. It reads from the shared DB only.
4. **No hallucination** — if a field name, table name, or function signature is uncertain, ask before writing.
5. **Follow DI conventions exactly** — `structlog`, `pydantic-settings` with env prefix, `create_app()` factory, `asynccontextmanager` lifespan, `hatchling` build.
6. **No over-engineering** — no abstract base classes, no plugin registries, no event bus. Straight functions and dataclasses.
7. **Ask before proceeding** if any sub-task has an ambiguity that would require a guess.

---

## Sub-Task 0 — DI webhook integration ⏸ DEFERRED

**Status:** [ ] deferred — activate when real-time triggering is required

**Decision (2026-08-21)**
The audit service is deployed as a fully independent service with zero changes to `verigence-di`.
The nightly batch scheduler (Sub-Task 9, Mode 3) reads `docintel.document_search_index` directly
and covers all audit needs with ≤24h latency — acceptable for an audit use case.

When the team is ready to activate near-real-time (Mode 1) triggering, this sub-task requires
exactly **2 files changed in verigence-di** and a separate PR against the `dev` branch:

1. **New file** `backend/src/verigence/di/audit_webhook.py` (~30 lines) — fire-and-forget POST helper.
   Skip webhook silently if `subject_id is None` (document not linked to a subject).
2. **Edit** `backend/src/verigence/di/settings.py` — add two optional fields:
   ```python
   audit_service_webhook_url: str = ""   # env: DI_AUDIT_SERVICE_WEBHOOK_URL
   audit_webhook_secret: str = ""        # env: DI_AUDIT_WEBHOOK_SECRET
   ```
3. **Edit** `backend/src/verigence/di/workers/job_runner.py` — add one `await` line immediately
   after `await upsert_search_index(...)` at Step 17b (line ~648), guarded by `if subject_id`:
   ```python
   if subject_id:
       from verigence.di.audit_webhook import fire_audit_webhook  # noqa: PLC0415
       await fire_audit_webhook(tenant_id, str(subject_id), accepted_document_type_key)
   ```

No new pip deps needed (httpx already in DI's pyproject.toml). No schema changes. No new DI routes.
Branch off `dev` → PR title: `feat(audit): activate real-time webhook trigger (Sub-Task 0)`.

---

## Sub-Task 1 — New repo scaffold: pyproject, Dockerfile, railway.toml, settings

**Status:** [ ] pending

**Intent**
Create the `verigence/rule-engine` service with the identical build and deploy conventions as DI.
This sub-task produces a runnable skeleton with `/health/live` returning 200 — nothing more.

**Expected outcomes**
- `Dockerfile` and `railway.toml` match DI's pattern (uvicorn factory)
- `pyproject.toml` uses `hatchling`, `env_prefix="AUDIT_"`, same core deps as DI minus Gemini/S3/Pillow/OpenCV
- `src/verigence/audit/main.py` has `create_app()` factory with lifespan, structlog middleware, CORS, correlation-ID middleware
- `src/verigence/audit/settings.py` has `Settings(BaseSettings)` with `env_prefix="AUDIT_"` and required fields: `audit_db_url`, `di_db_url`, `audit_webhook_secret`, `audit_batch_hour`
- `src/verigence/audit/logging_config.py` is a **verbatim copy** of DI's with namespace changed to `verigence.audit`
- `GET /health/live` returns `{"status": "ok"}`

**Todo list**
1. Create `Dockerfile`:
   ```dockerfile
   FROM python:3.12-slim AS runtime
   ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
   WORKDIR /app/backend
   COPY backend/pyproject.toml ./
   COPY backend/src ./src
   RUN pip install --no-cache-dir .
   EXPOSE 8000
   CMD ["sh", "-c", "uvicorn verigence.audit.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000}"]
   ```
2. Create `railway.toml` — same as DI, change `startCommand` and `healthcheckPath`
3. Create `backend/pyproject.toml` — `name="verigence-audit"`, hatchling, same core deps as DI minus Gemini/S3
4. Create `backend/src/verigence/audit/settings.py` — `env_prefix="AUDIT_"`, same `@lru_cache get_settings()` pattern
5. Create `backend/src/verigence/audit/logging_config.py` — verbatim copy of DI's, two namespace changes
6. Create `backend/src/verigence/audit/main.py` — `create_app()` factory, correlation-ID + CORS middleware, `/health/live`

**Relevant context**
- DI `main.py`: `backend/src/verigence/di/main.py` — copy middleware + lifespan skeleton
- DI `settings.py`: `backend/src/verigence/di/settings.py` — copy pattern, change prefix and fields

---

## Sub-Task 2 — Alembic setup + migration 0001 (DDL + 85-rule seed)

**Status:** [ ] pending

**Intent**
Create the audit schema and all three tables, plus seed all 85 rules.

**Expected outcomes**
- `backend/alembic/` directory matches DI's structure
- `alembic upgrade head` creates `audit.audit_rules`, `audit.audit_runs`, `audit.audit_findings`
- `audit_rules` contains exactly 85 rows after migration
- No `docintel.*` table is touched

**Todo list**
1. Copy DI's `backend/alembic/env.py` and `backend/alembic/script.py.mako`, update import to `verigence.audit.settings`
2. Create `backend/alembic/versions/0001_audit_engine.py`:
   - `CREATE SCHEMA IF NOT EXISTS audit`
   - `CREATE TABLE audit.audit_rules` — full DDL from design doc §14.1
   - `CREATE TABLE audit.audit_runs` — full DDL from design doc §14.2
   - `CREATE TABLE audit.audit_findings` — full DDL from design doc §14.3
   - All indexes from design doc §14
   - 85 `INSERT INTO audit.audit_rules` statements from design doc §12
3. Verify: `phases` JSONB matches §18; `requires_both_docs` TRUE only when both doc types non-null; `condition_expression` for 6 conditional rules

**Ask before proceeding if:**
- Any rule has ambiguous `condition_expression` or unclear threshold unit (₹ vs ratio vs days)

**Relevant context**
- Full DDL: design doc §14 | All 85 rules: design doc §12 | Phase mapping: design doc §18

---

## Sub-Task 3 — Domain types + comparators

**Status:** [ ] pending

**Intent**
Pure Python layer — zero I/O, zero DB, zero FastAPI. Fully unit-testable in isolation.

**Expected outcomes**
- `src/verigence/audit/domain/types.py` contains all dataclasses and enums
- `src/verigence/audit/domain/comparators.py` implements all 10 comparator functions
- Every comparator returns `AuditResult.SKIPPED` when either operand is `None`
- `tests/domain/test_comparators.py` has at least one PASS, FAIL, and SKIPPED test per comparator

**Todo list**
1. Create `src/verigence/audit/domain/types.py`:
   - `AuditResult(str, Enum)`: PASS, FAIL, SKIPPED
   - `AuditSeverity(str, Enum)`: CRITICAL, WARNING, INFO
   - `AuditScope(str, Enum)`: WITHIN_CASE, CROSS_CASE
   - `Aggregation(str, Enum)`: SINGLE, SUM, MAX, MIN, COUNT
   - `Comparator(str, Enum)`: 10 values from design doc §8
   - `@dataclass DocumentContext`: `document_id: UUID`, `document_type_key: str`, `indexed_fields: dict[str, Any]`
   - `@dataclass AuditContext`: `tenant_id: str`, `subject_id: UUID`, `documents: list[DocumentContext]`, `config: dict[str, Any]`
   - `@dataclass AuditRule`: all columns from `audit_rules` table
   - `@dataclass AuditFinding`: rule_code, result, severity, category, audit_scope, left_value, right_value, left_doc_id, right_doc_id, detail, affected_subjects
   - `@dataclass AuditRunSummary`: audit_run_id, total_rules, pass_count, fail_count, skipped_count, critical_fail_count, warning_fail_count, info_fail_count, verdict, skipped_reasons
   - Use `str + Enum` (not `StrEnum`) — same as DI for Pydantic v2 compat
2. Create `src/verigence/audit/domain/comparators.py` — one function per comparator, dispatch via `evaluate()`
3. Create `tests/domain/test_comparators.py`

**Relevant context** — Comparator specs: design doc §8

---

## Sub-Task 4 — DB connection setup (two pools: audit RW + DI read-only)

**Status:** [ ] pending

**Intent**
Two SQLAlchemy async engine/session factories: audit (RW) and DI (read-only).

**Expected outcomes**
- `src/verigence/audit/repositories/database.py` provides `get_audit_session()` and `get_di_session()`
- `di_db_url` session uses `execution_options={"postgresql_readonly": True}`

**Todo list**
1. Create `src/verigence/audit/repositories/database.py` — lazy singletons, same `async with session: yield session` pattern as DI
2. No ORM Base — all queries via `text()` SQL

---

## Sub-Task 5 — AuditContextBuilder

**Status:** [ ] pending

**Intent**
Load all confirmed documents for a subject from `docintel.document_search_index` and return `AuditContext`. Follows `reconciliation.py` exactly.

**Expected outcomes**
- `src/verigence/audit/application/context_builder.py` exists
- `build_audit_context(di_session, tenant_id, subject_id, config_overrides) -> AuditContext`
- `_to_float`, `_to_date`, `_to_str` helpers; `aggregate_field()`; `DEFAULT_CONFIG` from §16

**Todo list**
1. Create `context_builder.py` with helpers + `build_audit_context`:
   - Query: `SELECT document_id, document_type_key, indexed_fields FROM docintel.document_search_index WHERE tenant_id = :tid AND subject_id = :sid`
   - `DEFAULT_CONFIG` with all values from design doc §16
2. `aggregate_field` — filter by doc_type_key, apply helper, aggregate by mode
3. Write `tests/application/test_context_builder.py`

**Ask before proceeding if:** `document_search_index` column names differ from design doc §14

---

## Sub-Task 6 — ConditionParser + RuleEvaluator

**Status:** [ ] pending

**Intent**
Pure Python evaluation layer. No DB write happens here.

**Expected outcomes**
- `condition_parser.py` handles all DSL expressions from design doc §7.2
- `evaluator.py` implements `run_audit() -> AuditRunSummary`
- SKIPPED findings never persisted

**Todo list**
1. `condition_parser.py`: `evaluate_condition(expr, context) -> bool` — handles `doc_present:X`, `doc_absent:X`, `field_gt:X.Y:Z`, `AND`/`OR`
2. `evaluator.py`:
   - `load_rules(audit_session, scope, phases=None) -> list[AuditRule]`
   - `resolve_operand(context, doc_type, field_key, aggregation) -> float | date | str | None`
   - `evaluate_rule(rule, context) -> AuditFinding`
   - `async def run_audit(di_session, audit_session, tenant_id, subject_id, phases=None) -> AuditRunSummary`
3. Write `tests/application/test_evaluator.py`

---

## Sub-Task 7 — Repositories (audit_runs + audit_findings)

**Status:** [ ] pending

**Intent**
Persist findings and manage audit run lifecycle. Batch INSERT — never one at a time.

**Expected outcomes**
- `audit_runs.py`: `create_run`, `complete_run`, `list_runs`
- `audit_findings.py`: `persist_findings` (batch), `get_findings`, `get_audit_summary`, `acknowledge_finding`, `bulk_acknowledge`, `get_pending_acknowledgements`
- `persist_findings` marks prior is_current=false before batch insert
- SKIPPED findings never inserted

---

## Sub-Task 8 — Internal webhook endpoint + PhaseRouter + CrossCaseEngine

**Status:** [ ] pending

**Intent**
Wire up execution triggers: (1) webhook from DI, (2) phase-scoped evaluation, (3) cross-case scan.

**Expected outcomes**
- `POST /internal/trigger` — validates `X-Webhook-Secret`, fires as `asyncio.create_task`, returns 202
- `phase_router.py` — maps phase → JSONB filter → `run_audit()`
- `cross_case_engine.py` — runs D1–D6 GROUP BY scans across `document_search_index`

**Todo list**
1. `api/v1/internal.py` — webhook handler, 401 on secret mismatch, never blocks
2. `application/phase_router.py` — `PHASE_TO_JSONB` dict, `run_phase_audit()`
3. `application/cross_case_engine.py` — `run_cross_case_scan()`, one finding per duplicate group

**Relevant context** — Webhook: §9.1 | Cross-case SQL: §12 | Phase values: §18

---

## Sub-Task 9 — Public API: 20 endpoints + nightly scheduler

**Status:** [ ] pending

**Intent**
Expose all 20 endpoints from §17. Wire up APScheduler nightly batch.
Same response envelope as DI: `{"errorCode": "000", "errorMessage": "Success", "data": {...}}`.

**Expected outcomes**
- `api/v1/audit.py` — all 20 routes
- `scheduler/batch.py` — nightly `CronTrigger(hour=settings.audit_batch_hour)`
- APScheduler started in `main.py` lifespan

**Todo list**
1. `api/schemas.py` — `ApiResponse(BaseModel, Generic[T])`, `ok()` / `err()` helpers
2. `api/v1/audit.py` — Group A (7), B (2), C (4), D (2), E (3), F (4)
3. `scheduler/batch.py` — scan subjects updated since last run, `run_audit()` per subject, `run_cross_case_scan()` once
4. Register routers + start scheduler in `main.py` lifespan

**Relevant context** — Endpoints: §17 | DI envelope: `errors.py` in DI (reference only, do not import)

---

## Sub-Task 10 — End-to-end smoke test + deployment

**Status:** [ ] pending

**Intent**
Verify the service runs end-to-end. No new code — configuration and validation only.

**Expected outcomes**
- `alembic upgrade head` creates 3 tables + 85 rows in `audit_rules`
- `pytest` passes with coverage ≥ 70%
- `GET /health/live` returns 200
- `POST /internal/trigger` returns 202 and triggers background log line
- Railway deployment succeeds

**Todo list**
1. `alembic upgrade head` against local Postgres
2. `pytest`
3. Test webhook: `curl -X POST http://localhost:8000/internal/trigger -H "X-Webhook-Secret: test" -d '{"tenant_id":"t1","subject_id":"<uuid>","doc_type_key":"booking_form"}'`
4. Deploy to Railway

---

## Context prompt for post-reset implementation

> Copy this prompt verbatim when starting a new Agent session after context reset.

```
You are implementing the Price Anomaly Rule Engine for Verigence — a separate service
called verigence-audit that reads from verigence-di's document_search_index and runs
85 anomaly detection rules.

The complete design is in the local file:
  PRICE_ANOMALY_RULE_ENGINE.md  (v5.1)

The implementation plan is in:
  verigence-audit-implementation-plan.md

Key facts you must hold:
1. Target repo: verigence/rule-engine — separate FastAPI service, own Railway deploy
2. Zero DI changes — Sub-Task 0 (DI webhook) is DEFERRED. Start with Sub-Task 1.
3. Zero DI DB schema changes — audit service creates its own `audit` schema on the same Postgres
4. No shared code imports — audit reads from shared DB only, no pip install verigence-di
5. Follow DI conventions exactly:
   - structlog for logging (copy logging_config.py, change namespace)
   - pydantic-settings with env_prefix="AUDIT_"
   - hatchling build system (pyproject.toml)
   - create_app() factory, asynccontextmanager lifespan, Dockerfile + railway.toml
   - all SQL via text() — no ORM models
6. The rule engine reads indexed_fields as a plain dict — _to_float()/_to_date() helpers only
   (pattern: reconciliation.py in verigence-di)
7. No over-engineering — no abstract base classes, no event bus, straight functions
8. If anything is ambiguous, ask before implementing

Start with Sub-Task 1 in the plan. Read the plan file first, then read the relevant
design doc sections before writing any code.
```
