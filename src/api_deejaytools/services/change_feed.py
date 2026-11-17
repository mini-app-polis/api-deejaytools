"""The activity feed's capture: what each person changed, kept for Discord.

Every change a request makes is written to ``activity_feed`` (migration 016)
in the change's own transaction, and the scheduler posts each person's rows
as one message once they have been quiet for a minute
(``services/activity_feed.py``). The outbox is the point: the API runs
several uvicorn workers, and a burst gathered in one worker's memory
arrived as one message or several depending on where its requests landed.
In the table, one person's minute is one message whichever worker wrote it,
and a rolled-back change leaves nothing behind.

Two ways in:

- **Captured.** SQLAlchemy listeners write a row for every ORM insert,
  update and delete a request flushes, with what the message needs to say
  about it (``SNAPSHOT_COLUMNS``) and, for an update, which columns changed.
  A route added later is covered without anyone remembering to cover it.
- **Recorded** (:func:`record`). Core ``update()`` and ``delete()`` write
  rows the session never loads, and a statement's rows are unknown when it
  runs, so the routes that use them say what they did themselves, from the
  row they loaded first. ``covers`` names the tables a record speaks for: a
  song delete covers the submissions it cascades to, so they are not listed
  again. A statement no route described is kept as a ``bulk`` row, so it is
  still said, by its table, rather than lost.

Only inside a request: ``begin`` (the activity middleware) opens the
request's capture and ``end`` closes it, so a build or a job the request
starts in the background, which inherits the context, is not taken for the
person's own change. Background work that is news records it with an
explicit actor (:func:`record_for`, "song added").

What is kept is what the message says, never more: names, titles,
divisions, the names of the columns an edit changed. Never an email, a note
or a message body — the channel is a chat room.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import event, func, or_, select, text, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from ..clock import now_ms
from ..models import ActivityFeedEntry

CREATED = "created"
UPDATED = "updated"
DELETED = "deleted"
BULK = "bulk"
#: A song whose Drive build committed: "song added".
ADDED = "added"
#: A route's one line for many rows (an import of a registration list).
SUMMARY = "summary"

#: Columns kept for each table, when its rows are captured or recorded: what
#: the message names the row by. A table not listed keeps none, and is said
#: by its name and a count. Never an email, a note or a body.
SNAPSHOT_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "songs": (
        "id",
        "user_id",
        "partner_id",
        "managed_partnership_id",
        "display_name",
        "routine_name",
        "division",
    ),
    "event_song_submissions": (
        "id",
        "event_id",
        "song_id",
        "submitted_by_user_id",
        "division",
        "round",
    ),
    "partners": ("id", "first_name", "last_name", "kind"),
    "teams": ("id", "identifier"),
    "managed_partnerships": (
        "id",
        "leader_first_name",
        "leader_last_name",
        "follower_first_name",
        "follower_last_name",
    ),
    "users": ("id", "first_name", "last_name"),
    "events": ("id", "name", "start_date", "end_date"),
    "sessions": ("id", "event_id", "name", "status"),
    "event_registrations": (
        "id",
        "event_id",
        "division",
        "dancer_1",
        "dancer_2",
        "handled",
        "scratched",
        "linked_label",
    ),
    "event_staff_grants": ("event_id", "user_id", "role"),
}

#: Columns whose change is not worth naming: every edit sets them.
UNNAMED_COLUMNS = frozenset({"updated_at", "created_at"})

#: Where a session keeps the rows captured in a flush, until the flush ends
#: and they can be added (a flush cannot add to its own session).
_PENDING_KEY = "api_deejaytools.change_feed.pending"


@dataclass
class _Capture:
    """One request's capture: whose changes, and which tables to leave out."""

    request: Any
    request_id: str
    ignored: frozenset[str]
    open: bool = True

    def actor(self) -> str | None:
        caller = getattr(getattr(self.request, "state", None), "caller", None)
        return str(caller) if caller else None


#: The request's capture. A context variable, so the ORM listeners see it:
#: Starlette copies the context into the downstream task, and SQLAlchemy's
#: asyncio bridge copies it into the greenlet the listeners run in.
_current: ContextVar[_Capture | None] = ContextVar("change_feed", default=None)


