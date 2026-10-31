"""Processing the ``drive_jobs`` queue: the batch the scheduler runs once
per tick (``process_drive_jobs``).

Expired leases are reclaimed, due jobs are claimed with ``FOR UPDATE SKIP
LOCKED``, each job is run by its kind's runner, and each outcome is recorded
with a ``status = 'running'`` guard. A DB error while recording one job's
outcome does not strand the rest of the batch in ``running``: each status
update is guarded on its own.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sentry_sdk
from mini_app_polis.logger import (
    LOG_FAILURE,
    LOG_SUCCESS,
    LOG_WARNING,
    get_logger,
    with_log_prefix,
)
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ...clock import now_ms
from ...models import DriveJob
from .. import notifications
from .copy_jobs import _run_copy_job, _run_rename_job, _run_trash_job
from .enqueue import SHARE_EVENT_FOLDER, SHARE_SONG, UNSHARE_EVENT_FOLDER
from .job import DriveJobRefused, _Job
from .share_jobs import (
    _run_share_folder_job,
    _run_share_song_job,
    _run_unshare_folder_job,
)

logger = get_logger()

MAX_ATTEMPTS = 10
"""Tries before a job is marked ``failed`` and reported: about three hours with
the backoff below, sized to outlast a Google-side incident, not a blip."""

DEFAULT_BATCH_SIZE = 10
"""Jobs claimed per tick. A batch must finish inside ``LEASE_TIMEOUT_MS``;
raising this without raising the lease lets live jobs be reclaimed."""

LEASE_TIMEOUT_MS = 10 * 60_000
"""A job ``running`` longer than this is presumed orphaned (process died
holding it) and returned to ``pending``."""

# Raw SQL on purpose: the claim is one statement that picks a batch with
# FOR UPDATE SKIP LOCKED and returns whole rows, so concurrent passes take
# disjoint batches. The ORM can spell the parts, but not more plainly than this.
_CLAIM_SQL = text(
    """
    UPDATE drive_jobs SET status = 'running', updated_at = :now
    WHERE id IN (
      SELECT id FROM drive_jobs
      WHERE status = 'pending' AND next_attempt_at <= :now
      ORDER BY next_attempt_at
      LIMIT :limit
      FOR UPDATE SKIP LOCKED
    )
    RETURNING *
    """
)


def backoff_ms(attempts: int) -> int:
    """Delay before the next try, given the attempt count after the failure
    just recorded: ``min(60 s × 2^attempts, 30 min)``."""
    return min(60_000 * 2**attempts, 30 * 60_000)


async def _reclaim_stuck_jobs(db: AsyncSession, now: int) -> int:
    """Return jobs whose lease expired to ``pending``, attempts unchanged: the
    job never got a real try, the process holding it died."""
    result = await db.execute(
        update(DriveJob)
        .where(
            DriveJob.status == "running",
            DriveJob.updated_at < now - LEASE_TIMEOUT_MS,
        )
        .values(status="pending", updated_at=now)
        .returning(DriveJob.id)
    )
    ids = [r.id for r in result]
    await db.commit()
    if ids:
        logger.warning(
            with_log_prefix(
                LOG_WARNING, f"drive_jobs_reclaimed count={len(ids)} job_ids={ids}"
            )
        )
    return len(ids)


async def _claim_jobs(db: AsyncSession, limit: int, now: int) -> list[_Job]:
    """Atomically claim up to ``limit`` due jobs; SKIP LOCKED gives concurrent
    replicas disjoint batches."""
    result = await db.execute(_CLAIM_SQL, {"now": now, "limit": limit})
    jobs = [
        _Job(
            id=m["id"],
            kind=m["kind"],
            submission_id=m["submission_id"],
            file_id=m["file_id"],
            attempts=int(m["attempts"]),
            row_keys=tuple(m.keys()),
            next_attempt_at=int(m["next_attempt_at"]),
            event_id=m.get("event_id"),
            user_id=m.get("user_id"),
        )
        for m in result.mappings()
    ]
    await db.commit()
    # UPDATE ... RETURNING does not keep the subquery's order; run oldest first.
    jobs.sort(key=lambda j: j.next_attempt_at)
    return jobs


async def _run_job(db: AsyncSession, job: _Job) -> None:
    if job.kind == "copy":
        await _run_copy_job(db, job)
    elif job.kind == "trash":
        await _run_trash_job(job)
    elif job.kind == "rename":
        await _run_rename_job(db, job)
    elif job.kind == SHARE_EVENT_FOLDER:
        await _run_share_folder_job(db, job)
    elif job.kind == UNSHARE_EVENT_FOLDER:
        await _run_unshare_folder_job(db, job)
    elif job.kind == SHARE_SONG:
        await _run_share_song_job(db, job)
    else:
        raise RuntimeError(f"unknown drive job kind: {job.kind}")


async def _mark_done(db: AsyncSession, job: _Job) -> bool:
    """Complete a job if this run still holds its lease; False when superseded."""
    result = await db.execute(
        update(DriveJob)
        .where(DriveJob.id == job.id, DriveJob.status == "running")
        .values(status="done", last_error=None, updated_at=now_ms())
        .returning(DriveJob.id)
    )
    completed = result.first() is not None
    await db.commit()
    return completed


async def _record_failure(db: AsyncSession, job: _Job, exc: Exception) -> None:
    """Reschedule (or fail) a job after its run raised, then log and report.

    The update is guarded on its own, so a DB error here does not leave the
    rest of the batch in ``running`` for the whole lease. If it fails the
    error is logged and the batch goes on; the job stays ``running`` until
    the lease reclaims it.
    """
    attempts = job.attempts + 1
    # A refused job is failed at once: no retry would change its outcome.
    exhausted = isinstance(exc, DriveJobRefused) or attempts >= MAX_ATTEMPTS
    message = str(exc) or type(exc).__name__
    now = now_ms()
    next_attempt_at = now + backoff_ms(attempts)
    # recorded: this run's outcome is the job's. superseded: the guard matched
    # no row — the lease ran out and the job was reclaimed, so another run
    # owns it now and will report its own outcome (as ``_mark_done``).
    recorded = superseded = False
    try:
        result = await db.execute(
            update(DriveJob)
            .where(DriveJob.id == job.id, DriveJob.status == "running")
            .values(
                status="failed" if exhausted else "pending",
                attempts=attempts,
                next_attempt_at=next_attempt_at,
                last_error=message,
                updated_at=now,
            )
            .returning(DriveJob.id)
        )
        recorded = result.first() is not None
        await db.commit()
        superseded = not recorded
    except Exception as update_exc:  # noqa: BLE001 - see docstring
        await _safe_rollback(db)
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"drive_job_failure_update_failed job={job.id} kind={job.kind}: "
                f"{update_exc!r}",
            )
        )

    # First failure at error level too: a configuration error fails the same
    # way every attempt, and waiting for exhaustion to say so wastes hours.
    next_at = (
        "null"
        if exhausted
        else datetime.fromtimestamp(next_attempt_at / 1000, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    line = (
        f"{'drive_job_exhausted' if exhausted else 'drive_job_retrying'} "
        f"job={job.id} kind={job.kind} submission={job.submission_id} "
        f"file={job.file_id} attempts={attempts} max_attempts={MAX_ATTEMPTS} "
        f"error_message={message!r} next_attempt_at={next_at}"
    )
    if superseded:
        line += " superseded=true"
    if exhausted or attempts == 1:
        logger.error(with_log_prefix(LOG_FAILURE, line))
    else:
        logger.warning(with_log_prefix(LOG_WARNING, line))

    # Only giving up is reported; every retry would bury real failures. A
    # superseded run reports nothing: the run that owns the job does, and two
    # runners at the last attempt must not make two Sentry events.
    if exhausted and not superseded:
        with sentry_sdk.new_scope() as scope:
            scope.set_level("error")
            scope.set_tag("subsystem", "drive_jobs")
            scope.set_tag("drive_job_kind", job.kind)
            scope.set_context(
                "drive_job",
                {
                    "job_id": job.id,
                    "kind": job.kind,
                    "submission_id": job.submission_id,
                    "file_id": job.file_id,
                    "attempts": attempts,
                },
            )
            event_id = sentry_sdk.capture_exception(exc)
        # Discord hears only of giving up, and only once the job is recorded
        # as failed: a job whose update failed stays running, is reclaimed
        # and fails again, and would otherwise be announced twice.
        if recorded:
            notifications.report_fault(
                f"drive job {job.id} ({job.kind}) · gave up after {attempts} attempts",
                exc,
                event_id=event_id,
            )


async def _safe_rollback(db: AsyncSession) -> None:
    try:
        await db.rollback()
    except Exception:  # noqa: BLE001 - the session is being abandoned anyway
        pass


async def process_drive_jobs(
    db: AsyncSession, limit: int = DEFAULT_BATCH_SIZE
) -> dict[str, int]:
    """Drain one batch of due Drive jobs. Called once per scheduler tick.

    Reclaims expired leases, claims up to ``limit`` due jobs with ``FOR UPDATE
    SKIP LOCKED``, runs them in sequence, and records each outcome with a
    ``status = 'running'`` guard so a run whose lease was reclaimed cannot
    overwrite the newer run. Never raises for a job's failure; a claim
    failure ends the pass.

    Returns ``{"claimed", "succeeded", "failed"}``; a superseded completion
    counts as failed.
    """
    now = now_ms()

    try:
        await _reclaim_stuck_jobs(db, now)
    except Exception as exc:  # noqa: BLE001 - reclaim is a backstop
        await _safe_rollback(db)
        logger.error(
            with_log_prefix(LOG_FAILURE, f"drive_jobs_reclaim_failed: {exc!r}")
        )

    try:
        jobs = await _claim_jobs(db, limit, now)
    except Exception as exc:  # noqa: BLE001 - the queue is not draining: report
        await _safe_rollback(db)
        logger.error(with_log_prefix(LOG_FAILURE, f"drive_jobs_claim_failed: {exc!r}"))
        with sentry_sdk.new_scope() as scope:
            scope.set_level("error")
            scope.set_tag("subsystem", "drive_jobs")
            event_id = sentry_sdk.capture_exception(exc)
        # Fails every tick while the cause lasts: posted once per run of
        # failures, re-armed by the next successful claim.
        notifications.report_fault_once(
            "drive_jobs.claim", "drive jobs · claim failed", exc, event_id=event_id
        )
        return {"claimed": 0, "succeeded": 0, "failed": 0}
    notifications.clear_fault("drive_jobs.claim")

    done = 0
    for job in jobs:
        try:
            await _run_job(db, job)
            # A DB error here is handled as the job failing: the work is
            # retried (and a copy reuses its tagged file).
            if await _mark_done(db, job):
                done += 1
            else:
                logger.warning(
                    with_log_prefix(
                        LOG_WARNING,
                        f"drive_job_completion_superseded job={job.id} kind={job.kind}",
                    )
                )
        except Exception as exc:  # noqa: BLE001 - one job never stops the batch
            await _safe_rollback(db)
            await _record_failure(db, job, exc)

    if jobs:
        logger.info(
            with_log_prefix(
                LOG_SUCCESS,
                f"drive_jobs_processed claimed={len(jobs)} succeeded={done} "
                f"failed={len(jobs) - done}",
            )
        )
    return {"claimed": len(jobs), "succeeded": done, "failed": len(jobs) - done}
