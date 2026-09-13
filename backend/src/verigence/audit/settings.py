"""settings.py — Verigence Rule Engine service configuration.

All runtime configuration is read from environment variables.
No secrets live in source code. Uses pydantic-settings for validation and type coercion.
"""
from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    LOCAL = "local"
    DEV = "dev"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AUDIT_",
        env_file=("infra/.env.local", "infra/.env.dev", "infra/.env.prod"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application
    env: Environment = Environment.LOCAL
    secret_key: str = Field(default="dev-secret-key-change-in-production-must-be-32c", min_length=32)

    # Local structured logging
    log_level: str = "INFO"
    log_stdout: bool = True
    # Deprecated direct-Axiom settings retained only for runtime compatibility.
    # Remote telemetry uses OTLP_EXPORTER_* variables through the independent controls below.
    log_axiom: bool = False
    axiom_token: str = ""
    axiom_dataset: str = "verigence-audit"

    # Independent, fail-open observability capabilities. Tracing is deliberately off by default.
    observability_logs_enabled: bool = False
    observability_errors_enabled: bool = False
    observability_metrics_enabled: bool = False
    observability_traces_enabled: bool = False
    observability_service_name: str = "verigence-rule-engine"
    observability_export_timeout_seconds: float = Field(default=2.0, gt=0)
    observability_batch_delay_ms: int = Field(default=1000, gt=0)
    observability_max_queue_size: int = Field(default=2048, gt=0)
    observability_max_export_batch_size: int = Field(default=512, gt=0)
    observability_metric_export_interval_ms: int = Field(default=60000, gt=0)

    # Database — audit/rule engine own DB (read-write)
    db_url: str = ""

    # Database — DI shared DB (read-only, docintel schema)
    di_db_url: str = ""

    # Auth
    security_jwks_url: str = ""

    # Webhook security
    webhook_secret: str = ""

    # Nightly batch scheduler
    batch_hour: int = 2

    # Sentry
    sentry_dsn: str = ""

    @property
    def is_production(self) -> bool:
        return self.env == Environment.PRODUCTION

    @model_validator(mode="after")
    def safety_rules(self) -> Settings:
        """Block unsafe configurations at startup — fail fast before serving traffic."""
        if self.observability_max_export_batch_size > self.observability_max_queue_size:
            raise ValueError(
                "AUDIT_OBSERVABILITY_MAX_EXPORT_BATCH_SIZE cannot exceed "
                "AUDIT_OBSERVABILITY_MAX_QUEUE_SIZE"
            )
        if self.is_production:
            if not self.db_url:
                raise ValueError("AUDIT_DB_URL must be set in production")
            if not self.di_db_url:
                raise ValueError("AUDIT_DI_DB_URL must be set in production")
            if not self.security_jwks_url or "mock" in self.security_jwks_url.lower():
                raise ValueError(
                    "AUDIT_SECURITY_JWKS_URL must be a real JWKS endpoint in production"
                )
        return self

    @field_validator("db_url", "di_db_url")
    @classmethod
    def normalise_db_url(cls, v: str) -> str:
        """Ensure asyncpg driver prefix and fix sslmode param for asyncpg."""
        if not v:
            return v
        v = (
            v.replace("postgresql://", "postgresql+asyncpg://")
            .replace("postgres://", "postgresql+asyncpg://")
        )
        v = v.replace("?sslmode=require", "?ssl=require")
        v = v.replace("&sslmode=require", "&ssl=require")
        return v


@lru_cache
def get_settings() -> Settings:
    """Return cached Settings instance. Use as FastAPI dependency."""
    return Settings()  # type: ignore[call-arg]