def begin(request: Any, *, ignored: Collection[str] = ()) -> Token[_Capture | None]:
    """Open ``request``'s capture; pass the token to :func:`end`."""
    return _current.set(
        _Capture(
            request=request,
            request_id=str(uuid.uuid4()),
            ignored=frozenset(ignored) | {ActivityFeedEntry.__tablename__},
        )
    )


def end(token: Token[_Capture | None]) -> None:
    """Close the request's capture.

    Closed, not only unset: a background task the request started holds a
    copy of the context, and its writes are not this person's.
    """
    capture = _current.get()
    if capture is not None:
        capture.open = False
    _current.reset(token)


def _active() -> _Capture | None:
    capture = _current.get()
    return capture if capture is not None and capture.open else None


def snapshot(obj: Any) -> dict[str, Any]:
    """What the message names ``obj`` by: its ``SNAPSHOT_COLUMNS``."""
    table = getattr(type(obj), "__tablename__", None)
    columns = SNAPSHOT_COLUMNS.get(str(table), ())
    # Only what is already loaded: a lazy load here would be IO in a flush.
    loaded = sa_inspect(obj).dict
    return {column: loaded[column] for column in columns if column in loaded}


def _changed_columns(obj: Any) -> list[str]:
    state = sa_inspect(obj)
    return sorted(
        attr.key
        for attr in state.mapper.column_attrs
        if attr.key not in UNNAMED_COLUMNS
        and state.attrs[attr.key].history.has_changes()
    )


def _entry(
    capture_actor: str,
    request_id: str | None,
    table: str,
    op: str,
    facts: Mapping[str, Any],
) -> ActivityFeedEntry:
    return ActivityFeedEntry(
        actor_id=capture_actor,
        request_id=request_id,
        table_name=table,
        op=op,
        facts=dict(facts),
        created_at=now_ms(),
    )


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


@event.listens_for(Session, "after_flush")
def _capture_flush(session: Session, _flush_context: Any) -> None:
    """Note each row the flush wrote, while the session still describes it."""
    capture = _active()
    if capture is None:
        return
    actor = capture.actor()
    if actor is None:
        return
    pending = session.info.setdefault(_PENDING_KEY, [])

    def keep(obj: Any, op: str, facts: dict[str, Any]) -> None:
        table = getattr(type(obj), "__tablename__", None)
        if not table or table in capture.ignored:
            return
        pending.append(_entry(actor, capture.request_id, str(table), op, facts))

    for obj in session.new:
        keep(obj, CREATED, snapshot(obj))
    for obj in session.deleted:
        keep(obj, DELETED, snapshot(obj))
    for obj in session.dirty:
        if not session.is_modified(obj, include_collections=False):
            continue
        fields = _changed_columns(obj)
        if fields:
            keep(obj, UPDATED, {**snapshot(obj), "fields": fields})


@event.listens_for(Session, "after_flush_postexec")
def _add_captured(session: Session, _flush_context: Any) -> None:
    """Add the rows the flush noted. The flush that commits loops, so they are
    written in the same transaction as what they describe."""
    pending = session.info.pop(_PENDING_KEY, None)
    if pending:
        session.add_all(pending)


@event.listens_for(Session, "do_orm_execute")
def _capture_statement(state: Any) -> None:
    """Keep a Core write no route described, by its table.

    It runs before the statement, so its rows are unknown: one statement is
    one ``bulk`` row. A route that described the change covers it.
    """
    if not (state.is_insert or state.is_update or state.is_delete):
        return
    capture = _active()
    if capture is None:
        return
    actor = capture.actor()
    table = getattr(getattr(state.statement, "table", None), "name", None)
    if actor is None or not table or table in capture.ignored:
        return
    state.session.add(_entry(actor, capture.request_id, str(table), BULK, {}))


