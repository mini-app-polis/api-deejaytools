"""The ``drive_jobs`` queue (deejaytools-api DRIVE.md, "drive_jobs queue").

Enqueueing (best-effort, after the request's own commit) and processing: the
batch the scheduler runs once per tick (``process_drive_jobs``), with the
per-kind work for ``copy``, ``rename`` and ``trash``.

Processing is deejaytools-api's ``processDriveJobs`` with the three "Known
defects" in this area fixed rather than reproduced (ADR-006 point 4):

- overlapping copy runs, and a copy followed by a failed DB write, no longer
  leave a duplicate copy: copies are tagged with their submission id and an
  existing one is reused (``services.drive.copy_song_to_event_folder``);
- a copy that finishes after its submission was deleted, or after another run
  recorded a different copy, is recorded only into a still-empty
  ``drive_copy_file_id`` and otherwise soft-deleted (``_record_copy``);
- a DB error while recording a job's outcome no longer strands the rest of
  the batch in ``running``: each status update is guarded on its own.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import sentry_sdk
from mini_app_polis.logger import (
    LOG_FAILURE,
    LOG_SUCCESS,
    LOG_WARNING,
    get_logger,
    with_log_prefix,
)
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..domain import season_year_from_date_string
from ..models import DriveJob, Event, EventSongSubmission, Song
from ..zod_coerce import js_trim
from . import drive, notifications

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


# --- processing ---------------------------------------------------------------

MAX_ATTEMPTS = 10
"""Tries before a job is marked ``failed`` and reported: about three hours with
the backoff below, sized to outlast a Google-side incident, not a blip."""

DEFAULT_BATCH_SIZE = 10
"""Jobs claimed per tick. A batch must finish inside ``LEASE_TIMEOUT_MS``;
raising this without raising the lease lets live jobs be reclaimed."""

LEASE_TIMEOUT_MS = 10 * 60_000
"""A job ``running`` longer than this is presumed orphaned (process died
holding it) and returned to ``pending``."""

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


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class _Job:
    id: str
    kind: str
    submission_id: str | None
    file_id: str | None
    attempts: int
    row_keys: tuple[str, ...]
    next_attempt_at: int


def _missing_field(job: _Job, field: str) -> RuntimeError:
    return RuntimeError(
        f"{job.kind} job {job.id} has no {field} (row keys: {','.join(job.row_keys)})"
    )


async def _reclaim_stuck_jobs(db: AsyncSession, now: int) -> int:
    """Return jobs whose lease expired to ``pending``, attempts unchanged: the
    job never got a real try, the process holding it died."""
    result = await db.execute(
        text(
            "UPDATE drive_jobs SET status = 'pending', updated_at = :now "
            "WHERE status = 'running' AND updated_at < :cutoff RETURNING id"
        ),
        {"now": now, "cutoff": now - LEASE_TIMEOUT_MS},
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
        )
        for m in result.mappings()
    ]
    await db.commit()
    # UPDATE ... RETURNING does not keep the subquery's order; run oldest first.
    jobs.sort(key=lambda j: j.next_attempt_at)
    return jobs


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
    # module for its enqueue helpers.
    from ..submissions import resolve_submission_filename

    return resolve_submission_filename(
        processed_filename=row.processed_filename,
        original_filename=row.original_filename,
        song_id=row.song_id,
        event_name=row.event_name,
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

    Fix for DRIVE.md "Known defects" (a copy finishing after its submission
    was deleted was orphaned; the old update had no IS NULL guard): the id is
    written only into a still-empty column. When nothing is updated, the
    submission is gone or holds another copy, so this copy is soft-deleted,
    unless it is the very copy recorded (another run reused and recorded it).
    """
    result = await db.execute(
        text(
            "UPDATE event_song_submissions SET drive_copy_file_id = :new "
            "WHERE id = :id AND drive_copy_file_id IS NULL RETURNING id"
        ),
        {"new": file_id, "id": submission_id},
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


async def _run_job(db: AsyncSession, job: _Job) -> None:
    if job.kind == "copy":
        await _run_copy_job(db, job)
    elif job.kind == "trash":
        await _run_trash_job(job)
    elif job.kind == "rename":
        await _run_rename_job(db, job)
    else:
        raise RuntimeError(f"unknown drive job kind: {job.kind}")


async def _mark_done(db: AsyncSession, job: _Job) -> bool:
    """Complete a job if this run still holds its lease; False when superseded."""
    result = await db.execute(
        text(
            "UPDATE drive_jobs SET status = 'done', last_error = NULL, "
            "updated_at = :now WHERE id = :id AND status = 'running' RETURNING id"
        ),
        {"now": _now_ms(), "id": job.id},
    )
    completed = result.first() is not None
    await db.commit()
    return completed


async def _record_failure(db: AsyncSession, job: _Job, exc: Exception) -> None:
    """Reschedule (or fail) a job after its run raised, then log and report.

    Fix for DRIVE.md "Known defects" (a DB error here escaped and left the
    rest of the batch in ``running`` for the whole lease): the update is
    guarded on its own. If it fails the error is logged and the batch goes on;
    the job stays ``running`` until the lease reclaims it.
    """
    attempts = job.attempts + 1
    exhausted = attempts >= MAX_ATTEMPTS
    message = str(exc) or type(exc).__name__
    now = _now_ms()
    next_attempt_at = now + backoff_ms(attempts)
    # recorded: this run's outcome is the job's. superseded: the guard matched
    # no row — the lease ran out and the job was reclaimed, so another run
    # owns it now and will report its own outcome (as ``_mark_done``).
    recorded = superseded = False
    try:
        result = await db.execute(
            text(
                "UPDATE drive_jobs SET status = :status, attempts = :attempts, "
                "next_attempt_at = :next, last_error = :error, updated_at = :now "
                "WHERE id = :id AND status = 'running' RETURNING id"
            ),
            {
                "status": "failed" if exhausted else "pending",
                "attempts": attempts,
                "next": next_attempt_at,
                "error": message,
                "now": now,
                "id": job.id,
            },
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
    now = _now_ms()

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
            # As deejaytools-api, a DB error here is handled as the job
            # failing: the work is retried (and a copy reuses its tagged file).
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
