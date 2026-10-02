"""``/v1/songs`` (deejaytools-api docs/API.md, src/routes/songs.ts).

A user's song library. Reads are behind ``deejaytools.songs.read``, changes
behind ``deejaytools.songs.write``; every route sees only the caller's own,
live (not soft-deleted) songs. The chunked upload, ``POST
/v1/songs/upload/chunk``, is not here.
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Any, ClassVar

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
    Partner,
    QueueEntry,
    Session,
    Song,
)
from ..services.drive_jobs import enqueue_trash_jobs
from ..song_records import (
    SongData,
    assert_partner_owned,
    load_song_with_partner,
    map_song,
)
from ..validation import NonEmptyStr, ZodModel
from ..zod_coerce import js_trim
from ..zod_types import QueryStr, zod_body, zod_query

logger = get_logger()

router = APIRouter(prefix="/v1/songs", tags=["songs"])


PARTNER_NOT_OWNED = "Partner not found or does not belong to you"


class ListQuery(ZodModel):
    """Query of ``GET /v1/songs``."""

    partner_id: QueryStr | None = Field(None, description="Only this partner's.")


class CreateSongBody(ZodModel):
    """Body of ``POST /v1/songs``: a metadata row with no file (legacy path)."""

    _NULLABLE: ClassVar[frozenset[str]] = frozenset(
        {"routine_name", "personal_descriptor"}
    )

    partner_id: str | None = Field(None, description="One of the caller's partners.")
    display_name: str | None = Field(None, description="Display name.")
    original_filename: str | None = Field(None, description="Original filename.")
    division: NonEmptyStr = Field(..., description="Division; required.")
    routine_name: str | None = Field(None, description="Routine name.")
    personal_descriptor: str | None = Field(None, description="Owner's descriptor.")
    season_year: str | None = Field(None, description="Season year.")


class PatchSongBody(ZodModel):
    """Body of ``PATCH /v1/songs/{id}``. Fields other than display_name may be
    null; values other than partner_id and display_name are stored as sent."""

    _NULLABLE: ClassVar[frozenset[str]] = frozenset(
        {
            "partner_id",
            "original_filename",
            "division",
            "routine_name",
            "personal_descriptor",
            "season_year",
        }
    )

    partner_id: str | None = Field(None, description="Partner; null or '' clears.")
    display_name: str | None = Field(None, description="Display name; blank clears.")
    original_filename: str | None = Field(None, description="Original filename.")
    division: str | None = Field(None, description="Division.")
    routine_name: str | None = Field(None, description="Routine name.")
    personal_descriptor: str | None = Field(None, description="Owner's descriptor.")
    season_year: str | None = Field(None, description="Season year.")


class SongResponse(BaseModel):
    """One song."""

    data: SongData = Field(..., description="The song.")
    meta: Meta = Field(..., description="Response metadata.")


class SongListResponse(BaseModel):
    """The caller's live songs, newest first."""

    data: list[SongData] = Field(..., description="The songs.")
    meta: ListMeta = Field(..., description="Response metadata.")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _blank_to_none(value: str | None) -> str | None:
    """``value?.trim() || null``."""
    return js_trim(value or "") or None


async def _load_live(session: AsyncSession, song_id: str, user_id: str) -> Song:
    row = (
        await session.execute(
            select(Song)
            .where(
                Song.id == song_id,
                Song.user_id == user_id,
                Song.deleted_at.is_(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Song not found")
    return row


SongId = Annotated[str, Path(description="Song id.")]
AUTH: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid token."},
    403: {"model": ErrorResponse, "description": "Lacks the scope."},
}
NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "No such live song of the caller's."}
}
BAD_PARTNER: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Not the caller's partner."}
}


