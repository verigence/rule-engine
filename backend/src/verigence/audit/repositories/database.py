"""database.py — Two async SQLAlchemy engine/session factories.

  audit_session  → audit schema (read-write)   — AUDIT_DB_URL
  di_session     → docintel schema (read-only)  — AUDIT_DI_DB_URL

Both are FastAPI Depends-compatible generators AND bare async context
managers (for use in the scheduler / background tasks).
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from verigence.audit.settings import get_settings

# ── Audit engine (RW) ─────────────────────────────────────────────────────────────
_audit_engine = None
_audit_factory: async_sessionmaker[AsyncSession] | None = None


def _get_audit_factory() -> async_sessionmaker[AsyncSession]:
    global _audit_engine, _audit_factory  # noqa: PLW0603
    if _audit_factory is None:
        settings = get_settings()
        _audit_engine = create_async_engine(
            settings.db_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            echo=not settings.is_production,
        )
        _audit_factory = async_sessionmaker(
            _audit_engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _audit_factory


# ── DI engine (read-only) ──────────────────────────────────────────────────────────
_di_engine = None
_di_factory: async_sessionmaker[AsyncSession] | None = None


def _get_di_factory() -> async_sessionmaker[AsyncSession]:
    global _di_engine, _di_factory  # noqa: PLW0603
    if _di_factory is None:
        settings = get_settings()
        _di_engine = create_async_engine(
            settings.di_db_url,
            pool_pre_ping=True,
            pool_size=3,
            max_overflow=5,
            echo=not settings.is_production,
            execution_options={"postgresql_readonly": True},
        )
        _di_factory = async_sessionmaker(
            _di_engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _di_factory


# ── FastAPI Depends-compatible generators ─────────────────────────────────────────

async def get_audit_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an audit RW session. Commits on success, rolls back on error."""
    async with _get_audit_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def get_di_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a DI read-only session. Never commits."""
    async with _get_di_factory()() as session:
        try:
            yield session
        except Exception:
            raise
        finally:
            await session.close()


# ── Async context managers for scheduler / background tasks ──────────────────────

@asynccontextmanager
async def audit_session_ctx() -> AsyncGenerator[AsyncSession, None]:
    """Audit RW session as an async context manager (for non-FastAPI callers)."""
    async with _get_audit_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def di_session_ctx() -> AsyncGenerator[AsyncSession, None]:
    """DI read-only session as an async context manager."""
    async with _get_di_factory()() as session:
        try:
            yield session
        finally:
            await session.close()
