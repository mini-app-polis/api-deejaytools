"""Event song submissions: rounds, The Open, the per-event Drive filename, and
the caller's joined submission rows.

Ported from deejaytools-api src/schemas/index.ts (submission rounds,
``isOpenEvent``, ``isFollowerAmDivision``), src/lib/submissionFilename.ts and
the exported ``fetchUserSubmissionRows`` / ``mapSubmissionRow`` of
src/routes/event-song-submissions.ts, which admin routes reuse.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from .labels import build_structured_song_label, partnership_label
from .models import Event, EventSongSubmission, ManagedPartnership, Partner, Song, User
from .routers.events import compute_status
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


def is_follower_am_division(division: str | None) -> bool:
    """Divisions ordered amateur-first because the follower is the amateur."""
    return js_trim(division or "") == "ProAm FollowerAm"


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
    event_name: str,
) -> str:
    """Filename for a submission's per-event Drive copy: the processed
    filename, else the original, else the song id.

    The Open is expected to get its own convention; that branch is kept here
    so there is one place to change, and today returns the same value.
    """
    filename = (
        js_trim(processed_filename or "") or js_trim(original_filename or "") or song_id
    )
    if is_open_event(event_name):
        # TODO(open): apply The Open's naming convention once defined.
        return filename
    return filename


async def fetch_user_submission_rows(
    session: AsyncSession,
    user_id: str,
    *,
    event_id: str | None = None,
    submission_id: str | None = None,
) -> Sequence[Any]:
    """A user's submissions joined to event, song, owner, partner and managed
    partnership, newest first. Map each with ``map_submission_row``."""
    owner = aliased(User, name="song_owner")
    managed = aliased(ManagedPartnership, name="managed_partnership")
    conditions = [EventSongSubmission.submitted_by_user_id == user_id]
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
            Song.season_year.label("song_season_year"),
            owner.first_name.label("owner_first"),
            owner.last_name.label("owner_last"),
            Partner.first_name.label("partner_first"),
            Partner.last_name.label("partner_last"),
            Partner.kind.label("partner_kind"),
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
    as deejaytools-api returns it.
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
            season_year=row.song_season_year,
            routine_name=row.song_routine_name,
            processed_filename=row.song_processed_filename,
            display_name=row.song_display_name,
            song_id=row.song_id,
        ),
        "division": division,
        "round": (
            row.submission_round if row.submission_round is not None else DEFAULT_ROUND
        ),
        "created_at": row.created_at,
    }
