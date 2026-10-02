"""``/v1/admin/drive-jobs`` (deejaytools-api docs/API.md and DRIVE.md,
src/routes/admin-drive-jobs.ts).

The operator view of the ``drive_jobs`` queue: health summary and recent
jobs behind ``deejaytools.drivejobs.read``; the rename backfill and the
retry of an exhausted job behind ``deejaytools.drivejobs.write`` (ADR-007).
"""

from __future__ import annotations

import math
import re
import time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse
from mini_app_polis.logger import LOG_FAILURE, LOG_SUCCESS, get_logger, with_log_prefix
from pydantic import BaseModel, BeforeValidator, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import Caller, require_scope
from ..database import get_db_session
from ..errors import (
    ErrorCode,
    ErrorResponse,
    ListMeta,
    Meta,
    api_error,
    error_body,
    success,
    success_list,
)
from ..models import DriveJob, Event, EventSongSubmission
from ..services.drive_jobs import enqueue_drive_job
from ..validation import ZodModel
from ..zod_coerce import JS_WHITESPACE
from ..zod_types import zod_query

logger = get_logger()

router = APIRouter(prefix="/v1/admin/drive-jobs", tags=["admin-drive-jobs"])


JobStatus = Literal["pending", "running", "done", "failed"]

# JavaScript's Number(string): StringNumericLiteral, after trimming.
_JS_DECIMAL = re.compile(
    r"[+-]?(?:Infinity|(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)"
)
_JS_PREFIXED = {"0x": 16, "0o": 8, "0b": 2}
_JS_WHITESPACE = JS_WHITESPACE
_MAX_SAFE_INTEGER = 2**53 - 1


def _js_number(text: str) -> float:
    """``Number(text)`` as JavaScript computes it (NaN for anything else)."""
    s = text.strip(_JS_WHITESPACE)
    if s == "":
        return 0.0
    base = _JS_PREFIXED.get(s[:2].lower())
    if base is not None:
        try:
            return float(int(s[2:], base)) if s[2:].isalnum() else math.nan
        except ValueError:
            return math.nan
    if not _JS_DECIMAL.fullmatch(s):
        return math.nan
    return float(s.replace("Infinity", "inf"))


def _coerce_limit(value: Any) -> int:
    """zod's ``z.coerce.number().int().min(1).max(200)`` on a query value."""
    if isinstance(value, list):
        # Number() of a repeated key's array is NaN.
        raise ValueError("Invalid input: expected number, received NaN")
    number = _js_number(str(value))
    if math.isnan(number):
        raise ValueError("Invalid input: expected number, received NaN")
    if math.isinf(number) or number != int(number) or abs(number) > _MAX_SAFE_INTEGER:
        raise ValueError("Invalid input: expected int, received number")
    if number < 1:
        raise ValueError("Too small: expected number to be >=1")
    if number > 200:
        raise ValueError("Too big: expected number to be <=200")
    return int(number)


class ListQuery(ZodModel):
    """Query of ``GET /v1/admin/drive-jobs``."""

    status: JobStatus | None = Field(None, description="Only jobs in this status.")
    limit: Annotated[int, BeforeValidator(_coerce_limit)] = Field(
        50, description="How many jobs, 1 to 200. Default 50."
    )


class SummaryData(BaseModel):
    """Queue health."""

    by_status: dict[str, int] = Field(
        ..., description="Job counts, keyed only by statuses that have jobs."
    )
    submissions_without_copy: int = Field(
        ..., description="Submissions whose event copy does not exist (yet)."
    )


class SummaryResponse(BaseModel):
    """Queue health."""

    data: SummaryData = Field(..., description="The summary.")
    meta: Meta = Field(..., description="Response metadata.")


