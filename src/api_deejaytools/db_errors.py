"""Database error classification (deejaytools-api src/lib/db-errors.ts)."""

from __future__ import annotations

UNIQUE_VIOLATION = "23505"


def is_unique_violation(exc: BaseException, constraint: str | None = None) -> bool:
    """Whether ``exc`` is Postgres's unique violation (SQLSTATE 23505),
    optionally on one constraint or unique index.

    SQLAlchemy wraps the driver error, and the asyncpg adapter wraps asyncpg's,
    so the code is looked for down the wrapping chain.
    """
    seen: BaseException | None = exc
    for _ in range(5):
        if seen is None:
            return False
        code = getattr(seen, "sqlstate", None) or getattr(seen, "pgcode", None)
        if code == UNIQUE_VIOLATION:
            if constraint is None:
                return True
            name = getattr(seen, "constraint_name", None)
            if name is None and seen.__cause__ is not None:
                name = getattr(seen.__cause__, "constraint_name", None)
            return name == constraint
        seen = getattr(seen, "orig", None) or seen.__cause__
    return False


def driver_message(exc: BaseException) -> str:
    """The database's own message for ``exc``, as postgres.js's error message
    carries it (``invalid byte sequence for encoding "UTF8": 0x00``), without
    SQLAlchemy's SQL text and wrapping."""
    seen: BaseException | None = exc
    for _ in range(5):
        if seen is None:
            break
        if getattr(seen, "sqlstate", None) is not None:
            # asyncpg's PostgresError: args[0] is the server's message.
            return str(seen.args[0]) if seen.args else str(seen)
        seen = getattr(seen, "orig", None) or seen.__cause__
    return str(exc)
