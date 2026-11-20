"""``/v1/registration-reminders`` (docs/API.md).

The caller's own entries on events' registration lists that have no music
in yet, or that only nearly name them: so My Content can tell a dancer we
see them registered but have no song for them, offer the song they likely
mean to send, and let them confirm an entry whose spelling is off. The list
is behind ``deejaytools.submissions.read``; confirming and dismissing behind
``deejaytools.submissions.write``, the scopes every signed-in dancer holds.

The caller's pairs are the caller with a partner in their address book, the
owner of a partner row linked to the caller with that row's names, a managed
partnership of theirs, and the names of one of their own or shared songs;
each with the partnership label its songs carry. An entry is the caller's:

- **exact** when its dancers are exactly one of those pairs, in either order
  (``name_key``: case, accents and spacing ignored), or it is linked by hand
  to one of their labels;
- **possible** when one name is exact and the other close (a typo or a
  shortened first name, ``close_name``), as the admin's look-alike match has
  it. The caller confirms it ("That's me"), which links it to their label,
  as an admin's link would; one shared name alone is never a match.

An exact entry is left out once music is in for it (a song submitted to the
event names its dancers exactly, or carries its linked label). Entries that
are scratched, handled, linked to someone else's label, dismissed by the
caller ("Not me"), or on completed events are left out. The suggested song
is the caller's best strong candidate not yet submitted, never anyone
else's.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Annotated, Any, Literal

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
from ..labels import partnership_label
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
from ..services import change_feed
from ..song_records import owned_or_shared
from .admin_event_registrations import name_key
from .admin_registration_songs import (
    SongOnFile,
    SongOnFileData,
    entry_names,
    event_season,
    match_names,
    name_set,
    rank_candidates,
    songs_on_file,
)
from .events import compute_status

logger = get_logger()

router = APIRouter(prefix="/v1/registration-reminders", tags=["registration-reminders"])

NameSet = frozenset[tuple[str, int]]
Match = Literal["exact", "possible"]


class RegistrationReminderData(BaseModel):
    """One of the caller's registration entries: no music in, or not yet confirmed."""

    registration_id: str = Field(..., description="Registration entry id.")
    event_id: str = Field(..., description="Event id.")
    event_name: str = Field(..., description="Event name.")
    event_start_date: str = Field(..., description="Event's first day.")
    event_status: str = Field(..., description="upcoming or active.")
    division: str = Field(..., description="The division as the registration names it.")
    dancer_1: str = Field(..., description="First dancer, as registered.")
    dancer_2: str | None = Field(None, description="Second dancer, as registered.")
    match: Match = Field(
        ...,
        description=(
            "exact: the entry names one of the caller's pairs, or is linked to "
            "their label; possible: one name exactly and the other closely, "
            "for the caller to confirm or dismiss."
        ),
    )
    partnership_label: str = Field(
        ...,
        description=(
            "The caller's partnership it matched, as their songs are labelled; "
            "confirming links the entry to it."
        ),
    )
    has_music: bool = Field(
        ...,
        description=(
            "possible only: that partnership already has a song in the event, "
            "so confirming settles the entry. Always false for exact."
        ),
    )
    song: SongOnFileData | None = Field(
        None,
        description=(
            "The caller's best song for it (a strong match, not yet submitted "
            "to the event), or null when they have none on file or music is "
            "already in."
        ),
    )


class RegistrationRemindersResponse(Envelope[list[RegistrationReminderData]]):
    """The caller's entries, by event start then list order."""

    data: list[RegistrationReminderData] = Field(..., description="The entries.")


@dataclass(frozen=True)
class Pair:
    """One of the caller's partnerships: its names and the label its songs carry."""

    names: tuple[str, ...]
    label: str


def plain(text: str) -> str:
    """``text`` as the web app's ``sameLabel`` compares labels: case, accents
    and punctuation ignored."""
    decomposed = unicodedata.normalize("NFKD", text)
    bare = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.sub(r"[\W_]+", " ", bare.lower()).split())


def same_label(a: str | None, b: str | None) -> bool:
    """Whether two partnership labels are the same, as an entry's link compares them."""
    return bool(a) and bool(b) and plain(a or "") == plain(b or "")


