"""The ``share_event_folder``, ``unshare_event_folder`` and ``share_song``
job runners.

The folder jobs give an event's DJs and event managers read access to the
event's folder and take it away again; each checks the staff list when it
runs, so jobs that run out of order still leave access matching it.
``share_song`` gives a song's partner reader access to the song's Drive
file, reading the song's partner when it runs.
"""

from __future__ import annotations

from mini_app_polis.logger import LOG_SUCCESS, LOG_WARNING, get_logger, with_log_prefix
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ...models import DriveJob, Event, EventStaffGrant, Partner, Song, User
from ...zod_coerce import js_trim
from .. import drive
from .enqueue import (
    DRIVE_FOLDER_ROLES,
    SHARE_EVENT_FOLDER,
    UNSHARE_EVENT_FOLDER,
    add_drive_job,
    event_folder_path,
)
from .job import DriveJobRefused, _Job, _missing_field

logger = get_logger()


async def _holds_folder_role(db: AsyncSession, event_id: str, user_id: str) -> bool:
    """Whether the person is the event's DJ or event manager now."""
    held = (
        await db.execute(
            select(EventStaffGrant.role)
            .where(
                EventStaffGrant.event_id == event_id,
                EventStaffGrant.user_id == user_id,
                EventStaffGrant.role.in_(DRIVE_FOLDER_ROLES),
            )
            .limit(1)
        )
    ).first()
    return held is not None


async def _share_email(db: AsyncSession, job: _Job, user_id: str) -> str:
    """The address a folder is shared with: the person's ``users.email``.

    Refused (failed at once) when they have none. The reason names the user
    id, never the address: it reaches the shared Discord channel.
    """
    email = (
        await db.execute(select(User.email).where(User.id == user_id))
    ).scalar_one_or_none()
    if not js_trim(email or ""):
        raise DriveJobRefused(
            f"{job.kind}: user {user_id} has no email address to share the "
            "event folder with"
        )
    return js_trim(email or "")


def _event_folder_fields(job: _Job) -> tuple[str, str]:
    if not job.event_id:
        raise _missing_field(job, "event_id")
    if not job.user_id:
        raise _missing_field(job, "user_id")
    return job.event_id, job.user_id


async def _run_share_folder_job(db: AsyncSession, job: _Job) -> None:
    """Share the event's folder with one of its DJs or event managers.

    Nothing to do when they hold neither role (an unshare is, or was,
    queued) or the event is gone. The folder is created if missing and
    recorded on the job (``file_id``): that is what an unshare takes access
    away from, wherever the event's folder is by then.
    """
    event_id, user_id = _event_folder_fields(job)
    if not await _holds_folder_role(db, event_id, user_id):
        await db.commit()
        logger.info(
            with_log_prefix(
                LOG_SUCCESS,
                f"drive_folder_share_skipped job={job.id} event={event_id} "
                f"user={user_id} reason=no_folder_role",
            )
        )
        return
    event = (
        await db.execute(
            select(Event.name, Event.season_year, Event.start_date).where(
                Event.id == event_id
            )
        )
    ).first()
    if event is None:
        await db.commit()
        return
    email = await _share_email(db, job, user_id)
    await db.commit()

    season, name = event_folder_path(event.name, event.season_year, event.start_date)
    folder_id = await drive.ensure_event_folder(season_year=season, event_name=name)
    result = await drive.share_with_reader(folder_id, email)
    if result.failed:
        raise result.failed[0][1]
    if not result.shared:
        raise DriveJobRefused(
            f"{job.kind}: the email address of user {user_id} is not one Drive "
            "can share with"
        )
    await db.execute(
        update(DriveJob).where(DriveJob.id == job.id).values(file_id=folder_id)
    )
    await db.commit()
    logger.info(
        with_log_prefix(
            LOG_SUCCESS,
            f"drive_folder_shared job={job.id} event={event_id} user={user_id} "
            f"folder={folder_id}",
        )
    )
    # Revoked while the share was in flight: the unshare may have found
    # nothing recorded yet, so queue another, which finds this folder.
    if not await _holds_folder_role(db, event_id, user_id):
        add_drive_job(db, UNSHARE_EVENT_FOLDER, event_id=event_id, user_id=user_id)
    await db.commit()


