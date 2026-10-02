"""SQLAlchemy models over the existing deejaytools schema.

The schema is migrations/001_baseline.sql — drizzle's result, adopted as is.
These models describe it; they never create it (no ``create_all``).
Timestamps are epoch milliseconds in ``bigint`` columns, as everywhere in
this schema.

The identity tables have their own models in the ``identity`` library.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Enum, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for the deejaytools tables."""


class User(Base):
    """A signed-in person. ``id`` is their Clerk subject (JWT ``sub``).

    ``role`` is a mirror kept for deejaytools-api, which still authorizes
    from it until it is retired (ADR-007). This service never decides from
    it: authority comes from the identity store.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    first_name: Mapped[str | None] = mapped_column(Text)
    last_name: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(
        Enum("user", "admin", name="user_role", create_type=False),
        nullable=False,
        server_default="user",
    )
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
