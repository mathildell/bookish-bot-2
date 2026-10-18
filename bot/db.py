"""Database layer: async SQLAlchemy engine/session over MySQL (aiomysql)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.schema import CreateColumn

from .models import Base

logger = logging.getLogger("bh2")

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_db(database_url: str) -> None:
    """Create the async engine and session factory for the given URL."""
    global _engine, _session_factory
    _engine = create_async_engine(database_url, pool_pre_ping=True)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)


def _add_missing_columns(sync_conn) -> list[str]:
    """Add columns that the models declare but existing tables lack.

    ``create_all`` only creates whole tables, so a table that already exists
    never gains new columns. Without this, a deploy that adds a column would
    leave the old schema in place and every query selecting it would fail.
    Only additive changes are handled here; drops/renames/type changes still
    need a migration script.
    """
    inspector = inspect(sync_conn)
    tables = set(inspector.get_table_names())
    added: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            continue
        present = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            ddl = str(CreateColumn(column).compile(dialect=sync_conn.dialect))
            sync_conn.execute(
                text(f"ALTER TABLE `{table.name}` ADD COLUMN {ddl}")
            )
            added.append(f"{table.name}.{column.name}")
    return added


async def create_tables() -> None:
    """Create missing tables and additively add missing columns (idempotent)."""
    assert _engine is not None, "init_db() must be called first"
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        added = await conn.run_sync(_add_missing_columns)
    if added:
        logger.info("Added missing database columns: %s", ", ".join(added))


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Yield a session that is always closed afterwards."""
    assert _session_factory is not None, "init_db() must be called first"
    async with _session_factory() as session:
        yield session


async def close_db() -> None:
    """Dispose of the engine and its connection pool on shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None