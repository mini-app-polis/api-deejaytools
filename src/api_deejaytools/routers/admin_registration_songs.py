"""``GET /v1/admin/events/{id}/registrations/songs-on-file`` (docs/API.md).

For an event's registration list: the entries with no music submitted to the
event that look like they already have a song on file in deejaytools, with
the candidate songs, so the admin can ask them to submit it. Admin only,
behind ``deejaytools.registrations.read`` held through a role: a song in
someone's library that they have not submitted is theirs, so event staff
never see this. Read only: it suggests, and never links or submits.

A song's competing names (its managed partnership's leader and follower, or
its owner and partner, as ``entity_name_pair`` gives them) are compared with
the entry's dancers using ``name_key``, the normalisation ``entry_key`` uses
(case, accents and spacing ignored):

- **strong**: the names are the entry's dancers exactly, in either order;
- **possible**: one name is exact and the other close (a typo or a shortened
  first name), and the divisions share a word ("Rising Star (NASDE)" and
  "Rising Star Classic"). One shared name alone is never a candidate.

An entry is left out when it is scratched, handled or linked by hand, or
when a song already submitted to the event names its dancers exactly.
Deleted songs, legacy songs and songs already submitted to the event are
never candidates.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Annotated, Any, Literal

import sentry_sdk
from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse
from mini_app_polis.logger import LOG_FAILURE, get_logger, with_log_prefix
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..auth import Caller, require_scope
from ..database import get_db_session
from ..domain import entity_name_pair, full_name, season_year_from_date_string
from ..errors import (
    Envelope,
    ErrorCode,
    ErrorResponse,
    api_error,
    error_body,
    success_list,
)
from ..labels import build_structured_song_label, partnership_label
from ..models import (
    Event,
    EventRegistration,
    EventSongSubmission,
    ManagedPartnership,
    Partner,
    Song,
    User,
)
from ..song_records import is_legacy_song
from .admin_event_registrations import name_key

logger = get_logger()

router = APIRouter(prefix="/v1/admin", tags=["admin-event-registrations"])

#: Most candidate songs returned for one entry, best first.
MAX_CANDIDATES = 5

Match = Literal["strong", "possible"]

# "Classic (NASDE)" is the Classic division: a trailing parenthetical is the
# registration's own qualifier, not part of the division's name.
_QUALIFIER = re.compile(r"\s*\([^)]*\)\s*$")


class SongOnFileData(BaseModel):
    """A song that looks like an entry's, not submitted to the event."""

    song_id: str = Field(..., description="Song id.")
    match: Match = Field(
        ...,
        description=(
            "strong: the song names the entry's dancers exactly, in either "
            "order; possible: one exactly and the other closely (a typo or a "
            "shortened first name), in a division sharing a word with the "
            "entry's."
        ),
    )
    song_label: str = Field(..., description="Structured song label.")
    partnership_label: str = Field(..., description="Who the song is for.")
    division: str | None = Field(None, description="The song's division.")
    season_year: str | None = Field(None, description="The song's season year.")
    same_division: bool = Field(
        ...,
        description=(
            "The song's division is the entry's (case, accents, spacing and a "
            "trailing parenthetical ignored)."
        ),
    )
    submitted_this_season: list[str] = Field(
        ...,
        description=(
            "Names of the other events of this event's season the song is "
            "submitted to, in name order."
        ),
    )


class RegistrationSongsData(BaseModel):
    """One entry with no music in, and the songs on file that look like its."""

    registration_id: str = Field(..., description="Registration entry id.")
    candidates: list[SongOnFileData] = Field(
        ...,
        description=(
            "Best first: strong before possible, then same division, then the "
            f"event's season year, then submitted elsewhere this season. At "
            f"most {MAX_CANDIDATES}."
        ),
    )


class RegistrationSongsResponse(Envelope[list[RegistrationSongsData]]):
    """The entries that likely have a song on file, in list order."""

    data: list[RegistrationSongsData] = Field(..., description="The entries.")


