"""``POST /v1/songs/upload/chunk`` (docs/API.md, docs/DRIVE.md "Upload pipeline").

Chunks are staged on local disk; the final chunk is checked, assembled and
turned into a song row, which is returned at once with its Drive fields
null. The build (tag, Drive upload, record) then runs in the background from
durable staging (``services/song_builds.py``).

The body is ``multipart/form-data`` with no zod schema: fields are read and
checked by hand, in the order and with the messages docs/API.md lists.

A staff upload on behalf can also submit the song to the event it is for
(``submit_to_event``): the dancer is the submitter, the staff member the
uploader, and a taken slot (ENTITY_SLOT_TAKEN) is refused before anything is
saved. api-deejaytools only.
"""

from __future__ import annotations

import asyncio
import math
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from mini_app_polis.logger import LOG_FAILURE, get_logger, with_log_prefix
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from ..auth import Caller, authorize_delegation, require_scope
from ..database import get_db_session
from ..domain import song_entity_key
from ..errors import ErrorCode, ErrorResponse, Meta, api_error, success
from ..models import Event, EventSongSubmission, Partner, Song, SongUpload, Team, User
from ..services import song_builds
from ..services.audio_format import detect_audio_format
from ..services.drive_jobs import enqueue_copy_job
from ..song_records import (
    assert_managed_partnership_owned,
    assert_partner_owned,
    map_song,
)
from ..submissions import (
    DEFAULT_ROUND,
    entity_slot_taken,
    fetch_user_submission_rows,
    map_submission_row,
    slot_taken_message,
)
from ..zod_coerce import js_number, js_trim

logger = get_logger()

router = APIRouter(prefix="/v1/songs", tags=["songs"])

CHUNK_TMP_BASE = Path("/tmp/dj-upload-chunks")
MAX_CHUNK_BYTES = 10 * 1024 * 1024
MAX_ASSEMBLED_BYTES = 110 * 1024 * 1024
CHUNK_TTL_SECONDS = 2 * 60 * 60
MAX_UPLOADS_IN_PROGRESS = 3
"""Uploads one caller may have staged at once; a new one past this is 429."""
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)


class ChunkReceived(BaseModel):
    """Answer to a chunk: ``complete`` is true on the final one, with the song."""

    received: bool = Field(True, description="Always true.")
    complete: bool = Field(..., description="Whether this was the final chunk.")
    song: dict[str, Any] | None = Field(
        None, description="The new song (final chunk only); Drive fields null."
    )
    submission: dict[str, Any] | None = Field(
        None,
        description=(
            "The song's submission to the event (final chunk with "
            "submit_to_event only)."
        ),
    )


class ChunkResponse(BaseModel):
    """Chunk acknowledgement envelope."""

    data: ChunkReceived = Field(..., description="Acknowledgement.")
    meta: Meta = Field(..., description="Response metadata.")


def _sweep_stale_dirs() -> None:
    """Remove upload directories untouched for two hours. Never raises."""
    try:
        entries = list(CHUNK_TMP_BASE.iterdir())
    except OSError:
        return
    cutoff = time.time() - CHUNK_TTL_SECONDS
    for entry in entries:
        try:
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue


def _remove(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)


def _text(form: Any, key: str) -> str | None:
    """A form field as text, or None when absent or a file (``typeof === "string"``)."""
    value = form.get(key)
    return value if isinstance(value, str) else None


def _trimmed_or_none(form: Any, key: str) -> str | None:
    value = _text(form, key)
    return (js_trim(value) or None) if value is not None else None


def _number(form: Any, key: str, absent: float) -> float:
    """``Number(body[key] ?? absent)``: a file is NaN, as Number(File) is."""
    value = form.get(key)
    if value is None:
        return absent
    if isinstance(value, UploadFile):
        return math.nan
    return js_number(value)


def _is_int(value: float) -> bool:
    return math.isfinite(value) and value == int(value)


