"""``/v1/registration-reminders`` (docs/API.md).

The caller's own entries on events' registration lists that have no music
in yet: so My Content can tell a dancer we see them registered but have no
song for them, and offer the song they likely mean to send. The list is
behind ``deejaytools.submissions.read``, dismissing behind
``deejaytools.submissions.write``, the scopes every signed-in dancer holds.

An entry is the caller's when its dancers are, exactly and in either order
(``name_key``: case, accents and spacing ignored), one of the caller's
pairs: the caller with a partner in their address book, the caller with the
owner of a partner row linked to them, a managed partnership of theirs, or
the names of one of their own or shared songs. One shared name is never a
match, so a pro is not told about every entry naming them.

The rest follows ``GET /v1/admin/events/:id/registrations/songs-on-file``:
an entry is left out when it is scratched, handled or linked by hand, or a
song submitted to the event names its dancers exactly. Only events that are
not completed are looked at, and only entries the caller has not dismissed
("Not me"). The suggested song is the caller's best strong candidate, never
a possible one and never anyone else's.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Annotated, Any

import sentry_sdk
from fastapi import APIRouter, Depends, Path, Response
from fastapi.responses import JSONResponse
from mini_app_polis.logger import LOG_FAILURE, get_logger, with_log_prefix
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..auth import Caller, require_scope
from ..clock import now_ms
from ..database import get_db_session
from ..domain import full_name
from ..errors import (
    Envelope,
    ErrorCode,
    ErrorResponse,
    api_error,
    error_body,
    success_list,
)
from ..models import (
    Event,
    EventRegistration,
    EventSongSubmission,
    ManagedPartnership,
    Partner,
    RegistrationReminderDismissal,
    Song,
    User,
)
from ..song_records import owned_or_shared
from .admin_event_registrations import name_key
from .admin_registration_songs import (
    SongOnFileData,
    entry_names,
    event_season,
    name_set,
    rank_candidates,
    songs_on_file,
)
from .events import compute_status

logger = get_logger()

router = APIRouter(prefix="/v1/registration-reminders", tags=["registration-reminders"])

NameSet = frozenset[tuple[str, int]]


class RegistrationReminderData(BaseModel):
    """One of the caller's registration entries with no music in yet."""

    registration_id: str = Field(..., description="Registration entry id.")
    event_id: str = Field(..., description="Event id.")
    event_name: str = Field(..., description="Event name.")
    event_start_date: str = Field(..., description="Event's first day.")
    event_status: str = Field(..., description="upcoming or active.")
    division: str = Field(..., description="The division as the registration names it.")
    dancer_1: str = Field(..., description="First dancer, as registered.")
    dancer_2: str | None = Field(None, description="Second dancer, as registered.")
    song: SongOnFileData | None = Field(
        None,
        description=(
            "The caller's best song for it (a strong match, not yet submitted "
            "to the event), or null when they have none on file."
        ),
    )


class RegistrationRemindersResponse(Envelope[list[RegistrationReminderData]]):
    """The caller's entries with no music in, by event start then list order."""

    data: list[RegistrationReminderData] = Field(..., description="The entries.")


def _pair(a: str, b: str) -> NameSet | None:
    names = tuple(k for k in (name_key(a), name_key(b)) if k)
    return name_set(names) if len(names) == 2 else None


async def caller_pairs(
    session: AsyncSession, user_id: str, songs: list[Any]
) -> set[NameSet]:
    """The name sets an entry of the caller's could be registered under."""
    me = await session.get(User, user_id)
    my_name = (
        full_name(me.first_name, me.last_name) or (me.display_name or "")
        if me is not None
        else ""
    )
    pairs: set[NameSet] = {name_set(s.names) for s in songs if s.names}

    for p_first, p_last in (
        await session.execute(
            select(Partner.first_name, Partner.last_name).where(
                Partner.user_id == user_id, Partner.kind == "partner"
            )
        )
    ).all():
        found = _pair(my_name, full_name(p_first, p_last))
        if found:
            pairs.add(found)

    owner = aliased(User, name="partner_owner")
    for first, last, display in (
        await session.execute(
            select(owner.first_name, owner.last_name, owner.display_name)
            .select_from(Partner)
            .join(owner, owner.id == Partner.user_id)
            .where(Partner.linked_user_id == user_id, Partner.user_id != user_id)
        )
    ).all():
        found = _pair(full_name(first, last) or (display or ""), my_name)
        if found:
            pairs.add(found)

    for lf, ll, ff, fl in (
        await session.execute(
            select(
                ManagedPartnership.leader_first_name,
                ManagedPartnership.leader_last_name,
                ManagedPartnership.follower_first_name,
                ManagedPartnership.follower_last_name,
            ).where(
                ManagedPartnership.user_id == user_id,
                ManagedPartnership.deleted_at.is_(None),
            )
        )
    ).all():
        found = _pair(full_name(lf, ll), full_name(ff, fl))
        if found:
            pairs.add(found)
    return pairs


