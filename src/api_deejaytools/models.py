"""SQLAlchemy models over the existing deejaytools schema.

The schema is migrations/001_baseline.sql — drizzle's result, adopted as is.
These models describe it; they never create it (no ``create_all``), so they
carry columns and primary keys only, not constraints or indexes. Generated
from a baseline-built database and kept in its column order.

Timestamps are epoch milliseconds in ``bigint`` columns, as everywhere in
this schema. Postgres enums are mapped as strings.

The identity tables have their own models in the ``identity`` library.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Enum,
    Integer,
    LargeBinary,
    Table,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for the deejaytools tables."""


def _pg_enum(name: str, *values: str) -> Enum:
    """An existing Postgres enum, mapped as plain strings."""
    return Enum(*values, name=name, create_type=False)


USER_ROLE = _pg_enum("user_role", "user", "admin")
SESSION_STATUS = _pg_enum(
    "session_status",
    "scheduled",
    "checkin_open",
    "in_progress",
    "completed",
    "cancelled",
)
PARTNER_ROLE = _pg_enum("partner_role", "leader", "follower")
QUEUE_TYPE = _pg_enum("queue_type", "priority", "non_priority", "active")
INITIAL_QUEUE = _pg_enum("initial_queue", "priority", "non_priority")
QUEUE_EVENT_ACTION = _pg_enum(
    "queue_event_action",
    "checked_in",
    "promoted_to_active",
    "run_completed",
    "run_incomplete_rotated",
    "withdrawn",
    "moved_within_queue",
)


class User(Base):
    """A signed-in person. ``id`` is their Clerk subject (JWT ``sub``).

    ``role`` is a mirror kept for deejaytools-api, which still authorizes
    from it until it is retired (ADR-007). This service never decides from
    it: authority comes from the identity store.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    email: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    first_name: Mapped[str | None] = mapped_column(Text)
    last_name: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(USER_ROLE, server_default="user")
    created_at: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)


class Event(Base):
    """An event: a date range in a timezone, holding sessions and submissions."""

    __tablename__ = "events"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)
    start_date: Mapped[str] = mapped_column(Text)
    end_date: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text, server_default="America/Chicago")
    season_year: Mapped[str | None] = mapped_column(Text)


class Partner(Base):
    """An entry in a user's address book of dance partners and placeholders."""

    __tablename__ = "partners"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text)
    first_name: Mapped[str] = mapped_column(Text)
    last_name: Mapped[str] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    linked_user_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)
    partner_role: Mapped[str] = mapped_column(PARTNER_ROLE, server_default="follower")
    kind: Mapped[str] = mapped_column(Text, server_default="partner")


class Team(Base):
    """A team name a user competes under."""

    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text)
    identifier: Mapped[str] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)


class ManagedPartnership(Base):
    """A couple managed by a user, named only, with no linked accounts."""

    __tablename__ = "managed_partnerships"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text)
    leader_first_name: Mapped[str] = mapped_column(Text)
    leader_last_name: Mapped[str] = mapped_column(Text)
    follower_first_name: Mapped[str] = mapped_column(Text)
    follower_last_name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)
    deleted_at: Mapped[int | None] = mapped_column(BigInteger)


class Pair(Base):
    """A registered pair: a signed-in user plus one of their partners."""

    __tablename__ = "pairs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_a_id: Mapped[str] = mapped_column(Text)
    partner_b_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(BigInteger)


class Song(Base):
    """A routine's music, owned by a user, with its file in Drive."""

    __tablename__ = "songs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text)
    partner_id: Mapped[str | None] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    original_filename: Mapped[str | None] = mapped_column(Text)
    drive_file_id: Mapped[str | None] = mapped_column(Text)
    drive_folder_id: Mapped[str | None] = mapped_column(Text)
    processed_filename: Mapped[str | None] = mapped_column(Text)
    division: Mapped[str | None] = mapped_column(Text)
    routine_name: Mapped[str | None] = mapped_column(Text)
    personal_descriptor: Mapped[str | None] = mapped_column(Text)
    season_year: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)
    deleted_at: Mapped[int | None] = mapped_column(BigInteger)
    managed_partnership_id: Mapped[str | None] = mapped_column(Text)


class EventSongSubmission(Base):
    """A song entered in an event, with its copy in the event's Drive folder."""

    __tablename__ = "event_song_submissions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    event_id: Mapped[str] = mapped_column(Text)
    song_id: Mapped[str] = mapped_column(Text)
    submitted_by_user_id: Mapped[str] = mapped_column(Text)
    drive_copy_file_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(BigInteger)
    division: Mapped[str | None] = mapped_column(Text)
    round: Mapped[str | None] = mapped_column(Text)


# No primary key: the logical key is (event_id, division_name), enforced by
# the unique index uq_event_division_run_limits_pk.
event_division_run_limits = Table(
    "event_division_run_limits",
    Base.metadata,
    Column("event_id", Text, nullable=False),
    Column("division_name", Text, nullable=False),
    Column("priority_run_limit", Integer, nullable=False),
)