@router.post(
    "/upload/chunk",
    # Unset fields stay out: a non-final answer omits "song" rather than
    # nulling it, as the web app expects.
    response_model=ChunkResponse,
    response_model_exclude_unset=True,
    summary="Upload one chunk of a song",
    description=(
        "Chunked upload, multipart/form-data. Each chunk is staged; the final "
        "one is assembled, checked by its magic bytes and becomes a song, "
        "returned at once. Tagging, the Drive upload and the Drive fields "
        "follow in the background; the song is removed again if that fails "
        "and nothing references it yet. Requires deejaytools.songs.write; "
        "on_behalf_of_user_id also needs deejaytools.delegation.act: through "
        "a role (an admin's), or on the event event_id names (that event's "
        "DJ or event manager). submit_to_event=true (with on_behalf_of_user_id "
        "and event_id) also submits the song to that event in the same "
        "request, the dancer as submitter, refused with 409 "
        "ENTITY_SLOT_TAKEN before anything is saved when the slot is taken."
    ),
    responses={
        200: {"model": ChunkResponse, "description": "Chunk received."},
        400: {"model": ErrorResponse, "description": "A field or the file is refused."},
        401: {"model": ErrorResponse, "description": "Missing token, or not synced."},
        403: {
            "model": ErrorResponse,
            "description": "Acting for another user, not allowed.",
        },
        404: {
            "model": ErrorResponse,
            "description": "submit_to_event: no such event.",
        },
        409: {
            "model": ErrorResponse,
            "description": (
                "Chunks missing (CHUNK_MISSING), or with submit_to_event the "
                "entity's slot is taken (ENTITY_SLOT_TAKEN)."
            ),
        },
        500: {
            "model": ErrorResponse,
            "description": "Chunks unreadable (CHUNK_ERROR).",
        },
    },
)
async def upload_chunk(
    request: Request,
    caller: Caller = Depends(require_scope("deejaytools.songs.write")),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Stage one chunk; on the final one, create the song and start its build."""
    asyncio.get_running_loop().run_in_executor(None, _sweep_stale_dirs)

    form = await request.form(max_part_size=MAX_CHUNK_BYTES * 2)
    upload_id = js_trim(_text(form, "upload_id") or "")
    chunk_index = _number(form, "chunk_index", -1)
    total_chunks = _number(form, "total_chunks", 0)
    original_raw = _text(form, "original_filename")
    original_name = (
        (js_trim(original_raw) or "song.mp3")
        if original_raw is not None
        else "song.mp3"
    )
    division = js_trim(_text(form, "division") or "")
    partner_id = _trimmed_or_none(form, "partner_id")
    managed_partnership_id = _trimmed_or_none(form, "managed_partnership_id")
    routine_name = _trimmed_or_none(form, "routine_name")
    personal_descriptor = _trimmed_or_none(form, "personal_descriptor")
    on_behalf_of = _trimmed_or_none(form, "on_behalf_of_user_id")
    # The event an upload on behalf is for (ADR-0012): what the event's DJ or
    # event manager is authorized against. Not stored, and ignored otherwise.
    event_id = _trimmed_or_none(form, "event_id")
    entity_type = js_trim(_text(form, "entity_type") or "")
    entity_name = js_trim(_text(form, "entity_name") or "")
    team_id = js_trim(_text(form, "team_id") or "")
    submit_to_event = _text(form, "submit_to_event") == "true"
    chunk = form.get("chunk")
    chunk_file = chunk if isinstance(chunk, UploadFile) else None

    def bad(message: str) -> Exception:
        return api_error(400, ErrorCode.BAD_REQUEST, message)

    if not _UUID.fullmatch(upload_id):
        raise bad("Invalid upload_id")
    if not division:
        raise bad("division is required")
    if not _is_int(chunk_index) or chunk_index < 0:
        raise bad("Invalid chunk_index")
    if not _is_int(total_chunks) or not 1 <= total_chunks <= 30:
        raise bad("Invalid total_chunks (max 30)")
    if chunk_index >= total_chunks:
        raise bad("chunk_index out of range")
    if chunk_file is None:
        raise bad("Missing chunk field")
    if submit_to_event and not (on_behalf_of and event_id):
        raise bad("submit_to_event needs on_behalf_of_user_id and event_id")
    # The event the song is submitted to, or None when it is not submitted.
    submit_event_id = event_id if submit_to_event else None
    if on_behalf_of:
        # A second, audited decision (ADR-0008), on event_id's event when
        # one is sent (ADR-0012). On every chunk.
        await authorize_delegation(caller, on_behalf_of, db, request, event_id=event_id)

    chunk_bytes = await chunk_file.read()
    if len(chunk_bytes) > MAX_CHUNK_BYTES:
        raise bad("Chunk exceeds 10 MB limit")

    # Keyed by the caller, even when uploading for someone else.
    upload_dir = CHUNK_TMP_BASE / f"{caller.user_id}_{upload_id}"
    index, total = int(chunk_index), int(total_chunks)

    def write_chunk() -> str | None:
        """Stage the chunk, or say why not: the staging area is bounded per
        caller and per upload, so no one can fill the disk with it."""
        if not upload_dir.exists():
            prefix = f"{caller.user_id}_"
            try:
                staged = sum(
                    1 for d in CHUNK_TMP_BASE.iterdir() if d.name.startswith(prefix)
                )
            except OSError:
                staged = 0
            if staged >= MAX_UPLOADS_IN_PROGRESS:
                return "too_many"
        upload_dir.mkdir(parents=True, exist_ok=True)
        target = upload_dir / f"chunk_{index:06d}"
        others = sum(
            f.stat().st_size for f in upload_dir.iterdir() if f.name != target.name
        )
        if others + len(chunk_bytes) > MAX_ASSEMBLED_BYTES:
            _remove(upload_dir)
            return "too_large"
        target.write_bytes(chunk_bytes)
        return None

    refused = await asyncio.to_thread(write_chunk)
    if refused == "too_many":
        raise api_error(
            429,
            "TOO_MANY_UPLOADS",
            f"You have {MAX_UPLOADS_IN_PROGRESS} uploads in progress. "
            "Finish or wait for one before starting another.",
        )
    if refused == "too_large":
        raise bad("File exceeds 100 MB limit")
    if index != total - 1:
        return success({"received": True, "complete": False})

    def refuse(message: str) -> Exception:
        _remove(upload_dir)
        return bad(message)

    is_portal = entity_type != ""
    if submit_to_event and is_portal:
        raise refuse("submit_to_event is not available for portal uploads")
    if is_portal and (managed_partnership_id or partner_id):
        raise refuse(
            "Portal uploads cannot specify partner_id or managed_partnership_id"
        )
    if is_portal and entity_type not in ("team", "other"):
        raise refuse("Invalid entity_type")

    effective_user_id = caller.user_id
    if on_behalf_of:
        if await db.get(User, on_behalf_of) is None:
            raise refuse("Target user not found")
        effective_user_id = on_behalf_of

    placeholder_name: str | None = None
    if is_portal:
        if entity_type == "team":
            if not team_id:
                raise refuse("team_id is required for team uploads")
            identifier = (
                await db.execute(
                    select(Team.identifier).where(
                        Team.id == team_id, Team.user_id == effective_user_id
                    )
                )
            ).scalar_one_or_none()
            if identifier is None:
                raise refuse("Team not found")
            placeholder_name = identifier
        else:
            # "other": any other entity_type was refused above.
            if not entity_name:
                raise refuse("entity_name is required for other uploads")
            placeholder_name = entity_name
    else:
        if managed_partnership_id and partner_id:
            raise refuse(
                "Specify either partner_id or managed_partnership_id, not both"
            )
        if managed_partnership_id:
            if not await assert_managed_partnership_owned(
                db, effective_user_id, managed_partnership_id
            ):
                raise refuse("Managed partnership not found or does not belong to you")
        elif partner_id:
            if not await assert_partner_owned(db, effective_user_id, partner_id):
                raise refuse("Partner not found or does not belong to you")

    if submit_event_id is not None:
        # The partner comes from the dancer's partner list (kind partner),
        # and the slot is checked before anything is saved.
        if partner_id:
            kind = (
                await db.execute(select(Partner.kind).where(Partner.id == partner_id))
            ).scalar_one_or_none()
            if kind != "partner":
                raise refuse("Partner is not on the dancer's partner list")
        if await db.get(Event, submit_event_id) is None:
            _remove(upload_dir)
            raise api_error(404, ErrorCode.NOT_FOUND, "Event not found")
        if await entity_slot_taken(
            db,
            event_id=submit_event_id,
            entity=song_entity_key(
                effective_user_id,
                None if managed_partnership_id else partner_id,
                managed_partnership_id,
            ),
            division=division,
            round_=DEFAULT_ROUND,
        ):
            _remove(upload_dir)
            raise api_error(409, "ENTITY_SLOT_TAKEN", slot_taken_message(division))

    try:
        names = sorted(
            p.name for p in await asyncio.to_thread(lambda: list(upload_dir.iterdir()))
        )
    except OSError as exc:
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"chunk_readdir_failed user={caller.user_id} upload={upload_id}: {exc!r}",
            )
        )
        raise api_error(500, "CHUNK_ERROR", "Failed to read uploaded chunks") from exc
    if len(names) != total:
        _remove(upload_dir)
        raise api_error(
            409,
            "CHUNK_MISSING",
            f"Expected {total} chunks but only received {len(names)}. Please retry the upload.",
        )

    def assemble() -> bytes:
        data = b"".join((upload_dir / name).read_bytes() for name in names)
        _remove(upload_dir)
        return data

    assembled = await asyncio.to_thread(assemble)
    if len(assembled) > MAX_ASSEMBLED_BYTES:
        raise bad("File exceeds 100 MB limit")
    mime_type = detect_audio_format(assembled)
    if mime_type is None:
        raise api_error(
            400,
            "UNSUPPORTED_FORMAT",
            "That file doesn't look like a supported audio format. "
            "Please upload an MP3, WAV, FLAC, or M4A.",
        )

    now = int(time.time() * 1000)
    resolved_partner_id = partner_id
    if is_portal and placeholder_name:
        existing = (
            await db.execute(
                select(Partner.id)
                .where(
                    Partner.user_id == effective_user_id,
                    Partner.kind == entity_type,
                    Partner.first_name == placeholder_name,
                    Partner.last_name == "",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = str(uuid.uuid4())
            db.add(
                Partner(
                    id=existing,
                    user_id=effective_user_id,
                    first_name=placeholder_name,
                    last_name="",
                    partner_role="follower",
                    kind=entity_type,
                    created_at=now,
                    updated_at=now,
                )
            )
        resolved_partner_id = existing

    song = Song(
        id=str(uuid.uuid4()),
        user_id=effective_user_id,
        partner_id=None if managed_partnership_id else resolved_partner_id,
        managed_partnership_id=managed_partnership_id,
        display_name=routine_name or original_name or None,
        original_filename=original_name,
        processed_filename=None,
        division=division or None,
        routine_name=routine_name,
        personal_descriptor=personal_descriptor,
        season_year=None,
        drive_file_id=None,
        drive_folder_id=None,
        created_at=now,
        updated_at=now,
    )
    db.add(song)
    await db.flush()
    # Staged in the same transaction as the song: a song row never exists
    # without the bytes its build needs.
    db.add(
        SongUpload(
            song_id=song.id,
            data=assembled,
            mime_type=mime_type,
            original_filename=original_name,
            # The season is taken when the upload is accepted; a build
            # retried across the October rollover keeps it.
            season_year=song_builds.season_year_now(),
            status="pending",
            attempts=0,
            next_attempt_at=now,
            created_at=now,
            updated_at=now,
            # For the "song added" notification, which the build posts.
            uploaded_by_user_id=(
                caller.user_id if effective_user_id != caller.user_id else None
            ),
        )
    )
    submission_id: str | None = None
    if submit_event_id is not None:
        # The song's own division and the default round: no override.
        submission_id = str(uuid.uuid4())
        db.add(
            EventSongSubmission(
                id=submission_id,
                event_id=submit_event_id,
                song_id=song.id,
                submitted_by_user_id=effective_user_id,
                division=None,
                round=None,
                created_at=now,
            )
        )
    await db.commit()

    song_builds.start_build(song.id)
    # Partner names are null in this answer whatever the partner.
    answer: dict[str, Any] = {
        "received": True,
        "complete": True,
        "song": map_song(song),
    }
    if submission_id is not None:
        # Finds no file until the build has one; the build re-queues it then.
        await enqueue_copy_job(
            db,
            submission_id,
            context={"submission_id": submission_id, "user_id": effective_user_id},
        )
        rows = await fetch_user_submission_rows(
            db, effective_user_id, submission_id=submission_id
        )
        answer["submission"] = map_submission_row(rows[0])
    return success(answer)
