# Price Anomaly Rule Engine — Design Summary

**Full document version:** v5.1 (2026-08-18)  
**Status:** DESIGN COMPLETE — READY FOR IMPLEMENTATION  
**Full local copy:** `PRICE_ANOMALY_RULE_ENGINE.md` in the RuleEngine workspace (2,552 lines)

> This committed copy is a structured summary. The workspace copy is the full authoritative reference.

---

## 1. Overview

85 deterministic rules across 16 within-case categories + 6 cross-case rules.  
Standalone FastAPI service (`verigence/rule-engine`). Zero changes to `verigence-di`.

| Metric | Value |
|---|---|
| Total rules | 85 (79 within-case + 6 cross-case) |
| CRITICAL rules | 41 |
| WARNING rules | 43 |
| INFO rules | 2 |
| API endpoints | 20 |
| Trigger modes | 3 (webhook deferred; nightly batch active) |

---

## 2. Rule Categories

| # | Category | Rules | CRITICAL | WARNING |
|---|---|---|---|---|
| 1 | Price Chain | 9 | 6 | 3 |
| 2 | Discount Chain | 7 | 4 | 3 |
| 3 | Accessory Chain | 5 | 1 | 4 |
| 4 | Insurance Money Chain | 4 | 2 | 2 |
| 5 | RTO / Registration Money Chain | 3 | 1 | 2 |
| 6 | Vehicle Identity Chain | 7 | 3 | 4 |
| 7 | Date / Timeline Chain | 8 | 1 | 5 |
| 8 | Exchange / Trade-in Chain | 5 | 2 | 3 |
| 9 | KYC / Identity Compliance | 7 | 2 | 5 |
| 10 | Third-Party Payment | 3 | 2 | 1 |
| 11 | Corporate Customer Compliance | 4 | 1 | 3 |
| 12 | NDC and Delivery Gating | 2 | 1 | 1 |
| 13 | Document Completeness | 5 | 2 | 3 |
| 14 | Process / Internal Control | 6 | 2 | 4 |
| 15 | Tally vs DMS Cross-System | 2 | 2 | 0 |
| 16 | Debit Notes | 2 | 2 | 0 |
| — | **Cross-Case / Duplicate Detection** | **6** | **6** | **0** |

---

## 3. Architecture

```
verigence-di (unchanged)
  └─ document_search_index (JSONB, read-only from audit)
           ↓ reads (shared Postgres, read-only connection)
verigence/rule-engine (this repo)
  ├─ AuditContextBuilder  → loads indexed_fields as plain dict
  ├─ RuleEvaluator        → 85 pure-Python comparators
  ├─ AuditFindingRepo     → batch writes to audit.* schema
  └─ APScheduler          → nightly batch (Mode 3)
```

---

## 4. DB Schema (audit schema)

| Table | Purpose |
|---|---|
| `audit.audit_rules` | 85 rule definitions (seeded via migration) |
| `audit.audit_runs` | One row per evaluation run |
| `audit.audit_findings` | One row per PASS/FAIL result |

Full DDL: design doc §14.

---

## 5. Comparator Library (10 comparators)

`ABS_DIFF_GT` · `NOT_EQ` · `GT` · `LT` · `EQ` · `DATE_BEFORE` · `DATE_DIFF_GT` · `RATIO_LT` · `FIELD_EMPTY` · `CROSS_DOC_SUM_GT`

All return `SKIPPED` when either operand is `None`. Full spec: design doc §8.

---

## 6. API Groups (20 endpoints)

| Group | Endpoints | Purpose |
|---|---|---|
| A | 7 | Phase-scoped audit (BOOKING, DELIVERY, FINANCE, EXCHANGE, CORPORATE) |
| B | 2 | Full subject audit + run history |
| C | 4 | Findings query (read-only) |
| D | 2 | Cross-case duplicate scan |
| E | 3 | Acknowledgement (CRITICAL requires PM sign-off) |
| F | 4 | Rule management (list, readiness, config override, re-evaluate) |

Full spec: design doc §17.

---

## 7. Phase-to-Rules Mapping

Each phase-scoped API (Group A) evaluates a subset of the 79 within-case rules.
Full mapping table: design doc §18.

---

## 8. Config Constants (§16)

| Key | Default | Used by |
|---|---|---|
| `config.region_max_discount` | 0 | DISCOUNT_EXCEEDS_POLICY |
| `config.cash_limit` | 200000 | CASH_ABOVE_2_LAKH |
| `config.market_floor_ratio` | 0.85 | EXCHANGE_VALUE_BELOW_MARKET |
| `config.max_rc_delay_days` | 45 | RC_DELAY_EXCESSIVE |
| `config.max_booking_to_delivery_days` | 180 | BOOKING_TO_DELIVERY_EXCESS |

---

## 9. Finding Lifecycle

```
FAIL created → acknowledgement_state = PENDING
  → ACKNOWLEDGED (by PM/TL)
  → WAIVED (by TENANT_ADMIN)

Re-audit: prior finding marked superseded (is_current=false), new finding created
```

Verdict: `CLEAN` | `FINDINGS_PRESENT` | `CRITICAL_OPEN` | `INSUFFICIENT_DATA`

---

## 10. Sub-Task 0 — DI webhook (DEFERRED)

The audit service is **zero-change to verigence-di** in Phase 1.  
Nightly batch (Mode 3) covers all audit needs.  
Webhook activation = 2-file DI PR when needed.  
See `verigence-audit-implementation-plan.md` for exact instructions.
