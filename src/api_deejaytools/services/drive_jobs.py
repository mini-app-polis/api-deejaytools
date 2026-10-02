"""The ``drive_jobs`` queue: enqueueing (deejaytools-api DRIVE.md, "drive_jobs queue").

Only enqueueing lives here for now. Processing — the batch the scheduler
runs each tick — comes with the scheduler.
"""

from __future__ import annotations

import time
import uuid
from typing import Literal

import sentry_sdk
from mini_app_polis.logger import LOG_FAILURE, get_logger, with_log_prefix
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DriveJob

logger = get_logger()

DriveJobKind = Literal["copy", "trash", "rename"]


async def enqueue_drive_job(
    session: AsyncSession,
    kind: DriveJobKind,
    *,
    submission_id: str | None = None,
    file_id: str | None = None,
) -> None:
    """Insert one pending job, due now, and commit it."""
    now = int(time.time() * 1000)
    session.add(
        DriveJob(
            id=str(uuid.uuid4()),
            kind=kind,
            submission_id=submission_id,
            file_id=file_id,
            status="pending",
            attempts=0,
            next_attempt_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    await session.commit()


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
