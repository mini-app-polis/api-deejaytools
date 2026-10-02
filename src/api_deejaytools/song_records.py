"""A song on the wire, and the ownership checks the song routes share
(deejaytools-api src/routes/songs.ts: ``mapSong``, ``isLegacySong``,
``assertPartnerOwned``, ``assertManagedPartnershipOwned``).

Kept outside the router so the upload route and admin routes reuse them.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import ManagedPartnership, Partner, Song
from .zod_coerce import js_trim

LEGACY_PREFIX = "[Legacy] "


def is_legacy_song(processed_filename: str | None) -> bool:
    """Rows from the removed claim-legacy flow: a "[Legacy] " processed
    filename, no Drive file, no playable audio."""
    return processed_filename is not None and processed_filename.startswith(
        LEGACY_PREFIX
    )


def computed_song_display_name(song: Song) -> str | None:
    """Display name, else processed filename, else original filename, trimmed."""
    for value in (song.display_name, song.processed_filename, song.original_filename):
        trimmed = js_trim(value or "")
        if trimmed:
            return trimmed
    return None


def map_song(
    song: Song,
    *,
    partner_first_name: str | None = None,
    partner_last_name: str | None = None,
    partner_kind: str | None = None,
    managed_leader_first_name: str | None = None,
    managed_leader_last_name: str | None = None,
    managed_follower_first_name: str | None = None,
    managed_follower_last_name: str | None = None,
) -> dict[str, Any]:
    """A songs row on the wire, with whichever joined names the caller loaded.

    Names not loaded are null, as deejaytools-api returns them: the single
    song routes join the partner only, so their managed_* names are null even
    when managed_partnership_id is set.
    """
    return {
        "id": song.id,
        "user_id": song.user_id,
        "partner_id": song.partner_id,
        "display_name": computed_song_display_name(song),
        "original_filename": song.original_filename,
        "drive_file_id": song.drive_file_id,
        "drive_folder_id": song.drive_folder_id,
        "processed_filename": song.processed_filename,
        "division": song.division,
        "routine_name": song.routine_name,
        "personal_descriptor": song.personal_descriptor,
        "season_year": song.season_year,
        "is_legacy": is_legacy_song(song.processed_filename),
        "created_at": song.created_at,
        "updated_at": song.updated_at,
        "partner_first_name": partner_first_name,
        "partner_last_name": partner_last_name,
        "partner_kind": partner_kind,
        "managed_partnership_id": song.managed_partnership_id,
        "managed_leader_first_name": managed_leader_first_name,
        "managed_leader_last_name": managed_leader_last_name,
        "managed_follower_first_name": managed_follower_first_name,
        "managed_follower_last_name": managed_follower_last_name,
    }


async def load_song_with_partner(
    session: AsyncSession, song_id: str, *, user_id: str | None = None
) -> dict[str, Any] | None:
    """One song joined to its partner's names, mapped; None if absent.

    With ``user_id``, only that user's live (not soft-deleted) song.
    """
    stmt = (
        select(Song, Partner.first_name, Partner.last_name, Partner.kind)
        .outerjoin(Partner, Partner.id == Song.partner_id)
        .where(Song.id == song_id)
    )
    if user_id is not None:
        stmt = stmt.where(Song.user_id == user_id, Song.deleted_at.is_(None))
    row = (await session.execute(stmt.limit(1))).first()
    if row is None:
        return None
    return map_song(
        row[0],
        partner_first_name=row[1],
        partner_last_name=row[2],
        partner_kind=row[3],
    )


async def assert_partner_owned(
    session: AsyncSession, user_id: str, partner_id: str | None
) -> bool:
    """True when no partner is named, or the named partner is the user's."""
    if not partner_id:
        return True
    found = await session.execute(
        select(Partner.id)
        .where(Partner.id == partner_id, Partner.user_id == user_id)
        .limit(1)
    )
    return found.first() is not None


async def assert_managed_partnership_owned(
    session: AsyncSession, user_id: str, managed_partnership_id: str | None
) -> bool:
    """True when none is named, or the named managed partnership is the
    user's (soft-deleted ones included, as deejaytools-api checks)."""
    if not managed_partnership_id:
        return True
    found = await session.execute(
        select(ManagedPartnership.id)
        .where(
            ManagedPartnership.id == managed_partnership_id,
            ManagedPartnership.user_id == user_id,
        )
        .limit(1)
    )
    return found.first() is not None


class SongData(BaseModel):
    """A song as the API returns it."""

    id: str = Field(..., description="Song id.")
    user_id: str = Field(..., description="users.id of the owner.")
    partner_id: str | None = Field(None, description="Partner (or placeholder) id.")
    display_name: str | None = Field(
        None,
        description="Display name, else processed, else original filename.",
    )
    original_filename: str | None = Field(None, description="Uploaded filename.")
    drive_file_id: str | None = Field(None, description="Drive file id.")
    drive_folder_id: str | None = Field(None, description="Drive folder id.")
    processed_filename: str | None = Field(
        None, description="Normalized, versioned filename."
    )
    division: str | None = Field(None, description="Division.")
    routine_name: str | None = Field(None, description="Routine name.")
    personal_descriptor: str | None = Field(None, description="Owner's descriptor.")
    season_year: str | None = Field(None, description="Season year.")
    is_legacy: bool = Field(..., description="A claimed legacy song with no file.")
    created_at: int = Field(..., description="Created, epoch ms.")
    updated_at: int = Field(..., description="Last updated, epoch ms.")
    partner_first_name: str | None = Field(None, description="Partner first name.")
    partner_last_name: str | None = Field(None, description="Partner last name.")
    partner_kind: str | None = Field(
        None, description="partner, or a placeholder kind (solo, team, other)."
    )
    managed_partnership_id: str | None = Field(
        None, description="Managed partnership the song is for."
    )
    managed_leader_first_name: str | None = Field(None, description="Leader first.")
    managed_leader_last_name: str | None = Field(None, description="Leader last.")
    managed_follower_first_name: str | None = Field(None, description="Follower first.")
    managed_follower_last_name: str | None = Field(None, description="Follower last.")
