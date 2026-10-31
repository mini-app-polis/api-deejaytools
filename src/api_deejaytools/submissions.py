"""Event song submissions: rounds, The Open, the per-event Drive filename, and
the caller's joined submission rows.

``fetch_user_submission_rows`` and ``map_submission_row`` are shared with the
admin routes.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from .domain import (
    is_follower_am_division,  # noqa: F401 - re-exported
    song_entity_key,
)
from .labels import build_structured_song_label, partnership_label
from .models import Event, EventSongSubmission, ManagedPartnership, Partner, Song, User
from .routers.events import compute_status
from .song_records import shared_with
from .zod_coerce import js_trim

SubmissionRound = Literal["prelims_and_finals", "prelims_only", "finals_only"]
SUBMISSION_ROUNDS: tuple[SubmissionRound, ...] = (
    "prelims_and_finals",
    "prelims_only",
    "finals_only",
)
DEFAULT_ROUND: SubmissionRound = "prelims_and_finals"

ROUND_SPLIT_DIVISION = "Classic"
"""The only division where a prelims/finals split is offered."""

OPEN_EVENT_LABEL = "The Open"


def rounds_occupied(round_: str) -> tuple[str, ...]:
    """Rounds a submission occupies; prelims_and_finals fills both."""
    if round_ == "prelims_only":
        return ("prelims",)
    if round_ == "finals_only":
        return ("finals",)
    return ("prelims", "finals")


def rounds_conflict(a: str, b: str) -> bool:
    """Whether two submissions for one entity and division overlap in rounds."""
    return bool(set(rounds_occupied(a)) & set(rounds_occupied(b)))


_NOT_ALNUM = re.compile(r"[^a-z0-9]")


def is_open_event(event_name: str | None) -> bool:
    """Whether an event name is The Open: lowercased, stripped to a-z0-9, and
    starting with "theopen" ("Open Practice Night" and "2026 The Open" do not)."""
    if not event_name:
        return False
    return _NOT_ALNUM.sub("", event_name.lower()).startswith("theopen")


def effective_division(
    submission_division: str | None, song_division: str | None
) -> str:
    """The division a submission counts as, trimmed: its override, else the song's."""
    division = submission_division if submission_division is not None else song_division
    return js_trim(division or "")


def resolve_submission_filename(
    *,
    processed_filename: str | None,
    original_filename: str | None,
    song_id: str,
) -> str:
    """Filename for a submission's per-event Drive copy: the processed
    filename, else the original, else the song id. The same for every event."""
    return (
        js_trim(processed_filename or "") or js_trim(original_filename or "") or song_id
    )


async def entity_slot_taken(
    session: AsyncSession,
    *,
    event_id: str,
    entity: str,
    division: str,
    round_: str,
    song_id: str | None = None,
) -> bool:
    """Whether ``entity`` (``song_entity_key``) already has a song submitted to
    the event for ``division`` (trimmed) in a round that overlaps ``round_``.

    ``song_id``'s own submission, if any, does not count. Each entity has one
    song per division per event (per round, for Classic at The Open).
    """
    existing = (
        await session.execute(
            select(
                Song.id.label("song_id"),
                Song.user_id,
                Song.partner_id,
                Song.managed_partnership_id,
                Song.division.label("song_division"),
                EventSongSubmission.division.label("submission_division"),
                EventSongSubmission.round.label("submission_round"),
            )
            .select_from(EventSongSubmission)
            .join(Song, Song.id == EventSongSubmission.song_id)
            .where(EventSongSubmission.event_id == event_id)
        )
    ).all()
    for row in existing:
        if song_id is not None and row.song_id == song_id:
            continue
        if (
            song_entity_key(row.user_id, row.partner_id, row.managed_partnership_id)
            != entity
        ):
            continue
        if effective_division(row.submission_division, row.song_division) != division:
            continue
        row_round = (
            row.submission_round if row.submission_round is not None else DEFAULT_ROUND
        )
        if rounds_conflict(round_, row_round):
            return True
    return False


def slot_taken_message(division: str) -> str:
    """The ENTITY_SLOT_TAKEN message for ``division``."""
    return (
        "This entity already has a song submitted for "
        f"{division or 'this division'}. Remove it before adding another."
    )


