"""types.py — Pure domain types for the Price Anomaly Rule Engine.

No I/O, no DB, no FastAPI imports. Fully unit-testable in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any
from uuid import UUID


class AuditResult(str, Enum):
    PASS    = "PASS"
    FAIL    = "FAIL"
    SKIPPED = "SKIPPED"


class AuditSeverity(str, Enum):
    CRITICAL = "CRITICAL"
    WARNING  = "WARNING"
    INFO     = "INFO"


class AuditScope(str, Enum):
    WITHIN_CASE = "WITHIN_CASE"
    CROSS_CASE  = "CROSS_CASE"


class Aggregation(str, Enum):
    SINGLE = "SINGLE"
    SUM    = "SUM"
    MAX    = "MAX"
    MIN    = "MIN"
    COUNT  = "COUNT"


class Comparator(str, Enum):
    ABS_DIFF_GT      = "ABS_DIFF_GT"
    NOT_EQ           = "NOT_EQ"
    GT               = "GT"
    LT               = "LT"
    EQ               = "EQ"
    DATE_BEFORE      = "DATE_BEFORE"
    DATE_DIFF_GT     = "DATE_DIFF_GT"
    RATIO_LT         = "RATIO_LT"
    FIELD_EMPTY      = "FIELD_EMPTY"
    CROSS_DOC_SUM_GT = "CROSS_DOC_SUM_GT"


@dataclass
class DocumentContext:
    document_id:       UUID
    document_type_key: str
    indexed_fields:    dict[str, Any]


@dataclass
class AuditContext:
    tenant_id:  str
    subject_id: UUID
    documents:  list[DocumentContext]
    config:     dict[str, Any]  # resolved config constants from DEFAULT_CONFIG + overrides


@dataclass
class AuditRule:
    rule_code:            str
    category:             str
    audit_scope:          str
    phases:               list[str]

    left_doc_type:        str | None
    left_field_key:       str | None
    left_aggregation:     str

    right_doc_type:       str | None
    right_field_key:      str | None
    right_aggregation:    str
    right_config_key:     str | None

    comparator:           str
    threshold:            float

    severity:             str
    finding_message:      str
    condition_expression: str | None
    requires_both_docs:   bool
    enabled:              bool


@dataclass
class AuditFinding:
    rule_code:        str
    category:         str
    audit_scope:      str
    result:           AuditResult
    severity:         str
    left_value:       str | None
    right_value:      str | None
    left_doc_id:      UUID | None
    right_doc_id:     UUID | None
    detail:           str                   # rendered finding_message
    affected_subjects: list[UUID] = field(default_factory=list)  # CROSS_CASE only


@dataclass
class AuditRunSummary:
    audit_run_id:        UUID | None  # set after DB persistence
    total_rules:         int
    pass_count:          int
    fail_count:          int
    skipped_count:       int
    critical_fail:       int
    warning_fail:        int
    info_fail:           int
    verdict:             str  # CLEAN | FINDINGS_PRESENT | CRITICAL_OPEN | INSUFFICIENT_DATA
    skipped_reasons:     dict[str, str]  # rule_code → human-readable skip reason
    findings:            list[AuditFinding] = field(default_factory=list)
