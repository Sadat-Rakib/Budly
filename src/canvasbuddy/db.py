"""Async engine and session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from canvasbuddy.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    settings = get_settings()
    connect_args: dict = {}
    if settings.database_search_path:
        # Optional schema isolation (e.g. a throwaway schema for demo/mock runs).
        # Unqualified table names then resolve inside that schema on every
        # connection, keeping the public schema untouched.
        connect_args["server_settings"] = {"search_path": settings.database_search_path}
    return create_async_engine(
        settings.database_url,
        # Railway cron services start, work, and exit. Recycling connections keeps a
        # long-lived process from holding a socket the database has already dropped.
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args=connect_args,
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


_store_ready: set[str] = set()


async def ensure_local_store() -> None:
    """Create the tables on a local SQLite store. Postgres uses migrations instead.

    Called on every session rather than only at server startup so that CLI commands
    (``budly sync``, ``budly digest``, ``budly doctor``) work against a store that has
    never been opened by ``budly start``.
    """
    engine = get_engine()
    if engine.dialect.name != "sqlite":
        return
    key = str(engine.url)
    if key in _store_ready:
        return
    from canvasbuddy.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    _store_ready.add(key)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope: commit on success, roll back on any exception."""
    await ensure_local_store()
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
