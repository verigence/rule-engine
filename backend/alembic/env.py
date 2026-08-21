"""Alembic environment — sync psycopg2 connection for DDL migrations.

We use a synchronous psycopg2 engine for migrations only.
The application runtime uses asyncpg (async), but Alembic DDL
does not benefit from async and psycopg2 handles multi-statement
SQL blocks without the asyncpg prepared-statement restriction.
"""
from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool, text

# Alembic Config object
config = context.config

# Read DB URL from environment — prefer AUDIT_DB_URL, fall back to DATABASE_URL
database_url = os.environ.get("AUDIT_DB_URL", os.environ.get("DATABASE_URL", ""))
sync_url = (
    database_url
    .replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    .replace("postgresql://", "postgresql+psycopg2://")
    .replace("postgres://", "postgresql+psycopg2://")
    .replace("?ssl=require", "?sslmode=require")
    .replace("&ssl=require", "&sslmode=require")
)
if sync_url:
    config.set_main_option("sqlalchemy.url", sync_url)

# Logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table_schema="audit",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(
        config.get_main_option("sqlalchemy.url"),  # type: ignore[arg-type]
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        # Ensure audit schema exists before Alembic creates its version table
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS audit"))
        connection.commit()

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema="audit",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