@router.get(
    "",
    response_model=SongListResponse,
    summary="List songs",
    description=(
        "The caller's live songs, newest first, with partner and managed "
        "partnership names; optionally one partner's (query partner_id). "
        "Requires deejaytools.songs.read."
    ),
    responses=AUTH,
)
async def list_songs(
    caller: Caller = Depends(require_scope("deejaytools.songs.read")),
    query: ListQuery = Depends(zod_query(ListQuery)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List the caller's songs."""
    conditions = [Song.user_id == caller.user_id, Song.deleted_at.is_(None)]
    if query.partner_id:
        conditions.append(Song.partner_id == query.partner_id)
    rows = (
        await session.execute(
            select(
                Song,
                Partner.first_name,
                Partner.last_name,
                Partner.kind,
                ManagedPartnership.leader_first_name,
                ManagedPartnership.leader_last_name,
                ManagedPartnership.follower_first_name,
                ManagedPartnership.follower_last_name,
            )
            .outerjoin(Partner, Partner.id == Song.partner_id)
            .outerjoin(
                ManagedPartnership,
                ManagedPartnership.id == Song.managed_partnership_id,
            )
            .where(*conditions)
            .order_by(Song.created_at.desc())
        )
    ).all()
    return success_list(
        [
            map_song(
                r[0],
                partner_first_name=r[1],
                partner_last_name=r[2],
                partner_kind=r[3],
                managed_leader_first_name=r[4],
                managed_leader_last_name=r[5],
                managed_follower_first_name=r[6],
                managed_follower_last_name=r[7],
            )
            for r in rows
        ]
    )


@router.post(
    "",
    status_code=201,
    response_model=SongResponse,
    summary="Create a song record",
    description=(
        "A metadata row with no file (legacy path; uploads use the chunked "
        "route). Text fields are trimmed, blanks stored as null; the display "
        "name defaults to the routine name, then the original filename. The "
        "response carries no partner names. Requires deejaytools.songs.write."
    ),
    responses={**AUTH, **BAD_PARTNER},
)
async def create_song(
    caller: Caller = Depends(require_scope("deejaytools.songs.write")),
    body: CreateSongBody = Depends(zod_body(CreateSongBody)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Create a song record."""
    if body.partner_id and not await assert_partner_owned(
        session, caller.user_id, body.partner_id
    ):
        raise api_error(400, ErrorCode.BAD_REQUEST, PARTNER_NOT_OWNED)

    now = _now_ms()
    song_id = str(uuid.uuid4())
    session.add(
        Song(
            id=song_id,
            user_id=caller.user_id,
            partner_id=body.partner_id or None,
            display_name=(
                _blank_to_none(body.display_name)
                or _blank_to_none(body.routine_name)
                or _blank_to_none(body.original_filename)
            ),
            original_filename=_blank_to_none(body.original_filename),
            processed_filename=None,
            division=_blank_to_none(body.division),
            routine_name=_blank_to_none(body.routine_name),
            personal_descriptor=_blank_to_none(body.personal_descriptor),
            season_year=_blank_to_none(body.season_year),
            drive_file_id=None,
            drive_folder_id=None,
            created_at=now,
            updated_at=now,
        )
    )
    await session.commit()
    row = await session.get(Song, song_id, populate_existing=True)
    assert row is not None
    # No join: deejaytools-api answers with null partner names here.
    return success(map_song(row))


@router.get(
    "/{id}",
    response_model=SongResponse,
    summary="Get a song",
    description=(
        "One of the caller's live songs, with its partner's names (managed "
        "partnership names are null here). Requires deejaytools.songs.read."
    ),
    responses={**AUTH, **NOT_FOUND},
)
async def get_song(
    id: SongId,
    caller: Caller = Depends(require_scope("deejaytools.songs.read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Get one song."""
    data = await load_song_with_partner(session, id, user_id=caller.user_id)
    if data is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Song not found")
    return success(data)


@router.patch(
    "/{id}",
    response_model=SongResponse,
    summary="Update a song",
    description=(
        "Only the fields sent change. partner_id null or '' clears it; a blank "
        "display_name is stored as null; other fields are stored as sent. "
        "Requires deejaytools.songs.write."
    ),
    responses={**AUTH, **NOT_FOUND, **BAD_PARTNER},
)
async def patch_song(
    id: SongId,
    caller: Caller = Depends(require_scope("deejaytools.songs.write")),
    body: PatchSongBody = Depends(zod_body(PatchSongBody)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Update a song's metadata."""
    await _load_live(session, id, caller.user_id)
    if body.partner_id and not await assert_partner_owned(
        session, caller.user_id, body.partner_id
    ):
        raise api_error(400, ErrorCode.BAD_REQUEST, PARTNER_NOT_OWNED)

    sent = body.model_fields_set
    values: dict[str, Any] = {"updated_at": _now_ms()}
    if "partner_id" in sent:
        values["partner_id"] = body.partner_id or None
    if "display_name" in sent:
        values["display_name"] = _blank_to_none(body.display_name)
    for field in (
        "division",
        "routine_name",
        "personal_descriptor",
        "season_year",
        "original_filename",
    ):
        if field in sent:
            values[field] = getattr(body, field)
    await session.execute(update(Song).where(Song.id == id).values(**values))
    await session.commit()
    data = await load_song_with_partner(session, id)
    assert data is not None
    return success(data)


@router.delete(
    "/{id}",
    response_model=None,  # 204: no body
    status_code=204,
    response_class=Response,
    summary="Delete a song",
    description=(
        "Refused while the song has a live queue entry in a session that is "
        "not completed or cancelled. Soft-deletes the song and deletes its "
        "event submissions; after commit, queues the song's own Drive file "
        "and its submissions' event copies for trashing. Requires "
        "deejaytools.songs.write."
    ),
    responses={
        **AUTH,
        **NOT_FOUND,
        409: {"model": ErrorResponse, "description": "Active check-in."},
        500: {"model": ErrorResponse, "description": "The delete failed."},
    },
)
async def delete_song(
    id: SongId,
    caller: Caller = Depends(require_scope("deejaytools.songs.write")),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """Soft-delete a song."""
    user_id = caller.user_id
    await _load_live(session, id, user_id)

    # Over sessions that are not finished: a finished session's queue
    # entries are history and do not block the delete.
    active = await session.execute(
        select(Checkin.id)
        .join(QueueEntry, QueueEntry.checkin_id == Checkin.id)
        .join(Session, Session.id == Checkin.session_id)
        .where(
            Checkin.song_id == id,
            Session.status.not_in(["completed", "cancelled"]),
        )
        .limit(1)
    )
    if active.first() is not None:
        raise api_error(
            409,
            "SONG_IN_ACTIVE_CHECKIN",
            "This song is referenced by an active check-in. "
            "Complete or withdraw the check-in first.",
        )

    # Fix of a DRIVE.md known defect: deejaytools-api moved the song's own
    # file to _deprecated inline, before the transaction, so a failed
    # transaction left a live song with a deprecated file. Its file is now
    # queued for trashing after commit, with the event copies.
    try:
        # The row stays for the run and check-in history that references it.
        # Its file id comes from RETURNING, not the read above: the UPDATE
        # waits for a build finishing on the row and sees the file it
        # recorded (a build finishing after sees deleted_at and discards it).
        marked = (
            await session.execute(
                update(Song)
                .where(Song.id == id, Song.user_id == user_id)
                .values(deleted_at=_now_ms())
                .returning(Song.drive_file_id, Song.drive_folder_id)
            )
        ).first()
        own_file = (
            [marked.drive_file_id]
            if marked is not None and marked.drive_file_id and marked.drive_folder_id
            else []
        )
        copies = [
            file_id
            for file_id in (
                await session.execute(
                    delete(EventSongSubmission)
                    .where(EventSongSubmission.song_id == id)
                    .returning(EventSongSubmission.drive_copy_file_id)
                )
            ).scalars()
            if file_id is not None
        ]
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - answered as deejaytools-api does
        await session.rollback()
        logger.error(
            with_log_prefix(
                LOG_FAILURE, f"song_delete_failed song={id} user={user_id}: {exc!r}"
            )
        )
        return JSONResponse(
            status_code=500,
            content=error_body(ErrorCode.INTERNAL, "Failed to delete song"),
        )

    await enqueue_trash_jobs(
        session,
        own_file + copies,
        source="song_delete",
        context={"song_id": id, "user_id": user_id},
    )
    return Response(status_code=204)