async def fetch_user_submission_rows(
    session: AsyncSession,
    user_id: str,
    *,
    event_id: str | None = None,
    submission_id: str | None = None,
    include_shared: bool = False,
) -> Sequence[Any]:
    """A user's submissions joined to event, song, owner, partner and managed
    partnership, newest first. Map each with ``map_submission_row``.

    ``include_shared`` adds, whoever submitted them, the submissions of live
    songs the user owns or shares as linked partner."""
    owner = aliased(User, name="song_owner")
    managed = aliased(ManagedPartnership, name="managed_partnership")
    own = EventSongSubmission.submitted_by_user_id == user_id
    conditions = [own]
    if include_shared:
        conditions = [
            or_(
                own,
                and_(
                    Song.deleted_at.is_(None),
                    or_(Song.user_id == user_id, shared_with(user_id)),
                ),
            )
        ]
    if event_id:
        conditions.append(EventSongSubmission.event_id == event_id)
    if submission_id:
        conditions.append(EventSongSubmission.id == submission_id)
    result = await session.execute(
        select(
            EventSongSubmission.id,
            EventSongSubmission.event_id,
            EventSongSubmission.song_id,
            EventSongSubmission.created_at,
            EventSongSubmission.submitted_by_user_id,
            Event.name.label("event_name"),
            Event.start_date.label("event_start_date"),
            Event.end_date.label("event_end_date"),
            Event.timezone.label("event_timezone"),
            EventSongSubmission.division.label("submission_division"),
            EventSongSubmission.round.label("submission_round"),
            Song.division.label("song_division"),
            Song.display_name.label("song_display_name"),
            Song.processed_filename.label("song_processed_filename"),
            Song.routine_name.label("song_routine_name"),
            owner.first_name.label("owner_first"),
            owner.last_name.label("owner_last"),
            Partner.first_name.label("partner_first"),
            Partner.last_name.label("partner_last"),
            Partner.kind.label("partner_kind"),
            Partner.partner_role.label("partner_role"),
            managed.leader_first_name.label("managed_leader_first"),
            managed.leader_last_name.label("managed_leader_last"),
            managed.follower_first_name.label("managed_follower_first"),
            managed.follower_last_name.label("managed_follower_last"),
        )
        .select_from(EventSongSubmission)
        .join(Event, Event.id == EventSongSubmission.event_id)
        .join(Song, Song.id == EventSongSubmission.song_id)
        .outerjoin(owner, owner.id == Song.user_id)
        .outerjoin(Partner, Partner.id == Song.partner_id)
        .outerjoin(managed, managed.id == Song.managed_partnership_id)
        .where(*conditions)
        .order_by(EventSongSubmission.created_at.desc())
    )
    return result.all()


def map_submission_row(row: Any) -> dict[str, Any]:
    """A row from ``fetch_user_submission_rows`` on the wire.

    ``division`` is the override or the song's, untrimmed and possibly null,
    as the web app expects it.
    """
    division = (
        row.submission_division
        if row.submission_division is not None
        else row.song_division
    )
    partnership = partnership_label(
        managed_leader_first=row.managed_leader_first,
        managed_leader_last=row.managed_leader_last,
        managed_follower_first=row.managed_follower_first,
        managed_follower_last=row.managed_follower_last,
        owner_first=row.owner_first,
        owner_last=row.owner_last,
        partner_first=row.partner_first,
        partner_last=row.partner_last,
        partner_kind=row.partner_kind,
        partner_role=row.partner_role,
        division=division,
    )
    return {
        "id": row.id,
        "event_id": row.event_id,
        "event_name": row.event_name,
        "event_start_date": row.event_start_date,
        "event_status": compute_status(
            row.event_start_date, row.event_end_date, row.event_timezone
        ),
        "song_id": row.song_id,
        "song_label": build_structured_song_label(
            partnership=partnership,
            division=division,
            # Dancers never see the season: it rolls over on October 1, so a
            # November event's routines would read as next year's.
            season_year=None,
            routine_name=row.song_routine_name,
            processed_filename=row.song_processed_filename,
            display_name=row.song_display_name,
            song_id=row.song_id,
            # The web app's song label uses the same pieces and separator.
            separator=" - ",
        ),
        "division": division,
        "round": (
            row.submission_round if row.submission_round is not None else DEFAULT_ROUND
        ),
        "created_at": row.created_at,
    }