def _keys(a: str | None, b: str | None) -> tuple[str, ...]:
    return tuple(k for k in (name_key(a), name_key(b)) if k)


async def caller_pairs(
    session: AsyncSession, user_id: str, songs: list[SongOnFile]
) -> dict[NameSet, Pair]:
    """The caller's partnerships, by the name set an entry would carry."""
    pairs: dict[NameSet, Pair] = {}

    def add(names: tuple[str, ...], label: str, *, single: bool = False) -> None:
        # One name alone only from a song of theirs (a solo or a team): never
        # from the caller's own name, which would match every entry naming them.
        if label and (len(names) == 2 or (single and len(names) == 1)):
            pairs[name_set(names)] = Pair(names, label)

    me = await session.get(User, user_id)
    me_first = me.first_name if me is not None else None
    me_last = me.last_name if me is not None else None
    if full_name(me_first, me_last):
        for first, last, role in (
            await session.execute(
                select(
                    Partner.first_name, Partner.last_name, Partner.partner_role
                ).where(Partner.user_id == user_id, Partner.kind == "partner")
            )
        ).all():
            add(
                _keys(full_name(me_first, me_last), full_name(first, last)),
                partnership_label(
                    managed_leader_first=None,
                    managed_leader_last=None,
                    managed_follower_first=None,
                    managed_follower_last=None,
                    owner_first=me_first,
                    owner_last=me_last,
                    partner_first=first,
                    partner_last=last,
                    partner_kind="partner",
                    partner_role=role,
                ),
            )

    # A partner row in someone else's book, linked to the caller: named as
    # that owner names them, as the owner's songs with them are labelled.
    owner = aliased(User, name="partner_owner")
    for o_first, o_last, first, last, role in (
        await session.execute(
            select(
                owner.first_name,
                owner.last_name,
                Partner.first_name,
                Partner.last_name,
                Partner.partner_role,
            )
            .select_from(Partner)
            .join(owner, owner.id == Partner.user_id)
            .where(
                Partner.linked_user_id == user_id,
                Partner.user_id != user_id,
                Partner.kind == "partner",
            )
        )
    ).all():
        add(
            _keys(full_name(o_first, o_last), full_name(first, last)),
            partnership_label(
                managed_leader_first=None,
                managed_leader_last=None,
                managed_follower_first=None,
                managed_follower_last=None,
                owner_first=o_first,
                owner_last=o_last,
                partner_first=first,
                partner_last=last,
                partner_kind="partner",
                partner_role=role,
            ),
        )

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
        add(
            _keys(full_name(lf, ll), full_name(ff, fl)),
            partnership_label(
                managed_leader_first=lf,
                managed_leader_last=ll,
                managed_follower_first=ff,
                managed_follower_last=fl,
                owner_first=None,
                owner_last=None,
                partner_first=None,
                partner_last=None,
            ),
        )

    # A song's own label wins: it is what the event's submissions carry.
    for s in songs:
        add(s.names, s.label, single=True)
    return pairs


