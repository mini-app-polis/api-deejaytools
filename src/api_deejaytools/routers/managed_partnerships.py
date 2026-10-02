"""``/v1/managed-partnerships`` (deejaytools-api docs/API.md,
src/routes/managed-partnerships.ts).

Couples a user manages by name only. Reads are behind
``deejaytools.partnerships.read``, changes behind
``deejaytools.partnerships.write``; every route sees only the caller's own,
live (not soft-deleted) partnerships.
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Response
from fastapi.responses import JSONResponse
from mini_app_polis.logger import LOG_FAILURE, get_logger, with_log_prefix
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
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
from ..models import (
    Checkin,
    EventSongSubmission,
    ManagedPartnership,
    QueueEntry,
    Session,
    Song,
)
from ..services.drive_jobs import enqueue_trash_jobs
from ..text import title_case_words
from ..validation import ZodModel
from ..zod_types import trimmed, zod_body

logger = get_logger()

router = APIRouter(prefix="/v1/managed-partnerships", tags=["managed-partnerships"])

READ_SCOPE = "deejaytools.partnerships.read"
WRITE_SCOPE = "deejaytools.partnerships.write"

Name = Annotated[str, trimmed(min_length=1, max_length=100)]


class PartnershipBody(ZodModel):
    """Body of ``POST`` and ``PATCH``. Names are trimmed, then title-cased."""

    leader_first_name: Name = Field(..., description="Leader's first name.")
    leader_last_name: Name = Field(..., description="Leader's last name.")
    follower_first_name: Name = Field(..., description="Follower's first name.")
    follower_last_name: Name = Field(..., description="Follower's last name.")


class PartnershipData(BaseModel):
    """A managed partnership as the API returns it."""

    id: str = Field(..., description="Managed partnership id.")
    user_id: str = Field(..., description="users.id of the manager.")
    leader_first_name: str = Field(..., description="Leader's first name.")
    leader_last_name: str = Field(..., description="Leader's last name.")
    follower_first_name: str = Field(..., description="Follower's first name.")
    follower_last_name: str = Field(..., description="Follower's last name.")
    created_at: int = Field(..., description="Created, epoch ms.")
    updated_at: int = Field(..., description="Last updated, epoch ms.")


class PartnershipResponse(BaseModel):
    """One managed partnership."""

    data: PartnershipData = Field(..., description="The partnership.")
    meta: Meta = Field(..., description="Response metadata.")


class PartnershipListResponse(BaseModel):
    """The caller's live managed partnerships, newest first."""

    data: list[PartnershipData] = Field(..., description="The partnerships.")
    meta: ListMeta = Field(..., description="Response metadata.")