@event.listens_for(Session, "after_soft_rollback")
def _forget_pending(session: Session, _previous_transaction: Any) -> None:
    session.info.pop(_PENDING_KEY, None)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def record(
    session: AsyncSession | Session,
    table: str,
    op: str,
    facts: Mapping[str, Any] | None = None,
    *,
    fields: Iterable[str] = (),
    covers: Iterable[str] = (),
) -> None:
    """Say what a Core write did, in the request's transaction.

    For a write the listeners cannot describe: an ``update()`` or
    ``delete()`` of rows the session never loaded. ``facts`` are taken from
    the row the route loaded first (:func:`snapshot`). ``covers`` names the
    tables this record speaks for, so their rows from the same request are
    not listed again. Does nothing outside a request's capture.
    """
    capture = _active()
    if capture is None:
        return
    actor = capture.actor()
    if actor is None:
        return
    body = dict(facts or {})
    if fields:
        body["fields"] = sorted(set(fields) - UNNAMED_COLUMNS)
    if covers:
        body["covers"] = sorted(set(covers))
    _sync(session).add(_entry(actor, capture.request_id, table, op, body))


def record_for(
    session: AsyncSession | Session,
    actor_id: str,
    table: str,
    op: str,
    facts: Mapping[str, Any],
) -> None:
    """Record a change made by work with no request, for ``actor_id``."""
    _sync(session).add(_entry(actor_id, None, table, op, facts))


def _sync(session: AsyncSession | Session) -> Session:
    return session.sync_session if isinstance(session, AsyncSession) else session


# ---------------------------------------------------------------------------
# Claiming rows to post
# ---------------------------------------------------------------------------

#: How long a person stays quiet before their changes are posted.
QUIET_MS = 60_000

#: The longest a change waits: someone busy for ten minutes still has their
#: first changes posted after five.
MAX_WAIT_MS = 5 * 60_000

#: How long posted rows are kept, for an operator looking back.
KEEP_MS = 7 * 24 * 60 * 60_000

#: The advisory lock one claim takes, so two workers never split one
#: person's changes between two messages.
_LOCK_KEY = 160_016


@dataclass
class Entry:
    """One claimed row, as the renderer reads it."""

    id: int
    actor_id: str
    request_id: str | None
    table: str
    op: str
    facts: dict[str, Any] = field(default_factory=dict)
    created_at: int = 0


async def claim_due(
    maker: async_sessionmaker[AsyncSession], *, now: int | None = None
) -> dict[str, list[Entry]]:
    """Mark the rows of everyone who has been quiet long enough as posted.

    Returns them by person, oldest first. Marked before they are sent:
    delivery is fire and forget, and a row is never posted twice. Prunes
    rows posted more than ``KEEP_MS`` ago. Another worker claiming at the
    same moment gets nothing.
    """
    now = now_ms() if now is None else now
    async with maker() as db:
        locked = (
            await db.execute(
                text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _LOCK_KEY}
            )
        ).scalar()
        if not locked:
            await db.rollback()
            return {}
        due = (
            select(ActivityFeedEntry.actor_id)
            .where(ActivityFeedEntry.posted_at.is_(None))
            .group_by(ActivityFeedEntry.actor_id)
            .having(
                or_(
                    func.max(ActivityFeedEntry.created_at) <= now - QUIET_MS,
                    func.min(ActivityFeedEntry.created_at) <= now - MAX_WAIT_MS,
                )
            )
        )
        rows = (
            await db.execute(
                update(ActivityFeedEntry)
                .where(
                    ActivityFeedEntry.posted_at.is_(None),
                    ActivityFeedEntry.actor_id.in_(due),
                    ActivityFeedEntry.created_at <= now,
                )
                .values(posted_at=now)
                .returning(
                    ActivityFeedEntry.id,
                    ActivityFeedEntry.actor_id,
                    ActivityFeedEntry.request_id,
                    ActivityFeedEntry.table_name,
                    ActivityFeedEntry.op,
                    ActivityFeedEntry.facts,
                    ActivityFeedEntry.created_at,
                )
            )
        ).all()
        await db.execute(
            sql_delete(ActivityFeedEntry).where(
                ActivityFeedEntry.posted_at.is_not(None),
                ActivityFeedEntry.posted_at < now - KEEP_MS,
            )
        )
        await db.commit()
    by_actor: dict[str, list[Entry]] = {}
    for row in sorted(rows, key=lambda r: r.id):
        by_actor.setdefault(row.actor_id, []).append(
            Entry(
                id=row.id,
                actor_id=row.actor_id,
                request_id=row.request_id,
                table=row.table_name,
                op=row.op,
                facts=dict(row.facts or {}),
                created_at=row.created_at,
            )
        )
    return by_actor
