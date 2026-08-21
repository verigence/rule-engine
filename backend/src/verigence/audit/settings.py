"""settings.py — Verigence Audit service configuration.

All runtime configuration is read from environment variables.
No secrets live in source code. Uses pydantic-settings for
validation and type coercion at startup.
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

    # Logging — mirrors DI D27 pattern
    log_level: str = "INFO"               # DEBUG | INFO | WARNING | ERROR
    log_stdout: bool = True                # emit structured logs to stdout
    log_axiom: bool = False                # emit logs to Axiom (async, fire-and-forget)
    axiom_token: str = ""                  # Axiom API token (required if log_axiom=true)
    axiom_dataset: str = "verigence-audit" # Axiom dataset name

    # Database — audit service own DB (read-write)
    db_url: str = ""  # postgresql+asyncpg://...  (AUDIT_DB_URL)

    # Database — DI shared DB (read-only, docintel schema)
    di_db_url: str = ""  # postgresql+asyncpg://...  (AUDIT_DI_DB_URL)

    # Auth
    security_jwks_url: str = ""  # https://<security-host>/.well-known/jwks.json

    # Webhook security
    webhook_secret: str = ""  # X-Webhook-Secret header value (AUDIT_WEBHOOK_SECRET)

    # Nightly batch scheduler
    batch_hour: int = 2  # UTC hour to run nightly batch (AUDIT_BATCH_HOUR)

    # Sentry
    sentry_dsn: str = ""

    # Derived helpers
    @property
    def is_production(self) -> bool:
        return self.env == Environment.PRODUCTION

    @model_validator(mode="after")
    def safety_rules(self) -> Settings:
        """Block unsafe configurations at startup — fail fast before serving traffic."""
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
        # asyncpg does not accept ?sslmode=require — replace with ?ssl=require
        v = v.replace("?sslmode=require", "?ssl=require")
        v = v.replace("&sslmode=require", "&ssl=require")
        return v


@lru_cache
def get_settings() -> Settings:
    """Return cached Settings instance. Use as FastAPI dependency."""
    return Settings()  # type: ignore[call-arg]
