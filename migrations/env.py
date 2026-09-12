"""Alembic environment — async, driven by ``app.config.settings.database_url``.

Works against PostgreSQL in production (asyncpg) and SQLite in tests/dev
(aiosqlite), because the models only use portable types (``sqlalchemy.Uuid``,
``JSON`` with a ``JSONB`` variant, timezone-aware ``DateTime``).
"""
from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.db.models import ALL_MODELS, Base  # noqa: F401 — import registers every table

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# CLI override: alembic -x database_url=postgresql+asyncpg://… upgrade head
_x_args = context.get_x_argument(as_dictionary=True)
DATABASE_URL = _x_args.get("database_url") or os.getenv("DATABASE_URL") or settings.database_url
config.set_main_option("sqlalchemy.url", DATABASE_URL)

target_metadata = Base.metadata

# tables alembic should never try to manage
EXCLUDE_TABLES = {"alembic_version"}


def include_object(obj, name, type_, reflected, compare_to) -> bool:  # noqa: ANN001
    if type_ == "table" and name in EXCLUDE_TABLES:
        return False
    return True


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live connection (`alembic upgrade head --sql`)."""
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
        render_as_batch=DATABASE_URL.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:  # noqa: ANN001
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
        # SQLite cannot ALTER most things in place → batch mode rewrites the table
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(DATABASE_URL, poolclass=None, future=True)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
