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

``/v1/events/{id}/people/{user_id}/partners``: one such person's partners
(kind ``partner``), for a staff upload on their behalf. Event-scoped
``deejaytools.delegation.act``, the scope the upload itself needs, and only
for a person connected to the event: the admin-wide list
(``/v1/admin/users/{id}/partners``) stays admin-only.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import Caller, EventRef, is_admin, require_event_scope
from ..database import get_db_session
from ..errors import Envelope, ErrorCode, ErrorResponse, api_error, success_list
from ..event_connections import connected_user_ids, mask_email
from ..models import (
    Event,
    Partner,
    User,
)
from ..validation import ZodModel
from ..zod_types import QueryStr, trimmed, zod_query
from .partners import PartnerListResponse, map_partner

router = APIRouter(prefix="/v1/events", tags=["event-people"])

THIS_EVENT = EventRef(source="path", name="id")

MIN_QUERY_LENGTH = 2
RESULT_LIMIT = 20


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


class EventPeopleResponse(Envelope[list[EventPersonData]]):
    """The people found, by name."""

    data: list[EventPersonData] = Field(..., description="The people.")


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
    _caller: Caller = Depends(
        require_event_scope("deejaytools.entries.read", THIS_EVENT)
    ),
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


@router.get(
    "/{id}/people/{user_id}/partners",
    response_model=PartnerListResponse,
    summary="A person's partners, for an upload on their behalf",
    description=(
        "The partners (kind partner) of one person connected to the event, by "
        "last then first name: the list a staff upload on their behalf picks "
        "partner_id from. Emails are masked and linked accounts withheld "
        "unless the caller is an admin. Requires deejaytools.delegation.act "
        "on the event."
    ),
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token."},
        403: {"model": ErrorResponse, "description": "Lacks the scope on this event."},
        404: {
            "model": ErrorResponse,
            "description": "No such event, or the person is not connected to it.",
        },
    },
)
async def list_event_person_partners(
    id: Annotated[str, Path(description="Event id.")],
    user_id: Annotated[str, Path(description="users.id of the person.")],
    caller: Caller = Depends(
        require_event_scope("deejaytools.delegation.act", THIS_EVENT)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """A person's partners, for an upload on their behalf at this event."""
    if await session.get(Event, id) is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Event not found")
    connected = (
        await session.execute(
            select(User.id)
            .where(User.id == user_id, User.id.in_(connected_user_ids(id)))
            .limit(1)
        )
    ).first()
    if connected is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Person not found at this event")
    rows = (
        (
            await session.execute(
                select(Partner)
                .where(Partner.user_id == user_id, Partner.kind == "partner")
                .order_by(Partner.last_name.asc(), Partner.first_name.asc())
            )
        )
        .scalars()
        .all()
    )
    whole = is_admin(caller)
    return success_list([_partner_for_staff(r, whole=whole) for r in rows])


def _partner_for_staff(row: Any, *, whole: bool) -> dict[str, Any]:
    """A partner as the event's staff see it: the email masked and the linked
    account withheld, unless the caller is an admin."""
    partner = map_partner(row)
    if whole:
        return partner
    return {
        **partner,
        "email": mask_email(partner["email"]),
        "linked_user_id": None,
    }