def map_managed_partnership(row: ManagedPartnership) -> dict[str, Any]:
    """A managed_partnerships row on the wire."""
    return {
        "id": row.id,
        "user_id": row.user_id,
        "leader_first_name": row.leader_first_name,
        "leader_last_name": row.leader_last_name,
        "follower_first_name": row.follower_first_name,
        "follower_last_name": row.follower_last_name,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _now_ms() -> int:
    return int(time.time() * 1000)


def _internal(message: str = "Internal server error") -> JSONResponse:
    return JSONResponse(
        status_code=500, content=error_body(ErrorCode.INTERNAL, message)
    )


async def _load_live(
    session: AsyncSession, partnership_id: str, user_id: str
) -> ManagedPartnership:
    row = (
        await session.execute(
            select(ManagedPartnership)
            .where(
                ManagedPartnership.id == partnership_id,
                ManagedPartnership.user_id == user_id,
                ManagedPartnership.deleted_at.is_(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Managed partnership not found")
    return row


def _titled(body: PartnershipBody) -> dict[str, str]:
    return {
        "leader_first_name": title_case_words(body.leader_first_name),
        "leader_last_name": title_case_words(body.leader_last_name),
        "follower_first_name": title_case_words(body.follower_first_name),
        "follower_last_name": title_case_words(body.follower_last_name),
    }


PartnershipId = Annotated[str, Path(description="Managed partnership id.")]
AUTH: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid token."},
    403: {"model": ErrorResponse, "description": "Lacks the scope."},
}
NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {
        "model": ErrorResponse,
        "description": "No such live partnership of the caller's.",
    }
}
INTERNAL: dict[int | str, dict[str, Any]] = {
    500: {"model": ErrorResponse, "description": "The write failed."}
}


@router.get(
    "",
    response_model=PartnershipListResponse,
    summary="List managed partnerships",
    description=(
        "The caller's live managed partnerships, newest first. Requires "
        "deejaytools.partnerships.read."
    ),
    responses=AUTH,
)
async def list_managed_partnerships(
    caller: Caller = Depends(require_scope(READ_SCOPE)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List the caller's managed partnerships."""
    rows = (
        (
            await session.execute(
                select(ManagedPartnership)
                .where(
                    ManagedPartnership.user_id == caller.user_id,
                    ManagedPartnership.deleted_at.is_(None),
                )
                .order_by(ManagedPartnership.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return success_list([map_managed_partnership(r) for r in rows])


@router.post(
    "",
    status_code=201,
    response_model=PartnershipResponse,
    summary="Create a managed partnership",
    description=(
        "Names are trimmed, whitespace collapsed and each word's first letter "
        "capitalized. Requires deejaytools.partnerships.write."
    ),
    responses={**AUTH, **INTERNAL},
)
async def create_managed_partnership(
    caller: Caller = Depends(require_scope(WRITE_SCOPE)),
    body: PartnershipBody = Depends(zod_body(PartnershipBody)),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Create a managed partnership."""
    now = _now_ms()
    partnership_id = str(uuid.uuid4())
    try:
        session.add(
            ManagedPartnership(
                id=partnership_id,
                user_id=caller.user_id,
                created_at=now,
                updated_at=now,
                **_titled(body),
            )
        )
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - answered as deejaytools-api does
        await session.rollback()
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"managed_partnership_create_failed user={caller.user_id}: {exc!r}",
            )
        )
        return _internal()
    row = await session.get(ManagedPartnership, partnership_id)
    assert row is not None
    return success(map_managed_partnership(row))


@router.patch(
    "/{id}",
    response_model=PartnershipResponse,
    summary="Update a managed partnership",
    description=(
        "Replaces all four names, cased as on create. Requires "
        "deejaytools.partnerships.write."
    ),
    responses={**AUTH, **NOT_FOUND, **INTERNAL},
)
async def patch_managed_partnership(
    id: PartnershipId,
    caller: Caller = Depends(require_scope(WRITE_SCOPE)),
    body: PartnershipBody = Depends(zod_body(PartnershipBody)),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Update a managed partnership's names."""
    await _load_live(session, id, caller.user_id)
    try:
        await session.execute(
            update(ManagedPartnership)
            .where(
                ManagedPartnership.id == id,
                ManagedPartnership.user_id == caller.user_id,
            )
            .values(updated_at=_now_ms(), **_titled(body))
        )
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - answered as deejaytools-api does
        await session.rollback()
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"managed_partnership_update_failed user={caller.user_id} "
                f"managed_partnership={id}: {exc!r}",
            )
        )
        return _internal()
    row = await session.get(ManagedPartnership, id, populate_existing=True)
    assert row is not None
    return success(map_managed_partnership(row))


@router.delete(
    "/{id}",
    status_code=204,
    response_class=Response,
    summary="Delete a managed partnership",
    description=(
        "Refused while it has a live queue entry in a session that is not "
        "completed or cancelled. Soft-deletes the partnership and its live "
        "songs and deletes their event submissions; after commit, queues "
        "the submissions' Drive copies and the songs' own Drive files for "
        "trashing. Requires deejaytools.partnerships.write."
    ),
    responses={
        **AUTH,
        **NOT_FOUND,
        409: {"model": ErrorResponse, "description": "Active check-in."},
        500: {"model": ErrorResponse, "description": "The delete failed."},
    },
)
async def delete_managed_partnership(
    id: PartnershipId,
    caller: Caller = Depends(require_scope(WRITE_SCOPE)),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """Soft-delete a managed partnership and its songs."""
    await _load_live(session, id, caller.user_id)

    active = await session.execute(
        select(Checkin.id)
        .join(QueueEntry, QueueEntry.checkin_id == Checkin.id)
        .join(Session, Session.id == Checkin.session_id)
        .where(
            Checkin.entity_managed_partnership_id == id,
            Session.status.not_in(["completed", "cancelled"]),
        )
        .limit(1)
    )
    if active.first() is not None:
        raise api_error(
            409,
            "MANAGED_PARTNERSHIP_IN_ACTIVE_CHECKIN",
            "This partnership has an active check-in. Complete or withdraw it first.",
        )

    now = _now_ms()
    orphaned: list[str] = []
    try:
        # File ids come from RETURNING, not an earlier read: an UPDATE waits
        # for a build or copy job holding the row and sees what it recorded.
        songs = (
            await session.execute(
                update(Song)
                .where(Song.managed_partnership_id == id, Song.deleted_at.is_(None))
                .values(deleted_at=now)
                .returning(Song.id, Song.drive_file_id, Song.drive_folder_id)
            )
        ).all()
        song_ids = [s.id for s in songs]
        if song_ids:
            orphaned = [
                file_id
                for file_id in (
                    await session.execute(
                        delete(EventSongSubmission)
                        .where(EventSongSubmission.song_id.in_(song_ids))
                        .returning(EventSongSubmission.drive_copy_file_id)
                    )
                ).scalars()
                if file_id is not None
            ]
            # Fix of a DRIVE.md known defect: deejaytools-api never deprecated
            # these songs' own files, only their event copies. They are queued
            # for trashing too, on the same condition as a song delete.
            orphaned += [
                s.drive_file_id for s in songs if s.drive_file_id and s.drive_folder_id
            ]
        # The row stays for the run and check-in history that references it.
        await session.execute(
            update(ManagedPartnership)
            .where(
                ManagedPartnership.id == id,
                ManagedPartnership.user_id == caller.user_id,
            )
            .values(deleted_at=now)
        )
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - answered as deejaytools-api does
        await session.rollback()
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"managed_partnership_delete_failed user={caller.user_id} "
                f"managed_partnership={id}: {exc!r}",
            )
        )
        return _internal("Failed to delete managed partnership")

    await enqueue_trash_jobs(
        session,
        orphaned,
        source="partnership_delete",
        context={"user_id": caller.user_id, "managed_partnership_id": id},
    )
    return Response(status_code=204)