async def registration_reminders(
    session: AsyncSession, user_id: str
) -> list[dict[str, Any]]:
    """The caller's open, undismissed entries on not-completed events."""
    events = {
        e.id: e
        for e in (
            await session.execute(
                select(Event).where(
                    Event.id.in_(select(EventRegistration.event_id).distinct())
                )
            )
        )
        .scalars()
        .all()
        if compute_status(e.start_date, e.end_date, e.timezone) != "completed"
    }
    if not events:
        return []

    dismissed = select(RegistrationReminderDismissal.registration_id).where(
        RegistrationReminderDismissal.user_id == user_id
    )
    entries = (
        (
            await session.execute(
                select(EventRegistration)
                .where(
                    EventRegistration.event_id.in_(events),
                    EventRegistration.scratched.is_(False),
                    EventRegistration.handled.is_(False),
                    EventRegistration.linked_label.is_(None),
                    EventRegistration.id.not_in(dismissed),
                )
                .order_by(EventRegistration.position)
            )
        )
        .scalars()
        .all()
    )
    if not entries:
        return []

    mine = await songs_on_file(session, owned_or_shared(user_id))
    pairs = await caller_pairs(session, user_id, mine)
    entries = [
        e for e in entries if name_set(entry_names(e.dancer_1, e.dancer_2)) in pairs
    ]
    if not entries:
        return []

    # Who already has music in each event: the names of every song in it.
    event_ids = {e.event_id for e in entries}
    in_event: dict[str, set[str]] = defaultdict(set)
    for song_id, event_id in (
        await session.execute(
            select(EventSongSubmission.song_id, EventSongSubmission.event_id).where(
                EventSongSubmission.event_id.in_(event_ids)
            )
        )
    ).all():
        in_event[event_id].add(song_id)
    submitted_ids = set().union(*in_event.values())
    covered: dict[str, set[NameSet]] = defaultdict(set)
    if submitted_ids:
        for s in await songs_on_file(session, Song.id.in_(submitted_ids)):
            for event_id, ids in in_event.items():
                if s.song_id in ids:
                    covered[event_id].add(name_set(s.names))

    # Where the caller's songs went, for ranking: (event id, name, season).
    mine_ids = [s.song_id for s in mine]
    went: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    if mine_ids:
        for song_id, event_id, name, season_year, start_date in (
            await session.execute(
                select(
                    EventSongSubmission.song_id,
                    Event.id,
                    Event.name,
                    Event.season_year,
                    Event.start_date,
                )
                .join(Event, Event.id == EventSongSubmission.event_id)
                .where(EventSongSubmission.song_id.in_(mine_ids))
            )
        ).all():
            went[song_id].append(
                (event_id, name, event_season(season_year, start_date))
            )

    out = []
    for entry in entries:
        if (
            name_set(entry_names(entry.dancer_1, entry.dancer_2))
            in covered[entry.event_id]
        ):
            continue
        event = events[entry.event_id]
        season = event_season(event.season_year, event.start_date)
        # The other events of this one's season each song is submitted to.
        submitted = {
            song_id: sorted(
                {n for eid, n, sy in places if eid != event.id and sy == season}
            )
            for song_id, places in went.items()
        }
        candidates = rank_candidates(
            entry,
            [s for s in mine if s.song_id not in in_event[entry.event_id]],
            season_year=season,
            submitted=submitted,
        )
        song = next((c for c in candidates if c["match"] == "strong"), None)
        out.append(
            {
                "registration_id": entry.id,
                "event_id": event.id,
                "event_name": event.name,
                "event_start_date": event.start_date,
                "event_status": compute_status(
                    event.start_date, event.end_date, event.timezone
                ),
                "division": entry.division,
                "dancer_1": entry.dancer_1,
                "dancer_2": entry.dancer_2,
                "song": song,
            }
        )
    out.sort(key=lambda r: (r["event_start_date"], r["event_name"]))
    return out


def _internal() -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content=error_body(ErrorCode.INTERNAL, "Internal server error"),
    )


AUTH: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid token."},
    403: {"model": ErrorResponse, "description": "Lacks the scope."},
}


@router.get(
    "",
    response_model=RegistrationRemindersResponse,
    summary="The caller's registrations with no music in",
    description=(
        "Entries on registration lists of events not yet completed whose "
        "dancers are exactly one of the caller's pairs (the caller and a "
        "partner, a managed partnership, or the names of one of their songs), "
        "with no song submitted to the event naming them, not scratched, "
        "handled or linked by hand, and not dismissed by the caller. Each "
        "carries the caller's best strongly matching song not yet submitted "
        "to the event, or null. Requires deejaytools.submissions.read."
    ),
    responses={
        **AUTH,
        500: {"model": ErrorResponse, "description": "The query failed."},
    },
)
async def list_registration_reminders(
    caller: Caller = Depends(require_scope("deejaytools.submissions.read")),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """List the caller's registrations with no music in."""
    try:
        return success_list(await registration_reminders(session, caller.user_id))
    except SQLAlchemyError as exc:
        sentry_sdk.capture_exception(exc)
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"registration_reminders_failed user={caller.user_id}: {exc!r}",
            )
        )
        return _internal()


RegistrationId = Annotated[str, Path(description="Registration entry id.")]


@router.post(
    "/{id}/dismiss",
    response_model=None,  # 204: no body
    status_code=204,
    response_class=Response,
    summary="Dismiss a registration reminder (not me)",
    description=(
        "The caller says the entry is not theirs: it is left out of their "
        "reminders from now on (other people's are unaffected). Idempotent. "
        "Requires deejaytools.submissions.write."
    ),
    responses={
        **AUTH,
        404: {"model": ErrorResponse, "description": "No such entry."},
        500: {"model": ErrorResponse, "description": "The write failed."},
    },
)
async def dismiss_registration_reminder(
    id: RegistrationId,
    caller: Caller = Depends(require_scope("deejaytools.submissions.write")),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Dismiss one of the caller's registration reminders."""
    if await session.get(EventRegistration, id) is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Registration not found")
    try:
        await session.execute(
            insert(RegistrationReminderDismissal)
            .values(
                registration_id=id,
                user_id=caller.user_id,
                created_at=now_ms(),
            )
            .on_conflict_do_nothing()
        )
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        sentry_sdk.capture_exception(exc)
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"registration_reminder_dismiss_failed user={caller.user_id} "
                f"registration={id}: {exc!r}",
            )
        )
        return _internal()
    return Response(status_code=204)
