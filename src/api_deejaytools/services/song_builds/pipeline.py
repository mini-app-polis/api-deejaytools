"""The build itself: claim the upload, name the file, tag it, upload it to
Drive (or reuse a file already uploaded for the song), and hand over to
``completion`` to record it, or to ``failure`` when a step raises.

The filename is reserved under a per-slot advisory lock, so two uploads in
flight for the same slot never get the same ``vNN``.
"""

from __future__ import annotations

import asyncio
import contextlib
import re

from mini_app_polis.logger import LOG_WARNING, get_logger, with_log_prefix
from sqlalchemy import select, text, union_all
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...database import get_sessionmaker
from ...models import ManagedPartnership, Partner, Song, SongUpload, User
from .. import drive
from ..tagging import tag_song_bytes
from .claims import LostClaim, _claim, _heartbeat, _update_mine
from .completion import _compose_announcement, _finish, _share
from .failure import _on_failure
from .naming import (
    entity_names,
    file_extension,
    processed_stem,
    season_year_now,
    versioned_filename,
)

logger = get_logger()

SONG_APP_PROPERTY = "deejaytools_song_id"
"""Drive appProperty naming the song a file was uploaded for."""

_VERSION_RE = re.compile(r"_v(\d+)(?:\.[^.]*)?$")


async def build_song(song_id: str) -> bool:
    """Claim and run one build. True when the song now has its file.

    Never raises: a failure deletes an unreferenced song (and deprecates any
    file already uploaded) or schedules a retry.
    """
    maker = get_sessionmaker()
    async with maker() as db:
        claim = await _claim(db, song_id)
    if claim is None:
        return False
    heartbeat = asyncio.ensure_future(_heartbeat(maker, song_id, claim))
    try:
        return await _build(maker, song_id, claim)
    except LostClaim:
        logger.warning(
            with_log_prefix(LOG_WARNING, f"song_build_claim_lost song={song_id}")
        )
        return False
    except Exception as exc:  # noqa: BLE001 - every failure is handled below
        await _on_failure(maker, song_id, claim, exc)
        return False
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat


async def _load_people(
    db: AsyncSession, song: Song
) -> tuple[User, Partner | None, ManagedPartnership | None]:
    user = await db.get(User, song.user_id)
    if user is None:
        raise RuntimeError("User not found")
    if song.managed_partnership_id:
        managed = (
            await db.execute(
                select(ManagedPartnership).where(
                    ManagedPartnership.id == song.managed_partnership_id,
                    ManagedPartnership.user_id == song.user_id,
                )
            )
        ).scalar_one_or_none()
        if managed is None:
            raise RuntimeError("Managed partnership not found")
        return user, None, managed
    partner = None
    if song.partner_id:
        partner = (
            await db.execute(
                select(Partner).where(
                    Partner.id == song.partner_id, Partner.user_id == song.user_id
                )
            )
        ).scalar_one_or_none()
    return user, partner, None


