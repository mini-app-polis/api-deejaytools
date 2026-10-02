"""``POST /v1/songs/upload/chunk`` (deejaytools-api docs/API.md, DRIVE.md "Upload pipeline").

Chunks are staged on local disk exactly as deejaytools-api stages them; the
final chunk is checked, assembled and turned into a song row, which is
returned at once with its Drive fields null. The build (tag, Drive upload,
record) then runs in the background from durable staging: see
``services/song_builds.py`` for how it differs from deejaytools-api's.

The body is ``multipart/form-data`` with no zod schema: fields are read and
checked by hand, in the order and with the messages API.md lists.
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
from ..errors import ErrorCode, ErrorResponse, Meta, api_error, success
from ..models import Partner, Song, SongUpload, Team, User
from ..services import song_builds
from ..services.audio_format import detect_audio_format
from ..song_records import (
    assert_managed_partnership_owned,
    assert_partner_owned,
    map_song,
)
from ..zod_coerce import js_number, js_trim

logger = get_logger()

router = APIRouter(prefix="/v1/songs", tags=["songs"])

CHUNK_TMP_BASE = Path("/tmp/dj-upload-chunks")
MAX_CHUNK_BYTES = 10 * 1024 * 1024
MAX_ASSEMBLED_BYTES = 110 * 1024 * 1024
CHUNK_TTL_SECONDS = 2 * 60 * 60
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
    # No response_model: a non-final answer omits "song" rather than nulling it.
    response_model=None,
    summary="Upload one chunk of a song",
    description=(
        "Chunked upload, multipart/form-data. Each chunk is staged; the final "
        "one is assembled, checked by its magic bytes and becomes a song, "
        "returned at once. Tagging, the Drive upload and the Drive fields "
        "follow in the background; the song is removed again if that fails "
        "and nothing references it yet. Requires deejaytools.songs.write; "
        "on_behalf_of_user_id also needs deejaytools.delegation.act."
    ),
    responses={
        200: {"model": ChunkResponse, "description": "Chunk received."},
        400: {"model": ErrorResponse, "description": "A field or the file is refused."},
        401: {"model": ErrorResponse, "description": "Missing token, or not synced."},
        403: {
            "model": ErrorResponse,
            "description": "Acting for another user, not allowed.",
        },
        409: {"model": ErrorResponse, "description": "Chunks missing (CHUNK_MISSING)."},
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
    entity_type = js_trim(_text(form, "entity_type") or "")
    entity_name = js_trim(_text(form, "entity_name") or "")
    team_id = js_trim(_text(form, "team_id") or "")
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
    if on_behalf_of:
        # Where deejaytools-api checked the admin role: a second, audited
        # decision (ADR-007). On every chunk, as there.
        await authorize_delegation(caller, on_behalf_of, db, request)

    chunk_bytes = await chunk_file.read()
    if len(chunk_bytes) > MAX_CHUNK_BYTES:
        raise bad("Chunk exceeds 10 MB limit")

    # Keyed by the caller, even when uploading for someone else.
    upload_dir = CHUNK_TMP_BASE / f"{caller.user_id}_{upload_id}"
    index, total = int(chunk_index), int(total_chunks)

    def write_chunk() -> None:
        upload_dir.mkdir(parents=True, exist_ok=True)
        (upload_dir / f"chunk_{index:06d}").write_bytes(chunk_bytes)

    await asyncio.to_thread(write_chunk)
    if index != total - 1:
        return success({"received": True, "complete": False})

    def refuse(message: str) -> Exception:
        _remove(upload_dir)
        return bad(message)

    is_portal = entity_type != ""
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
            # "other". deejaytools-api's third branch (solo) is unreachable:
            # any other entity_type was refused above.
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
            # Node takes the season when the upload is accepted; a build
            # retried across the October rollover keeps it.
            season_year=song_builds.season_year_now(),
            status="pending",
            attempts=0,
            next_attempt_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    await db.commit()

    song_builds.start_build(song.id)
    # Partner names are null in this answer whatever the partner, as in
    # deejaytools-api.
    return success({"received": True, "complete": True, "song": map_song(song)})