@dataclass(frozen=True)
class _SongOnFile:
    song_id: str
    names: tuple[str, ...]
    label: str
    song_label: str
    division: str | None
    season_year: str | None
    updated_at: int


def song_names(
    *,
    owner_first: str | None,
    owner_last: str | None,
    owner_display: str | None,
    partner_first: str | None,
    partner_last: str | None,
    partner_kind: str | None,
    partner_role: str | None,
    managed_leader_first: str | None,
    managed_leader_last: str | None,
    managed_follower_first: str | None,
    managed_follower_last: str | None,
) -> tuple[str, ...]:
    """The names a song competes under, each as ``name_key`` gives it."""
    managed = managed_leader_first is not None
    first, second = entity_name_pair(
        owner_name=full_name(owner_first, owner_last) or (owner_display or ""),
        partner_name=(
            full_name(partner_first, partner_last)
            if partner_first is not None
            else None
        ),
        partner_kind=partner_kind,
        partner_role=partner_role,
        managed_leader=(
            full_name(managed_leader_first, managed_leader_last) if managed else None
        ),
        managed_follower=(
            full_name(managed_follower_first, managed_follower_last)
            if managed
            else None
        ),
    )
    return tuple(k for k in (name_key(first), name_key(second)) if k)


def entry_names(dancer_1: str, dancer_2: str | None) -> tuple[str, ...]:
    """An entry's dancers, each as ``name_key`` gives it."""
    return tuple(k for k in (name_key(dancer_1), name_key(dancer_2)) if k)


def _name_set(names: tuple[str, ...]) -> frozenset[tuple[str, int]]:
    """``names`` in any order: equal for the same names, either way round."""
    return frozenset(Counter(names).items())


def _bare(name: str) -> str:
    """A ``name_key`` name with punctuation dropped: letters, digits, spaces."""
    return " ".join(re.sub(r"[^\w ]+|_", " ", name).split())


def _edit_distance(a: str, b: str) -> int:
    """Optimal string alignment distance: a swap of neighbours is one edit."""
    rows = [[i] + [0] * len(b) for i in range(len(a) + 1)]
    rows[0] = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            rows[i][j] = min(
                rows[i - 1][j] + 1, rows[i][j - 1] + 1, rows[i - 1][j - 1] + cost
            )
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                rows[i][j] = min(rows[i][j], rows[i - 2][j - 2] + 1)
    return rows[len(a)][len(b)]


def _similar(a: str, b: str) -> bool:
    """The same word but for a typo: one edit, two in words of 7 or more."""
    if a == b:
        return True
    longest = max(len(a), len(b))
    if longest < 4:
        return False
    return _edit_distance(a, b) <= (2 if longest >= 7 else 1)


def close_name(a: str, b: str) -> bool:
    """Two ``name_key`` names that are the same but for a typo or a shortened
    first name ("Kris" for "Kristen"), as the web app's look-alike match has it."""
    pa, pb = _bare(a), _bare(b)
    if not pa or not pb:
        return False
    if pa.replace(" ", "") == pb.replace(" ", ""):
        return True
    ta, tb = pa.split(" "), pb.split(" ")
    if len(ta) >= 2 and len(tb) >= 2:
        first_a, first_b = ta[0], tb[0]
        first_ok = _similar(first_a, first_b) or (
            min(len(first_a), len(first_b)) >= 3
            and (first_a.startswith(first_b) or first_b.startswith(first_a))
        )
        return first_ok and _similar("".join(ta[1:]), "".join(tb[1:]))
    return _similar(pa.replace(" ", ""), pb.replace(" ", ""))


def division_key(division: str | None) -> str:
    """A division name as ``same_division`` compares it."""
    return name_key(_QUALIFIER.sub("", division or ""))


def divisions_overlap(a: str | None, b: str | None) -> bool:
    """Whether two division names share a word ("Rising Star (NASDE)" and
    "Rising Star Classic"), a trailing parenthetical and punctuation aside."""
    words_a = set(_bare(division_key(a)).split())
    words_b = set(_bare(division_key(b)).split())
    return bool(words_a & words_b)


