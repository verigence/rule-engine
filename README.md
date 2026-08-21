# verigence-audit — Price Anomaly Rule Engine

Standalone FastAPI service that runs 85 deterministic audit rules against vehicle sale
document chains extracted by `verigence-di`.

## Quick links

- [Design Reference](docs/PRICE_ANOMALY_RULE_ENGINE.md)
- [Implementation Plan](docs/verigence-audit-implementation-plan.md)

## Key facts

| Item | Value |
|---|---|
| Language | Python 3.12 |
| Framework | FastAPI + SQLAlchemy async + Alembic |
| Build | hatchling (`pyproject.toml`) |
| Deploy | Railway (own service) |
| DB | PostgreSQL — `audit` schema (same instance as DI, zero DI schema changes) |
| DI integration | Read-only from `docintel.document_search_index` |
| Rules | 85 (79 within-case + 6 cross-case) |
| Trigger | Nightly batch (Mode 3) — DI webhook deferred (Sub-Task 0) |

## Status

Sub-Task 0 (DI webhook) — ⏸ DEFERRED  
Sub-Task 1 (repo scaffold) — [ ] pending  
See `docs/verigence-audit-implementation-plan.md` for full sub-task list.
