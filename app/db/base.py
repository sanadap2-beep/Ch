"""Async engine / session factory and database bootstrap helpers."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
from app.db.models import Base

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _engine_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {"echo": settings.db_echo, "future": True}
    if settings.database_url.startswith("postgresql"):
        kwargs.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_recycle=settings.db_pool_recycle,
            pool_pre_ping=True,
        )
    else:
        # SQLite (dev/tests): a single file, check_same_thread disabled for asyncio.
        kwargs["connect_args"] = {"timeout": 30}
    return kwargs


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(settings.database_url, **_engine_kwargs())
        logger.info("DB engine created for %s", settings.database_url.split("@")[-1])
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope: commit on success, rollback on error."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db(create_schema: bool | None = None) -> None:
    """Create tables when ``DB_AUTO_CREATE`` is on (dev / first boot).

    Production should run ``alembic upgrade head`` instead; this is a
    convenience so a fresh deploy is never blocked by an empty schema.
    """
    should_create = settings.db_auto_create if create_schema is None else create_schema
    if not should_create:
        return
    # Import models so metadata is fully populated.
    from app.db import models  # noqa: F401

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database schema ensured (%d tables)", len(Base.metadata.tables))


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        logger.info("DB engine disposed")
    _engine = None
    _session_factory = None


def reset_engine_for_tests(database_url: str | None = None) -> None:
    """Point the global engine at another URL (used by the test-suite)."""
    global _engine, _session_factory
    _engine = None
    _session_factory = None
    if database_url:
        settings.database_url = database_url