def match_names(
    entry: tuple[str, ...],
    song: tuple[str, ...],
    *,
    divisions_meet: bool = False,
) -> Match | None:
    """How a song's names meet an entry's: strong, possible, or not at all.

    Strong: the same names, in either order. Possible: two names each, one
    the same and the other close (a typo or a shortened first name), and
    only when the divisions at least partly match (``divisions_meet``).
    """
    if not entry or not song:
        return None
    if _name_set(entry) == _name_set(song):
        return "strong"
    if not divisions_meet or len(entry) != 2 or len(song) != 2:
        return None
    for i in (0, 1):
        for j in (0, 1):
            if entry[i] == song[j] and close_name(entry[1 - i], song[1 - j]):
                return "possible"
    return None


def rank_candidates(
    entry: EventRegistration,
    songs: list[_SongOnFile],
    *,
    season_year: str,
    submitted: dict[str, list[str]],
) -> list[dict[str, Any]]:
    """``songs`` that look like ``entry``'s, best first, at most MAX_CANDIDATES."""
    names = entry_names(entry.dancer_1, entry.dancer_2)
    wanted = division_key(entry.division)
    found = []
    for song in songs:
        how = match_names(
            names,
            song.names,
            divisions_meet=divisions_overlap(entry.division, song.division),
        )
        if how is None:
            continue
        same = bool(wanted) and division_key(song.division) == wanted
        elsewhere = submitted.get(song.song_id, [])
        rank = (
            how != "strong",
            not same,
            song.season_year != season_year,
            not elsewhere,
            -song.updated_at,
            song.song_id,
        )
        found.append(
            (
                rank,
                {
                    "song_id": song.song_id,
                    "match": how,
                    "song_label": song.song_label,
                    "partnership_label": song.label,
                    "division": song.division,
                    "season_year": song.season_year,
                    "same_division": same,
                    "submitted_this_season": elsewhere,
                },
            )
        )
    found.sort(key=lambda pair: pair[0])
    return [data for _, data in found[:MAX_CANDIDATES]]


async def _songs_on_file(session: AsyncSession) -> list[_SongOnFile]:
    """Every song that could be submitted: not deleted, not legacy."""
    owner = aliased(User, name="song_owner")
    managed = aliased(ManagedPartnership, name="managed_partnership")
    rows = (
        await session.execute(
            select(
                Song.id,
                Song.division,
                Song.season_year,
                Song.routine_name,
                Song.display_name,
                Song.processed_filename,
                Song.updated_at,
                owner.first_name.label("owner_first"),
                owner.last_name.label("owner_last"),
                owner.display_name.label("owner_display"),
                Partner.first_name.label("partner_first"),
                Partner.last_name.label("partner_last"),
                Partner.kind.label("partner_kind"),
                Partner.partner_role.label("partner_role"),
                managed.leader_first_name.label("managed_leader_first"),
                managed.leader_last_name.label("managed_leader_last"),
                managed.follower_first_name.label("managed_follower_first"),
                managed.follower_last_name.label("managed_follower_last"),
            )
            .select_from(Song)
            .outerjoin(owner, owner.id == Song.user_id)
            .outerjoin(Partner, Partner.id == Song.partner_id)
            .outerjoin(managed, managed.id == Song.managed_partnership_id)
            .where(Song.deleted_at.is_(None))
        )
    ).all()
    songs = []
    for r in rows:
        if is_legacy_song(r.processed_filename):
            continue
        names = song_names(
            owner_first=r.owner_first,
            owner_last=r.owner_last,
            owner_display=r.owner_display,
            partner_first=r.partner_first,
            partner_last=r.partner_last,
            partner_kind=r.partner_kind,
            partner_role=r.partner_role,
            managed_leader_first=r.managed_leader_first,
            managed_leader_last=r.managed_leader_last,
            managed_follower_first=r.managed_follower_first,
            managed_follower_last=r.managed_follower_last,
        )
        label = partnership_label(
            managed_leader_first=r.managed_leader_first,
            managed_leader_last=r.managed_leader_last,
            managed_follower_first=r.managed_follower_first,
            managed_follower_last=r.managed_follower_last,
            owner_first=r.owner_first,
            owner_last=r.owner_last,
            partner_first=r.partner_first,
            partner_last=r.partner_last,
            partner_kind=r.partner_kind,
            partner_role=r.partner_role,
            division=r.division,
        )
        songs.append(
            _SongOnFile(
                song_id=r.id,
                names=names,
                label=label,
                song_label=build_structured_song_label(
                    partnership=label,
                    division=r.division,
                    season_year=r.season_year,
                    routine_name=r.routine_name,
                    processed_filename=r.processed_filename,
                    display_name=r.display_name,
                    song_id=r.id,
                ),
                division=r.division,
                season_year=r.season_year,
                updated_at=r.updated_at,
            )
        )
    return songs


