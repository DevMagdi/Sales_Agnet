"""Async engine/session management.

The URL comes from DATABASE_URL. PostgreSQL 16 is the production target (`postgresql+asyncpg`),
SQLite (`sqlite+aiosqlite`) is used for tests and single-node dev so the whole system runs
without extra infrastructure (see docs/DECISIONS.md #2).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.db.base import Base

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def build_engine(url: str | None = None, *, echo: bool = False) -> AsyncEngine:
    settings = get_settings()
    target = url or settings.database_url
    kwargs: dict[str, Any] = {"echo": echo, "future": True}
    if target.startswith("sqlite"):
        kwargs["connect_args"] = {"timeout": 30}
        if ":memory:" in target:
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool
    else:
        kwargs["pool_size"] = 10
        kwargs["max_overflow"] = 20
        kwargs["pool_pre_ping"] = True
    return create_async_engine(target, **kwargs)


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False, class_=AsyncSession)
    return _session_factory


def configure_engine(engine: AsyncEngine) -> None:
    """Point the process at a specific engine (tests, CLI overrides)."""
    global _engine, _session_factory
    _engine = engine
    _session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one transaction per request."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_all(engine: AsyncEngine | None = None) -> None:
    target = engine or get_engine()
    async with target.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_all(engine: AsyncEngine | None = None) -> None:
    target = engine or get_engine()
    async with target.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