class DriveJobData(BaseModel):
    """A job as the operator list returns it."""

    id: str = Field(..., description="Job id.")
    kind: str = Field(..., description="copy, rename or trash.")
    status: str = Field(..., description="pending, running, done or failed.")
    attempts: int = Field(..., description="Attempts made.")
    last_error: str | None = Field(None, description="Why the last attempt failed.")
    next_attempt_at: int = Field(..., description="Next attempt due, epoch ms.")
    created_at: int = Field(..., description="Enqueued, epoch ms.")
    updated_at: int = Field(..., description="Last changed, epoch ms.")
    submission_id: str | None = Field(None, description="Set for copy and rename.")
    file_id: str | None = Field(None, description="Set for trash.")
    event_name: str | None = Field(
        None, description="Through the submission; null for trash or a gone row."
    )


class DriveJobListResponse(BaseModel):
    """Recent jobs, most recently updated first."""

    data: list[DriveJobData] = Field(..., description="The jobs.")
    meta: ListMeta = Field(..., description="Response metadata.")


class BackfillData(BaseModel):
    """What the backfill queued."""

    enqueued: int = Field(..., description="Rename jobs inserted.")


class BackfillResponse(BaseModel):
    """What the backfill queued."""

    data: BackfillData = Field(..., description="The count.")
    meta: Meta = Field(..., description="Response metadata.")


class RetryData(BaseModel):
    """The job as updated."""

    id: str = Field(..., description="Job id.")
    status: str = Field(..., description="Always 'pending'.")


class RetryResponse(BaseModel):
    """The job as updated."""

    data: RetryData = Field(..., description="The job.")
    meta: Meta = Field(..., description="Response metadata.")


AUTH: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid token."},
    403: {"model": ErrorResponse, "description": "Lacks the scope."},
}
INTERNAL: dict[int | str, dict[str, Any]] = {
    500: {"model": ErrorResponse, "description": "The query failed."},
}


def _internal() -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content=error_body(ErrorCode.INTERNAL, "Internal server error"),
    )


def _now_ms() -> int:
    return int(time.time() * 1000)