async def _run_unshare_folder_job(db: AsyncSession, job: _Job) -> None:
    """Take a former DJ's or event manager's access to the event's folders.

    Every folder a share for this person on this event recorded, so a former
    folder of the event is unshared too, except one they still
    reach as staff of another event. Nothing to do when they hold the role
    again (re-granted since).
    """
    event_id, user_id = _event_folder_fields(job)
    if await _holds_folder_role(db, event_id, user_id):
        await db.commit()
        logger.info(
            with_log_prefix(
                LOG_SUCCESS,
                f"drive_folder_unshare_skipped job={job.id} event={event_id} "
                f"user={user_id} reason=folder_role_held",
            )
        )
        return
    still_held = (
        select(DriveJob.file_id)
        .join(
            EventStaffGrant,
            (EventStaffGrant.event_id == DriveJob.event_id)
            & (EventStaffGrant.user_id == DriveJob.user_id),
        )
        .where(
            DriveJob.kind == SHARE_EVENT_FOLDER,
            DriveJob.user_id == user_id,
            DriveJob.event_id != event_id,
            DriveJob.file_id.is_not(None),
            EventStaffGrant.role.in_(DRIVE_FOLDER_ROLES),
        )
    )
    folders = sorted(
        folder_id
        for folder_id in (
            await db.execute(
                select(DriveJob.file_id)
                .distinct()
                .where(
                    DriveJob.kind == SHARE_EVENT_FOLDER,
                    DriveJob.event_id == event_id,
                    DriveJob.user_id == user_id,
                    DriveJob.file_id.is_not(None),
                    DriveJob.file_id.not_in(still_held),
                )
            )
        ).scalars()
        if folder_id is not None
    )
    if not folders:
        await db.commit()
        logger.info(
            with_log_prefix(
                LOG_SUCCESS,
                f"drive_folder_unshare_noop job={job.id} event={event_id} "
                f"user={user_id}",
            )
        )
        return
    email = await _share_email(db, job, user_id)
    await db.commit()
    for folder_id in folders:
        removed = await drive.remove_reader(folder_id, email)
        logger.info(
            with_log_prefix(
                LOG_SUCCESS,
                f"drive_folder_unshared job={job.id} event={event_id} "
                f"user={user_id} folder={folder_id} removed={removed}",
            )
        )


async def _run_share_song_job(db: AsyncSession, job: _Job) -> None:
    """Give the song's partner reader access to the song's Drive file.

    Reads the song and its partner when it runs, so a partner changed again
    or cleared since the job was queued is followed. Nothing to do when the
    song is gone, has no partner, or the partner has no email. Like the
    build's share it is best-effort: an address Drive refuses is logged,
    not retried.
    """
    if not job.file_id:
        raise _missing_field(job, "file_id")
    row = (
        await db.execute(
            select(Song.id, Partner.email)
            .join(
                Partner,
                (Partner.id == Song.partner_id) & (Partner.user_id == Song.user_id),
            )
            .where(Song.drive_file_id == job.file_id, Song.deleted_at.is_(None))
            .limit(1)
        )
    ).first()
    await db.commit()
    if row is None or not js_trim(row.email or ""):
        logger.info(
            with_log_prefix(
                LOG_SUCCESS, f"drive_share_song_noop drive_file_id={job.file_id}"
            )
        )
        return
    result = await drive.share_song_file(job.file_id, [row.email])
    if result.failed:
        failed = [(email, repr(err)) for email, err in result.failed]
        logger.warning(
            with_log_prefix(
                LOG_WARNING,
                f"song_drive_share_partial_failure song={row.id} "
                f"file={job.file_id} shared={len(result.shared)} failed={failed}",
            )
        )
        return
    logger.info(
        with_log_prefix(
            LOG_SUCCESS,
            f"drive_share_song_succeeded song={row.id} drive_file_id={job.file_id}",
        )
    )
