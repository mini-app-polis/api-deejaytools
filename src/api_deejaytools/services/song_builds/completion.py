"""The end of a build that has its Drive file: sharing the file, recording
it on the song in one transaction with "song added", and what follows (the
event copies re-queued, or the file deprecated when the song was deleted
while it built).

A notification never fails a build: by the time "song added" is composed
the file is in Drive, so a failure there leaves the song unannounced.
"""

from __future__ import annotations

import sentry_sdk
from mini_app_polis.logger import LOG_FAILURE, LOG_WARNING, get_logger, with_log_prefix
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...clock import now_ms
from ...models import (
    EventSongSubmission,
    ManagedPartnership,
    Partner,
    Song,
    SongUpload,
    User,
)
from .. import drive, notifications
from ..drive_jobs import enqueue_drive_job
from .claims import LostClaim, _mine
from .naming import _name

logger = get_logger()


def song_added_text(
    song: Song,
    user: User,
    partner: Partner | None,
    managed: ManagedPartnership | None,
    uploader: User | None,
) -> str:
    """The "song added" sentence: who added it, and its details.

    ``uploader`` is set when someone uploaded the song for its owner ("Upload
    For"); the sentence then names both.
    """

    def name(person: User) -> str:
        return notifications.person_name(
            person.first_name, person.last_name, person.display_name, person.id
        )

    return notifications.song_added_text(
        owner=name(user),
        uploader=name(uploader) if uploader is not None else None,
        routine=song.routine_name or song.display_name,
        division=song.division,
        partner=_name(partner.first_name, partner.last_name) if partner else None,
        partner_kind=partner.kind if partner else None,
        managed=(
            f"{_name(managed.leader_first_name, managed.leader_last_name)} & "
            f"{_name(managed.follower_first_name, managed.follower_last_name)}"
            if managed is not None
            else None
        ),
    )


def _compose_announcement(
    song: Song,
    user: User,
    partner: Partner | None,
    managed: ManagedPartnership | None,
    uploader: User | None,
) -> str | None:
    """``song_added_text``, or None (logged) if it cannot be composed.

    By the time it runs the file is in Drive: a bug in a notification must
    not be treated as the build failing, which would delete or retry a song
    whose upload succeeded. It goes unannounced instead.
    """
    try:
        return song_added_text(song, user, partner, managed, uploader)
    except Exception as exc:  # noqa: BLE001 - a notification never fails a build
        logger.error(
            with_log_prefix(
                LOG_FAILURE, f"song_added_announcement_failed song={song.id}: {exc!r}"
            )
        )
        return None


def _announce(
    db: AsyncSession, song_id: str, sentence: str, drive_file_id: str
) -> None:
    """Register "song added" on ``db``'s transaction; never raises.

    Inside ``_finish``'s transaction, where a raise would roll back the
    commit that adds the song and send it down the failure path.
    """
    try:
        notifications.announce_song_added(db, sentence, drive_file_id=drive_file_id)
    except Exception as exc:  # noqa: BLE001 - a notification never fails a build
        logger.error(
            with_log_prefix(
                LOG_FAILURE, f"song_added_announcement_failed song={song_id}: {exc!r}"
            )
        )


async def _share(song: Song, user: User, partner: Partner | None, file_id: str) -> None:
    """Reader access for the uploader and, for a partner song, the partner's
    email. Best-effort."""
    targets: list[str | None] = [user.email]
    if partner is not None:
        targets.append(partner.email)
    try:
        result = await drive.share_song_file(file_id, targets)
    except Exception as exc:  # noqa: BLE001 - sharing never fails a build
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"song_drive_share_failed song={song.id} file={file_id}: {exc!r}",
            )
        )
        sentry_sdk.capture_exception(exc)
        return
    if result.failed:
        failed = [(email, repr(err)) for email, err in result.failed]
        logger.warning(
            with_log_prefix(
                LOG_WARNING,
                f"song_drive_share_partial_failure song={song.id} file={file_id} "
                f"shared={len(result.shared)} failed={failed}",
            )
        )


async def _finish(
    maker: async_sessionmaker[AsyncSession],
    song_id: str,
    claim: str,
    *,
    announcement: str | None,
    original_filename: str,
    processed_filename: str,
    season: str,
    drive_file_id: str,
    drive_folder_id: str,
) -> bool:
    """Record the file on the song and drop the staged bytes, in one transaction.

    That commit is when the song is added for good, so "song added" rides on
    it: posted if it commits, never if it rolls back or the song was deleted
    while it built. ``announcement`` is None when it could not be composed;
    the song is added unannounced.
    """
    async with maker() as db:
        song = (
            await db.execute(select(Song).where(Song.id == song_id).with_for_update())
        ).scalar_one_or_none()
        if song is None:
            await db.rollback()
            # Hard-deleted mid-build: nothing will ever reference the file.
            await _discard_file(maker, drive_file_id, song_id)
            return False
        song.original_filename = original_filename
        song.processed_filename = processed_filename
        song.season_year = season
        song.drive_file_id = drive_file_id
        song.drive_folder_id = drive_folder_id
        song.updated_at = now_ms()
        deleted_meanwhile = song.deleted_at is not None
        dropped = (
            await db.execute(
                delete(SongUpload)
                .where(*_mine(song_id, claim))
                .returning(SongUpload.song_id)
            )
        ).first()
        if dropped is None:
            await db.rollback()
            raise LostClaim(song_id)
        if announcement is not None and not deleted_meanwhile:
            _announce(db, song_id, announcement, drive_file_id)
        await db.commit()

    if deleted_meanwhile:
        # Deleted by its owner while building: its delete saw no file to
        # deprecate, so deprecate it now.
        await _discard_file(maker, drive_file_id, song_id)
    else:
        await _requeue_event_copies(maker, song_id)
    return True


async def _requeue_event_copies(
    maker: async_sessionmaker[AsyncSession], song_id: str
) -> None:
    """A copy job for each submission of the song still without a copy. A
    submission made mid-build may have had its copy job find no file."""
    submission_ids: list[str] = []
    try:
        async with maker() as db:
            submission_ids = list(
                (
                    await db.execute(
                        select(EventSongSubmission.id).where(
                            EventSongSubmission.song_id == song_id,
                            EventSongSubmission.drive_copy_file_id.is_(None),
                        )
                    )
                ).scalars()
            )
            for submission_id in submission_ids:
                await enqueue_drive_job(db, "copy", submission_id=submission_id)
    except Exception as exc:  # noqa: BLE001 - best-effort, reported
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"drive_copy_requeue_failed song={song_id} submissions={submission_ids}: {exc!r}",
            )
        )
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("subsystem", "drive_jobs")
            scope.set_tag("drive_job_kind", "copy")
            scope.set_context(
                "drive_job",
                {
                    "song_id": song_id,
                    "submission_ids": submission_ids,
                    "stage": "requeue",
                },
            )
            sentry_sdk.capture_exception(exc)


async def _discard_file(
    maker: async_sessionmaker[AsyncSession], file_id: str, song_id: str
) -> None:
    """Deprecate an uploaded file nothing will reference; queue it if Drive says no."""
    try:
        await drive.soft_delete(file_id)
    except Exception as exc:  # noqa: BLE001 - the queue retries it
        logger.warning(
            with_log_prefix(
                LOG_WARNING,
                f"song_orphan_file_trash_queued song={song_id} file={file_id}: {exc!r}",
            )
        )
        async with maker() as db:
            await enqueue_drive_job(db, "trash", file_id=file_id)