@router.get(
    "/summary",
    response_model=SummaryResponse,
    summary="Queue health",
    description=(
        "Job counts by status, and how many submissions have no event copy. "
        "Requires deejaytools.drivejobs.read."
    ),
    responses={**AUTH, **INTERNAL},
)
async def summary(
    _caller: Caller = Depends(require_scope("deejaytools.drivejobs.read")),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Queue health."""
    try:
        by_status = (
            await session.execute(
                select(DriveJob.status, func.count()).group_by(DriveJob.status)
            )
        ).all()
        uncopied = (
            await session.execute(
                select(func.count())
                .select_from(EventSongSubmission)
                .where(EventSongSubmission.drive_copy_file_id.is_(None))
            )
        ).scalar_one()
        return success(
            {
                "by_status": {status: int(count) for status, count in by_status},
                "submissions_without_copy": int(uncopied or 0),
            }
        )
    except Exception as exc:  # noqa: BLE001 - answered as deejaytools-api does
        logger.error(
            with_log_prefix(LOG_FAILURE, f"drive_jobs_summary_failed: {exc!r}")
        )
        return _internal()


@router.get(
    "",
    response_model=DriveJobListResponse,
    summary="Recent jobs",
    description=(
        "Jobs by updated_at descending, up to limit (default 50, at most 200), "
        "optionally in one status, with last_error. Requires "
        "deejaytools.drivejobs.read."
    ),
    responses={
        **AUTH,
        400: {"model": ErrorResponse, "description": "Unknown status, bad limit."},
        **INTERNAL,
    },
)
async def list_jobs(
    _caller: Caller = Depends(require_scope("deejaytools.drivejobs.read")),
    query: ListQuery = Depends(zod_query(ListQuery)),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Recent jobs."""
    try:
        stmt = (
            select(
                DriveJob.id,
                DriveJob.kind,
                DriveJob.status,
                DriveJob.attempts,
                DriveJob.last_error,
                DriveJob.next_attempt_at,
                DriveJob.created_at,
                DriveJob.updated_at,
                DriveJob.submission_id,
                DriveJob.file_id,
                Event.name.label("event_name"),
            )
            .select_from(DriveJob)
            .outerjoin(
                EventSongSubmission,
                EventSongSubmission.id == DriveJob.submission_id,
            )
            .outerjoin(Event, Event.id == EventSongSubmission.event_id)
        )
        if query.status:
            stmt = stmt.where(DriveJob.status == query.status)
        rows = (
            await session.execute(
                stmt.order_by(DriveJob.updated_at.desc()).limit(query.limit)
            )
        ).all()
        return success_list([dict(r._mapping) for r in rows])
    except Exception as exc:  # noqa: BLE001 - answered as deejaytools-api does
        logger.error(with_log_prefix(LOG_FAILURE, f"drive_jobs_list_failed: {exc!r}"))
        return _internal()


@router.post(
    "/backfill-renames",
    response_model=BackfillResponse,
    summary="Queue a rename for every copied submission",
    description=(
        "Inserts a rename job for each submission with an event copy, one at a "
        "time, re-applying the current naming rule. Safe to repeat. Requires "
        "deejaytools.drivejobs.write."
    ),
    responses={**AUTH, **INTERNAL},
)
async def backfill_renames(
    _caller: Caller = Depends(require_scope("deejaytools.drivejobs.write")),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Queue a rename for every copied submission."""
    try:
        ids = (
            (
                await session.execute(
                    select(EventSongSubmission.id).where(
                        EventSongSubmission.drive_copy_file_id.is_not(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        for submission_id in ids:
            await enqueue_drive_job(session, "rename", submission_id=submission_id)
        logger.info(
            with_log_prefix(
                LOG_SUCCESS, f"drive_rename_backfill_enqueued count={len(ids)}"
            )
        )
        return success({"enqueued": len(ids)})
    except Exception as exc:  # noqa: BLE001 - answered as deejaytools-api does
        await session.rollback()
        logger.error(
            with_log_prefix(LOG_FAILURE, f"drive_rename_backfill_failed: {exc!r}")
        )
        return _internal()


@router.post(
    "/{id}/retry",
    response_model=RetryResponse,
    summary="Retry an exhausted job",
    description=(
        "Returns a failed job to the queue: pending, zero attempts, due now. "
        "Any other status is a 409. Requires deejaytools.drivejobs.write."
    ),
    responses={
        **AUTH,
        404: {"model": ErrorResponse, "description": "No such job."},
        409: {"model": ErrorResponse, "description": "The job is not failed."},
    },
)
async def retry_job(
    id: Annotated[str, Path(description="Job id.")],
    _caller: Caller = Depends(require_scope("deejaytools.drivejobs.write")),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Retry an exhausted job."""
    existing = (
        await session.execute(
            select(DriveJob.id, DriveJob.status).where(DriveJob.id == id).limit(1)
        )
    ).first()
    if existing is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Drive job not found")
    if existing.status != "failed":
        raise api_error(
            409,
            "conflict",
            f"Job is {existing.status}, not failed — only exhausted jobs can be "
            "retried.",
        )

    # The status guard is repeated so a tick claiming the job between the
    # read and this write loses the race safely.
    now = _now_ms()
    updated = (
        await session.execute(
            update(DriveJob)
            .where(DriveJob.id == id, DriveJob.status == "failed")
            .values(status="pending", attempts=0, next_attempt_at=now, updated_at=now)
            .returning(DriveJob.id, DriveJob.status)
        )
    ).first()
    await session.commit()
    if updated is None:
        raise api_error(
            409,
            "conflict",
            "Job changed state before it could be retried — re-check and try again.",
        )
    logger.info(
        with_log_prefix(
            LOG_SUCCESS,
            f"drive_job_retry_requested job={id} previous_status={existing.status}",
        )
    )
    return success({"id": updated.id, "status": updated.status})