def _possible(entry: tuple[str, ...], pairs: dict[NameSet, Pair]) -> Pair | None:
    """The caller's pair an entry nearly names: one name exact, the other close."""
    for pair in pairs.values():
        if match_names(entry, pair.names, divisions_meet=True) == "possible":
            return pair
    return None


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
    if not pairs:
        return []

    # Each entry the caller's, with how and through which pair.
    found: list[tuple[EventRegistration, Match, Pair]] = []
    for entry in entries:
        names = entry_names(entry.dancer_1, entry.dancer_2)
        if entry.linked_label:
            pair = next(
                (p for p in pairs.values() if same_label(p.label, entry.linked_label)),
                None,
            )
            if pair:
                found.append((entry, "exact", pair))
        elif (pair := pairs.get(name_set(names))) is not None:
            found.append((entry, "exact", pair))
        elif (pair := _possible(names, pairs)) is not None:
            found.append((entry, "possible", pair))
    if not found:
        return []

    # What is already in each event: its songs' names and labels.
    event_ids = {e.event_id for e, _, _ in found}
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
    covered_names: dict[str, set[NameSet]] = defaultdict(set)
    covered_labels: dict[str, list[str]] = defaultdict(list)
    if submitted_ids:
        for s in await songs_on_file(session, Song.id.in_(submitted_ids)):
            for event_id, ids in in_event.items():
                if s.song_id in ids:
                    covered_names[event_id].add(name_set(s.names))
                    covered_labels[event_id].append(s.label)

    def label_in(event_id: str, label: str) -> bool:
        return any(same_label(label, x) for x in covered_labels[event_id])

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
    for entry, match, pair in found:
        names = entry_names(entry.dancer_1, entry.dancer_2)
        has_music = False
        if match == "possible":
            if name_set(names) in covered_names[entry.event_id]:
                continue
            has_music = label_in(entry.event_id, pair.label)
        elif entry.linked_label:
            if label_in(entry.event_id, entry.linked_label):
                continue
        elif name_set(names) in covered_names[entry.event_id]:
            continue

        event = events[entry.event_id]
        season = event_season(event.season_year, event.start_date)
        song = None
        if not has_music:
            # The other events of this one's season each song is submitted to.
            submitted = {
                song_id: sorted(
                    {n for eid, n, sy in places if eid != event.id and sy == season}
                )
                for song_id, places in went.items()
            }
            # Linked or nearly named: rank as the pair's own names would.
            target = (
                entry
                if match == "exact" and not entry.linked_label
                else SimpleNamespace(
                    dancer_1=pair.names[0],
                    dancer_2=pair.names[1] if len(pair.names) > 1 else None,
                    division=entry.division,
                )
            )
            candidates = rank_candidates(
                target,  # type: ignore[arg-type]
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
                "match": match,
                "partnership_label": pair.label,
                "has_music": has_music,
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
    summary="The caller's registrations with no music in, or to confirm",
    description=(
        "Entries on registration lists of events not yet completed that are "
        "the caller's: exact when they name one of the caller's pairs (the "
        "caller and a partner, a managed partnership, or the names of one of "
        "their songs) or are linked to one of their labels, with no music in "
        "yet; possible when one name is exact and the other close (a typo or "
        "a shortened first name), for the caller to confirm. Left out: "
        "scratched, handled, linked to someone else, or dismissed by the "
        "caller. Each carries the caller's best strongly matching song not "
        "yet submitted to the event, or null. Requires "
        "deejaytools.submissions.read."
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
    """List the caller's registrations with no music in, or to confirm."""
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
    "/{id}/confirm",
    response_model=None,  # 204: no body
    status_code=204,
    response_class=Response,
    summary="Confirm a possible registration match (that's me)",
    description=(
        "The caller says an entry that only nearly names them is theirs: it "
        "is linked to their partnership label, as an admin's link would, so "
        "their music counts for it. Only an entry the caller's reminders list "
        "as a possible match. Requires deejaytools.submissions.write."
    ),
    responses={
        **AUTH,
        404: {
            "model": ErrorResponse,
            "description": "Not one of the caller's possible matches.",
        },
        500: {"model": ErrorResponse, "description": "The write failed."},
    },
)
async def confirm_registration_reminder(
    id: RegistrationId,
    caller: Caller = Depends(require_scope("deejaytools.submissions.write")),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Link a possible match to the caller's partnership."""
    try:
        reminder = next(
            (
                r
                for r in await registration_reminders(session, caller.user_id)
                if r["registration_id"] == id and r["match"] == "possible"
            ),
            None,
        )
        if reminder is None:
            raise api_error(
                404, ErrorCode.NOT_FOUND, "No possible registration match to confirm"
            )
        row = await session.get(EventRegistration, id)
        if row is None:
            raise api_error(404, ErrorCode.NOT_FOUND, "Registration not found")
        row.linked_label = reminder["partnership_label"]
        row.updated_at = now_ms()
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        sentry_sdk.capture_exception(exc)
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"registration_reminder_confirm_failed user={caller.user_id} "
                f"registration={id}: {exc!r}",
            )
        )
        return _internal()
    return Response(status_code=204)


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
    row = await session.get(EventRegistration, id)
    if row is None:
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
        change_feed.record(
            session,
            "event_registrations",
            change_feed.SUMMARY,
            {
                "event_id": row.event_id,
                "kind": "not-me",
                "division": row.division,
                "dancer_1": row.dancer_1,
                "dancer_2": row.dancer_2,
            },
            covers=["registration_reminder_dismissals"],
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
