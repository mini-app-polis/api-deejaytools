"""The ``copy``, ``rename`` and ``trash`` job runners.

A copy is recorded only into a still-empty ``drive_copy_file_id`` and is
otherwise soft-deleted (``_record_copy``), so a copy that finishes after its
submission was deleted, or after another run recorded a different copy, is
not orphaned.
"""

from __future__ import annotations

from typing import Any

from mini_app_polis.logger import (
    LOG_FAILURE,
    LOG_SUCCESS,
    LOG_WARNING,
    get_logger,
    with_log_prefix,
)
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ...domain import season_year_from_date_string
from ...models import Event, EventSongSubmission, Song
from ...zod_coerce import js_trim
from .. import drive
from .enqueue import enqueue_trash_jobs
from .job import _Job, _missing_field

logger = get_logger()


async def _fetch_submission_context(db: AsyncSession, submission_id: str) -> Any:
    """The submission, song and event fields copy and rename need, or None."""
    result = await db.execute(
        select(
            EventSongSubmission.id.label("submission_id"),
            EventSongSubmission.drive_copy_file_id.label("already_copied"),
            Song.id.label("song_id"),
            Song.drive_file_id,
            Song.original_filename,
            Song.processed_filename,
            Song.division,
            EventSongSubmission.division.label("submission_division"),
            EventSongSubmission.round.label("submission_round"),
            Event.name.label("event_name"),
            Event.season_year.label("event_season_year"),
            Event.start_date.label("event_start_date"),
        )
        .join(Song, Song.id == EventSongSubmission.song_id)
        .join(Event, Event.id == EventSongSubmission.event_id)
        .where(EventSongSubmission.id == submission_id)
        .limit(1)
    )
    row = result.first()
    # End the read's transaction so nothing is held open across Drive calls.
    await db.commit()
    return row


def _submission_filename(row: Any) -> str:
    # Imported here: submissions imports routers.events, which imports this
    # package for its enqueue helpers.
    from ...submissions import resolve_submission_filename

    return resolve_submission_filename(
        processed_filename=row.processed_filename,
        original_filename=row.original_filename,
        song_id=row.song_id,
    )


def _event_copy_destination(row: Any) -> tuple[str, str, str | None]:
    """(season year, division, round subfolder) for a submission's copy.

    The event's season, not the song's: one event's submissions share one year
    folder. The submission's division wins over the song's. Round-specific
    songs nest under Finals/ or Prelims/; both rounds (or null) sit in the
    division folder itself.
    """
    season = js_trim(row.event_season_year or "") or season_year_from_date_string(
        row.event_start_date
    )
    division = row.submission_division
    if division is None:
        division = row.division
    division = js_trim(division or "") or "unknown"
    subfolder = {"finals_only": "Finals", "prelims_only": "Prelims"}.get(
        row.submission_round or ""
    )
    return season, division, subfolder


async def _run_copy_job(db: AsyncSession, job: _Job) -> None:
    if not job.submission_id:
        raise _missing_field(job, "submission_id")
    submission_id = job.submission_id

    row = await _fetch_submission_context(db, submission_id)
    if row is None:
        return  # submission deleted since: nothing to copy
    if row.already_copied:
        return  # a copy is recorded: a retry must not make another
    if not row.drive_file_id:
        # The upload's background build re-queues the copy once the file exists.
        logger.warning(
            with_log_prefix(
                LOG_WARNING,
                f"drive_copy_skipped_no_source submission={submission_id} "
                f"song={row.song_id}",
            )
        )
        return

    season, division, subfolder = _event_copy_destination(row)
    copy = await drive.copy_song_to_event_folder(
        row.drive_file_id,
        submission_id=submission_id,
        filename=_submission_filename(row),
        season_year=season,
        event_name=row.event_name,
        division=division,
        subfolder=subfolder,
    )
    await _record_copy(db, submission_id, copy.file_id, reused=copy.reused)


async def _record_copy(
    db: AsyncSession, submission_id: str, file_id: str, *, reused: bool
) -> None:
    """Store a copy's id on its submission, or discard the copy if it lost.

    A copy finishing after its submission was deleted must not be orphaned,
    so the id is written only into a still-empty column. When nothing is updated, the
    submission is gone or holds another copy, so this copy is soft-deleted,
    unless it is the very copy recorded (another run reused and recorded it).
    """
    result = await db.execute(
        update(EventSongSubmission)
        .where(
            EventSongSubmission.id == submission_id,
            EventSongSubmission.drive_copy_file_id.is_(None),
        )
        .values(drive_copy_file_id=file_id)
        .returning(EventSongSubmission.id)
    )
    updated = result.first() is not None
    await db.commit()
    if updated:
        logger.info(
            with_log_prefix(
                LOG_SUCCESS,
                f"drive_copy_succeeded submission={submission_id} "
                f"drive_file_id={file_id} reused={reused}",
            )
        )
        return

    current = await db.execute(
        select(EventSongSubmission.drive_copy_file_id).where(
            EventSongSubmission.id == submission_id
        )
    )
    current_row = current.first()
    await db.commit()
    if current_row is not None and current_row.drive_copy_file_id == file_id:
        return

    reason = "submission_deleted" if current_row is None else "other_copy_recorded"
    logger.warning(
        with_log_prefix(
            LOG_WARNING,
            f"drive_copy_discarded submission={submission_id} drive_file_id={file_id} "
            f"reason={reason}",
        )
    )
    try:
        await drive.soft_delete(file_id)
    except Exception as exc:  # noqa: BLE001 - fall back to the durable queue
        # The job itself must not fail: its retry would find the submission
        # gone (or copied) and stop, leaving this copy orphaned for good.
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"drive_copy_discard_failed drive_file_id={file_id}: {exc!r}",
            )
        )
        await enqueue_trash_jobs(
            db,
            [file_id],
            source="drive_copy_discard",
            context={"submission_id": submission_id},
        )


async def _run_rename_job(db: AsyncSession, job: _Job) -> None:
    """Re-apply the current naming rule to an existing copy; safe to repeat."""
    if not job.submission_id:
        raise _missing_field(job, "submission_id")
    row = await _fetch_submission_context(db, job.submission_id)
    # Gone, or no copy yet (a pending copy job applies the current rule).
    if row is None or not row.already_copied:
        return
    filename = _submission_filename(row)
    changed = await drive.rename_file(row.already_copied, filename)
    event = "drive_rename_succeeded" if changed else "drive_rename_noop"
    logger.info(
        with_log_prefix(
            LOG_SUCCESS,
            f"{event} submission={job.submission_id} "
            f"drive_file_id={row.already_copied} filename={filename}",
        )
    )


async def _run_trash_job(job: _Job) -> None:
    if not job.file_id:
        raise _missing_field(job, "file_id")
    await drive.soft_delete(job.file_id)
    logger.info(
        with_log_prefix(
            LOG_SUCCESS, f"drive_trash_succeeded drive_file_id={job.file_id}"
        )
    )
