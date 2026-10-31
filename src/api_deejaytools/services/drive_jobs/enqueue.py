"""Adding jobs to the ``drive_jobs`` queue.

The job kinds, ``add_drive_job`` (in the caller's transaction, durable with
the change it is for) and ``enqueue_drive_job`` (committed at once), the
best-effort enqueue helpers that run after a request's own commit, and
``event_folder_path``, the Drive folder names of an event.
"""

from __future__ import annotations

import time
import uuid
from typing import Literal

import sentry_sdk
from mini_app_polis.logger import LOG_FAILURE, get_logger, with_log_prefix
from sqlalchemy.ext.asyncio import AsyncSession

from ...domain import season_year_from_date_string
from ...models import DriveJob
from ...zod_coerce import js_trim
from .. import drive

logger = get_logger()

DriveJobKind = Literal[
    "copy",
    "trash",
    "rename",
    "share_event_folder",
    "unshare_event_folder",
    "share_song",
]
SHARE_EVENT_FOLDER: DriveJobKind = "share_event_folder"
UNSHARE_EVENT_FOLDER: DriveJobKind = "unshare_event_folder"
SHARE_SONG: DriveJobKind = "share_song"

DRIVE_FOLDER_ROLES = ("dj", "event-manager")
"""The staff roles that carry read access to the event's Drive folder."""


def add_drive_job(
    session: AsyncSession,
    kind: DriveJobKind,
    *,
    submission_id: str | None = None,
    file_id: str | None = None,
    event_id: str | None = None,
    user_id: str | None = None,
) -> None:
    """Add one pending job, due now, to the session's transaction. Does not
    commit: the caller's commit makes it durable with the change it is for."""
    now = int(time.time() * 1000)
    session.add(
        DriveJob(
            id=str(uuid.uuid4()),
            kind=kind,
            submission_id=submission_id,
            file_id=file_id,
            event_id=event_id,
            user_id=user_id,
            status="pending",
            attempts=0,
            next_attempt_at=now,
            created_at=now,
            updated_at=now,
        )
    )


async def enqueue_drive_job(
    session: AsyncSession,
    kind: DriveJobKind,
    *,
    submission_id: str | None = None,
    file_id: str | None = None,
) -> None:
    """Insert one pending job, due now, and commit it."""
    add_drive_job(session, kind, submission_id=submission_id, file_id=file_id)
    await session.commit()


def event_folder_path(
    name: str, season_year: str | None, start_date: str
) -> tuple[str, str]:
    """(season year, event name) of an event's Drive folder,
    ``<root>/<season_year>/Events/<event_name>/``, as folder names.

    The season is the event's, or else its start date's, as for the copies
    that go into the folder.
    """
    season = js_trim(season_year or "") or season_year_from_date_string(start_date)
    return drive.sanitize_folder_name(season), drive.sanitize_folder_name(name)


async def enqueue_trash_jobs(
    session: AsyncSession, file_ids: list[str], *, source: str, context: dict[str, str]
) -> None:
    """Queue Drive files for trashing after their rows are gone.

    A failure to enqueue one file is logged and reported, never raised: the
    delete that orphaned it has already committed.
    """
    for file_id in file_ids:
        try:
            await enqueue_drive_job(session, "trash", file_id=file_id)
        except Exception as exc:  # noqa: BLE001 - see docstring
            await session.rollback()
            logger.error(
                with_log_prefix(
                    LOG_FAILURE,
                    f"drive_trash_enqueue_failed source={source} file={file_id} "
                    f"{context}: {exc!r}",
                )
            )
            with sentry_sdk.new_scope() as scope:
                scope.set_tag("subsystem", "drive_jobs")
                scope.set_tag("drive_job_kind", "trash")
                scope.set_context(
                    "drive_job",
                    {"file_id": file_id, "stage": "enqueue", "source": source},
                )
                sentry_sdk.capture_exception(exc)


async def enqueue_copy_job(
    session: AsyncSession, submission_id: str, *, context: dict[str, str]
) -> None:
    """Queue the per-event Drive copy of a new submission.

    Best-effort, like ``enqueue_trash_jobs``: the submission is the record of
    truth. Nothing retries a lost enqueue, so a failure is reported, not only
    logged.
    """
    try:
        await enqueue_drive_job(session, "copy", submission_id=submission_id)
    except Exception as exc:  # noqa: BLE001 - see docstring
        await session.rollback()
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"drive_copy_enqueue_failed submission={submission_id} "
                f"{context}: {exc!r}",
            )
        )
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("subsystem", "drive_jobs")
            scope.set_tag("drive_job_kind", "copy")
            scope.set_context(
                "drive_job", {"submission_id": submission_id, "stage": "enqueue"}
            )
            sentry_sdk.capture_exception(exc)
