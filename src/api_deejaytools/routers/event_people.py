"""``/v1/events/{id}/people``: find a person connected to an event, by name (ADR-0012).

The web portals' person picker: an event's staff type part of a name and
pick the person. Event-scoped ``deejaytools.entries.read``, as the event's
entries: the scope held through a role (an admin's) allows on any event, a
grant of it on ``event:<id>`` allows on that event only.

Only people connected to the event can be found, never every user:

- on a song submitted to it: the submitter, the song's owner, and the
  account linked to the song's partner;
- checked in at, or with a queue entry in, one of its sessions: the solo
  dancer, both sides of the pair (its leader, and the account linked to its
  partner), or the user who manages the managed partnership;
- on its staff (``event_staff_grants``).

The match is on the person's name only, never their email, and the email
goes out masked (``mask_email``).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field
from sqlalchemy import CompoundSelect, func, or_, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import Caller, EventRef, require_event_scope
from ..database import get_db_session
from ..errors import ErrorCode, ErrorResponse, ListMeta, api_error, success_list
from ..models import (
    Checkin,
    Event,
    EventSongSubmission,
    EventStaffGrant,
    ManagedPartnership,
    Pair,
    Partner,
    QueueEntry,
    Song,
    User,
)
from ..models import Session as FloorSession
from ..validation import ZodModel
from ..zod_types import QueryStr, trimmed, zod_query

router = APIRouter(prefix="/v1/events", tags=["event-people"])

ENTRIES_READ_SCOPE = "deejaytools.entries.read"
THIS_EVENT = EventRef(source="path", name="id")

MIN_QUERY_LENGTH = 2
RESULT_LIMIT = 20
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


class PeopleQuery(ZodModel):
    """Query of ``GET /v1/events/{id}/people``."""

    q: Annotated[QueryStr, trimmed(min_length=MIN_QUERY_LENGTH)] = Field(
        ...,
        description=(
            "Part of the person's name, case-insensitive; at least "
            f"{MIN_QUERY_LENGTH} characters once trimmed."
        ),
    )


class EventPersonData(BaseModel):
    """A person connected to the event, as the picker shows them."""

    user_id: str = Field(..., description="users.id of the person.")
    display_name: str = Field(
        ..., description="Their display name, or else first and last name."
    )
    email_masked: str | None = Field(
        None, description="Their email with the local part hidden: k•••@gmail.com."
    )


class EventPeopleResponse(BaseModel):
    """The people found, by name."""

    data: list[EventPersonData] = Field(..., description="The people.")
    meta: ListMeta = Field(..., description="Response metadata.")


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


@router.get(
    "/{id}/people",
    response_model=EventPeopleResponse,
    summary="Find a person at an event, by name",
    description=(
        "People connected to the event (its submissions, its sessions' "
        "check-ins and queues, its staff) whose name contains q, "
        f"case-insensitive, by name, at most {RESULT_LIMIT}. Emails are "
        "masked. Requires deejaytools.entries.read on the event."
    ),
    responses={
        400: {"model": ErrorResponse, "description": "Missing or short q."},
        401: {"model": ErrorResponse, "description": "Missing or invalid token."},
        403: {"model": ErrorResponse, "description": "Lacks the scope on this event."},
        404: {"model": ErrorResponse, "description": "No such event."},
    },
)
async def find_event_people(
    id: Annotated[str, Path(description="Event id.")],
    _caller: Caller = Depends(require_event_scope(ENTRIES_READ_SCOPE, THIS_EVENT)),
    query: PeopleQuery = Depends(zod_query(PeopleQuery)),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Find people connected to the event by name."""
    if await session.get(Event, id) is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Event not found")
    full_name = func.concat_ws(" ", User.first_name, User.last_name)
    name = func.coalesce(
        func.nullif(func.btrim(User.display_name), ""),
        func.nullif(func.btrim(full_name), ""),
    )
    rows = (
        await session.execute(
            select(User.id, name.label("display_name"), User.email)
            .where(
                User.id.in_(connected_user_ids(id)),
                or_(
                    User.display_name.icontains(query.q, autoescape=True),
                    full_name.icontains(query.q, autoescape=True),
                ),
            )
            .order_by(func.lower(name), User.id)
            .limit(RESULT_LIMIT)
        )
    ).all()
    return success_list(
        [
            {
                "user_id": r.id,
                "display_name": r.display_name,
                "email_masked": mask_email(r.email),
            }
            for r in rows
        ]
    )
