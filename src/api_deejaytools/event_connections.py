"""Who is connected to an event, and how their email is shown to its staff (ADR-0012).

An event's staff act only for the event's own people. A person is connected
to an event when they are:

- on a song submitted to it: the submitter, the song's owner, and the
  account linked to the song's partner;
- checked in at, or with a queue entry in, one of its sessions: the solo
  dancer, both sides of the pair (its leader, and the account linked to its
  partner), or the user who manages the managed partnership;
- on its staff (``event_staff_grants``).

Staff see every email masked (``mask_email``), on every event-scoped route;
only admins see emails whole (``email_for_caller``).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import CompoundSelect, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    Checkin,
    EventSongSubmission,
    EventStaffGrant,
    ManagedPartnership,
    Pair,
    Partner,
    QueueEntry,
    Song,
    User,
)
from .models import Session as FloorSession

MASK = "•••"


def mask_email(email: str | None) -> str | None:
    """An email with its local part hidden: ``k•••@gmail.com``.

    The local part's first character, three bullets whatever its length,
    then ``@`` and the whole domain. The domain starts after the last ``@``,
    so a quoted local part holding an ``@`` is hidden with the rest. No
    email, or one without a local part or a domain, is None: the address is
    never returned as it is.
    """
    if not email:
        return None
    local, at, domain = email.strip().rpartition("@")
    if not at or not local or not domain:
        return None
    return f"{local[0]}{MASK}@{domain}"


def email_for_caller(email: str | None, *, whole: bool) -> str | None:
    """``email`` as a caller may see it: whole for an admin, else masked."""
    return email if whole else mask_email(email)


def connected_user_ids(event_id: str) -> CompoundSelect:
    """The ``users.id`` of everyone connected to the event, once each."""
    submission = EventSongSubmission
    of_event = submission.event_id == event_id
    selects: list[Any] = [
        select(submission.submitted_by_user_id.label("user_id")).where(of_event),
        select(Song.user_id)
        .join(submission, submission.song_id == Song.id)
        .where(of_event),
        select(Partner.linked_user_id)
        .join(Song, Song.partner_id == Partner.id)
        .join(submission, submission.song_id == Song.id)
        .where(of_event),
        select(EventStaffGrant.user_id).where(EventStaffGrant.event_id == event_id),
    ]
    event_sessions = select(FloorSession.id).where(FloorSession.event_id == event_id)
    for entry in (Checkin, QueueEntry):
        in_event = entry.session_id.in_(event_sessions)
        selects += [
            select(entry.entity_solo_user_id).where(in_event),
            select(Pair.user_a_id)
            .join(entry, entry.entity_pair_id == Pair.id)
            .where(in_event),
            select(Partner.linked_user_id)
            .join(Pair, Pair.partner_b_id == Partner.id)
            .join(entry, entry.entity_pair_id == Pair.id)
            .where(in_event),
            select(ManagedPartnership.user_id)
            .join(entry, entry.entity_managed_partnership_id == ManagedPartnership.id)
            .where(in_event),
        ]
    return union(*selects)


async def is_connected(session: AsyncSession, event_id: str, user_id: str) -> bool:
    """Whether ``user_id`` is connected to the event."""
    row = (
        await session.execute(
            select(User.id)
            .where(User.id == user_id, User.id.in_(connected_user_ids(event_id)))
            .limit(1)
        )
    ).first()
    return row is not None
