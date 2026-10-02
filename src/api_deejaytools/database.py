from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import get_settings


def async_url(database_url: str) -> str:
    """Point a Postgres URL at the asyncpg driver.

    The shared config holds the plain form deejaytools-api uses
    (``postgresql://`` or ``postgres://``); SQLAlchemy would otherwise try
    psycopg2, which is not installed.
    """
    for prefix in ("postgresql://", "postgres://"):
        if database_url.startswith(prefix):
            return "postgresql+asyncpg://" + database_url[len(prefix) :]
    return database_url


@lru_cache(maxsize=1)
def _get_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(
        async_url(database_url),
        pool_pre_ping=True,
        # No prepared-statement caches, in asyncpg or in SQLAlchemy's adapter.
        # A cached statement pins the type ids it was planned with, so a
        # schema rebuilt under a running service (what the conformance suite
        # does before every run, and what deejaytools-api CONFORMANCE.md
        # requires a target to survive) fails every later query with "cache
        # lookup failed for type". The cost is one extra round trip per
        # query, which this service's traffic does not notice.
        connect_args={"statement_cache_size": 0, "prepared_statement_cache_size": 0},
        # Keeps bound parameters out of every DBAPI exception's string form,
        # which is what a log line or a Sentry event would otherwise carry.
        hide_parameters=True,
    )


def get_engine() -> AsyncEngine:
    """Return the process engine for the configured database."""
    return _get_engine(get_settings().DEEJAYTOOLS_DATABASE_URL)


@lru_cache(maxsize=1)
def _get_sessionmaker(database_url: str) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        _get_engine(database_url), expire_on_commit=False, autoflush=False
    )


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Yield an async database session for a request lifecycle."""
    maker = _get_sessionmaker(get_settings().DEEJAYTOOLS_DATABASE_URL)
    async with maker() as session:
        yield session
