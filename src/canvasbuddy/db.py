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


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope: commit on success, roll back on any exception."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