def _event_season(season_year: str | None, start_date: str) -> str:
    return season_year or season_year_from_date_string(start_date)


async def registration_songs(
    session: AsyncSession, event: Event
) -> list[dict[str, Any]]:
    """``event``'s entries with no music in that likely have a song on file."""
    entries = (
        (
            await session.execute(
                select(EventRegistration)
                .where(EventRegistration.event_id == event.id)
                .order_by(EventRegistration.position)
            )
        )
        .scalars()
        .all()
    )
    open_entries = [
        e for e in entries if not (e.scratched or e.handled or e.linked_label)
    ]
    if not open_entries:
        return []

    season = _event_season(event.season_year, event.start_date)
    in_event: set[str] = set()
    submitted: dict[str, list[str]] = defaultdict(list)
    for song_id, event_id, name, season_year, start_date in (
        await session.execute(
            select(
                EventSongSubmission.song_id,
                Event.id,
                Event.name,
                Event.season_year,
                Event.start_date,
            ).join(Event, Event.id == EventSongSubmission.event_id)
        )
    ).all():
        if event_id == event.id:
            in_event.add(song_id)
        elif _event_season(season_year, start_date) == season:
            if name not in submitted[song_id]:
                submitted[song_id].append(name)
    for names in submitted.values():
        names.sort()

    songs = await _songs_on_file(session)
    # Who already has music in: the names of every song submitted here.
    covered = {_name_set(s.names) for s in songs if s.song_id in in_event}
    available = [s for s in songs if s.song_id not in in_event]

    out = []
    for entry in open_entries:
        if _name_set(entry_names(entry.dancer_1, entry.dancer_2)) in covered:
            continue
        candidates = rank_candidates(
            entry, available, season_year=season, submitted=submitted
        )
        if candidates:
            out.append({"registration_id": entry.id, "candidates": candidates})
    return out


EventId = Annotated[str, Path(description="Event id.")]


@router.get(
    "/events/{id}/registrations/songs-on-file",
    response_model=RegistrationSongsResponse,
    summary="Registrations that likely have a song on file",
    description=(
        "The event's registration entries with no music submitted that look "
        "like they have a song in deejaytools already, each with up to "
        f"{MAX_CANDIDATES} candidate songs not submitted to the event. Read "
        "only. Requires deejaytools.registrations.read (admin only)."
    ),
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token."},
        403: {"model": ErrorResponse, "description": "Lacks the scope."},
        404: {"model": ErrorResponse, "description": "No such event."},
        500: {"model": ErrorResponse, "description": "The query failed."},
    },
)
async def list_registration_songs(
    id: EventId,
    _caller: Caller = Depends(require_scope("deejaytools.registrations.read")),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """List the registrations that likely have a song on file."""
    event = await session.get(Event, id)
    if event is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Event not found")
    try:
        return success_list(await registration_songs(session, event))
    except SQLAlchemyError as exc:
        sentry_sdk.capture_exception(exc)
        logger.error(
            with_log_prefix(
                LOG_FAILURE, f"registration_songs_failed event={id}: {exc!r}"
            )
        )
        return JSONResponse(
            status_code=500,
            content=error_body(ErrorCode.INTERNAL, "Internal server error"),
        )