class Session(Base):
    """A floor-trial session: check-in window, floor-trial window and caps."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    event_id: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    date: Mapped[str | None] = mapped_column(Text)
    checkin_opens_at: Mapped[int] = mapped_column(BigInteger)
    floor_trial_starts_at: Mapped[int] = mapped_column(BigInteger)
    floor_trial_ends_at: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(SESSION_STATUS, server_default="scheduled")
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(BigInteger)
    active_priority_max: Mapped[int] = mapped_column(Integer, server_default="6")
    active_non_priority_max: Mapped[int] = mapped_column(Integer, server_default="4")


class SessionDivision(Base):
    """A division a session runs, with its priority flag and run limit."""

    __tablename__ = "session_divisions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    session_id: Mapped[str] = mapped_column(Text)
    division_name: Mapped[str] = mapped_column(Text)
    is_priority: Mapped[bool] = mapped_column(Boolean, server_default="false")
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0")
    priority_run_limit: Mapped[int] = mapped_column(Integer, server_default="0")


class Checkin(Base):
    """A check-in: one entity, one song, one division, in one session."""

    __tablename__ = "checkins"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    session_id: Mapped[str] = mapped_column(Text)
    division_name: Mapped[str] = mapped_column(Text)
    entity_pair_id: Mapped[str | None] = mapped_column(Text)
    entity_solo_user_id: Mapped[str | None] = mapped_column(Text)
    song_id: Mapped[str] = mapped_column(Text)
    submitted_by_user_id: Mapped[str] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    initial_queue: Mapped[str] = mapped_column(INITIAL_QUEUE)
    created_at: Mapped[int] = mapped_column(BigInteger)
    entity_managed_partnership_id: Mapped[str | None] = mapped_column(Text)


class QueueEntry(Base):
    """A live place in a session's priority, non-priority or active queue."""

    __tablename__ = "queue_entries"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    checkin_id: Mapped[str] = mapped_column(Text)
    session_id: Mapped[str] = mapped_column(Text)
    queue_type: Mapped[str] = mapped_column(QUEUE_TYPE)
    position: Mapped[int] = mapped_column(Integer)
    entered_queue_at: Mapped[int] = mapped_column(BigInteger)
    entity_pair_id: Mapped[str | None] = mapped_column(Text)
    entity_solo_user_id: Mapped[str | None] = mapped_column(Text)
    entity_managed_partnership_id: Mapped[str | None] = mapped_column(Text)


class QueueEvent(Base):
    """One movement in a session's queues, for the audit trail."""

    __tablename__ = "queue_events"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    session_id: Mapped[str] = mapped_column(Text)
    checkin_id: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(QUEUE_EVENT_ACTION)
    from_queue: Mapped[str | None] = mapped_column(QUEUE_TYPE)
    from_position: Mapped[int | None] = mapped_column(Integer)
    to_queue: Mapped[str | None] = mapped_column(QUEUE_TYPE)
    to_position: Mapped[int | None] = mapped_column(Integer)
    actor_user_id: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(BigInteger)


class Run(Base):
    """A completed floor-trial run."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    checkin_id: Mapped[str] = mapped_column(Text)
    session_id: Mapped[str] = mapped_column(Text)
    event_id: Mapped[str | None] = mapped_column(Text)
    division_name: Mapped[str] = mapped_column(Text)
    entity_pair_id: Mapped[str | None] = mapped_column(Text)
    entity_solo_user_id: Mapped[str | None] = mapped_column(Text)
    song_id: Mapped[str] = mapped_column(Text)
    completed_at: Mapped[int] = mapped_column(BigInteger)
    completed_by_user_id: Mapped[str] = mapped_column(Text)
    entity_managed_partnership_id: Mapped[str | None] = mapped_column(Text)


class DriveJob(Base):
    """A pending or finished Drive operation (copy, trash, rename)."""

    __tablename__ = "drive_jobs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    submission_id: Mapped[str | None] = mapped_column(Text)
    file_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    next_attempt_at: Mapped[int] = mapped_column(BigInteger)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)


class SongUpload(Base):
    """An uploaded song's bytes and build progress, until its build finishes.

    This service's own table (migration 003), not deejaytools-api's: it makes
    the background build durable. See services/song_builds.py.
    """

    __tablename__ = "song_uploads"

    song_id: Mapped[str] = mapped_column(Text, primary_key=True)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    mime_type: Mapped[str] = mapped_column(Text)
    original_filename: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    next_attempt_at: Mapped[int] = mapped_column(BigInteger)
    last_error: Mapped[str | None] = mapped_column(Text)
    claim_id: Mapped[str | None] = mapped_column(Text)
    season_year: Mapped[str | None] = mapped_column(Text)
    processed_filename: Mapped[str | None] = mapped_column(Text)
    drive_file_id: Mapped[str | None] = mapped_column(Text)
    drive_folder_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)