async def _reserve_filename(
    maker: async_sessionmaker[AsyncSession],
    song: Song,
    claim: str,
    stem: str,
    extension: str,
    season: str,
) -> str:
    """Pick the next version for the song's slot and record it on the upload.

    Under a transaction-scoped advisory lock on the slot, so two builds for
    the same slot never pick the same number. Counts finished songs of the
    slot (soft-deleted included, so a deleted version's number is not handed
    out again) and filenames other unfinished builds have reserved, in one
    statement: one snapshot, so a build finishing in between (its name moving
    from ``song_uploads`` to ``songs``) is seen in exactly one of the two.
    """
    slot = "|".join(
        [
            song.user_id,
            song.division or "",
            song.routine_name or "",
            season,
            song.partner_id or "",
            song.managed_partnership_id or "",
        ]
    )
    same_slot = (
        (Song.user_id == song.user_id)
        & (text("coalesce(songs.division, '') = :division"))
        & (text("coalesce(songs.routine_name, '') = :routine"))
        & (text("coalesce(songs.partner_id, '') = :partner"))
        & (text("coalesce(songs.managed_partnership_id, '') = :managed"))
        & (Song.id != song.id)
    )
    params = {
        "division": song.division or "",
        "routine": song.routine_name or "",
        "partner": song.partner_id or "",
        "managed": song.managed_partnership_id or "",
    }
    async with maker() as db:
        # Raw SQL on purpose: a transaction-scoped advisory lock is a
        # Postgres function call the ORM has no construct for.
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:slot))"), {"slot": slot}
        )
        names = (
            await db.execute(
                union_all(
                    select(Song.processed_filename.label("name")).where(
                        same_slot, Song.season_year == season
                    ),
                    select(SongUpload.processed_filename.label("name"))
                    .join(Song, Song.id == SongUpload.song_id)
                    .where(same_slot, SongUpload.season_year == season),
                ),
                params,
            )
        ).scalars()
        highest = 0
        for name in names:
            match = _VERSION_RE.search(name or "")
            if match:
                highest = max(highest, int(match.group(1)))
        filename = versioned_filename(stem, highest + 1, extension)
        await _update_mine(
            db, song.id, claim, season_year=season, processed_filename=filename
        )
        await db.commit()
    return filename


async def _build(
    maker: async_sessionmaker[AsyncSession], song_id: str, claim: str
) -> bool:
    async with maker() as db:
        song = await db.get(Song, song_id)
        upload = await db.get(SongUpload, song_id)
        if song is None or upload is None:
            return False  # deleted meanwhile; the cascade took the upload with it
        user, partner, managed = await _load_people(db, song)
        uploader = (
            await db.get(User, upload.uploaded_by_user_id)
            if upload.uploaded_by_user_id
            else None
        )
        season = upload.season_year or season_year_now()
        processed_filename = upload.processed_filename
        drive_file_id, drive_folder_id = upload.drive_file_id, upload.drive_folder_id
        mime_type, original_filename = upload.mime_type, upload.original_filename

    first, second = entity_names(
        user=user, partner=partner, managed=managed, division=song.division
    )
    if processed_filename is None:
        stem = processed_stem(
            first=first,
            second=second,
            user_id=user.id,
            division=song.division,
            season_year=season,
            routine_name=song.routine_name,
            personal_descriptor=song.personal_descriptor,
        )
        processed_filename = await _reserve_filename(
            maker, song, claim, stem, file_extension(original_filename), season
        )

    if drive_file_id is None or drive_folder_id is None:
        found = await drive.find_song_file(
            key=SONG_APP_PROPERTY,
            value=song.id,
            season_year=season,
            division=song.division or "",
        )
        if found is None:
            async with maker() as db:
                data = (
                    await db.execute(
                        select(SongUpload.data).where(SongUpload.song_id == song.id)
                    )
                ).scalar_one()
            tagged = await asyncio.to_thread(
                tag_song_bytes,
                data,
                title=f"{first} & {second}" if second else first,
                artist=" | ".join(p for p in (song.division, song.routine_name) if p),
                year=season,
                mime_type=mime_type,
            )
            found = await drive.upload_song_file(
                tagged,
                filename=processed_filename,
                mime_type=mime_type,
                season_year=season,
                division=song.division or "",
                app_properties={SONG_APP_PROPERTY: song.id},
            )
        drive_file_id, drive_folder_id = found
        async with maker() as db:
            await _update_mine(
                db,
                song.id,
                claim,
                drive_file_id=drive_file_id,
                drive_folder_id=drive_folder_id,
            )
            await db.commit()

    await _share(song, user, partner, drive_file_id)
    return await _finish(
        maker,
        song.id,
        claim,
        announcement=_compose_announcement(song, user, partner, managed, uploader),
        original_filename=original_filename,
        processed_filename=processed_filename,
        season=season,
        drive_file_id=drive_file_id,
        drive_folder_id=drive_folder_id,
    )
