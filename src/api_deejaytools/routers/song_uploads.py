"""``POST /v1/songs/upload/chunk`` (docs/API.md, docs/DRIVE.md "Upload pipeline").

Chunks are staged on local disk; the final chunk is checked, assembled and
turned into a song row, which is returned at once with its Drive fields
null. The build (tag, Drive upload, record) then runs in the background from
durable staging (``services/song_builds/``).

The body is ``multipart/form-data`` with no zod schema: fields are read and
checked by hand, in the order and with the messages docs/API.md lists.

A staff upload on behalf can also submit the song to the event it is for
(``submit_to_event``): the dancer is the submitter, the staff member the
uploader, and a taken slot (ENTITY_SLOT_TAKEN) is refused before anything is
saved. api-deejaytools only.
"""

from __future__ import annotations

import asyncio
import json
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
from ..clock import now_ms
from ..database import get_db_session
from ..domain import song_entity_key
from ..errors import (
    Envelope,
    ErrorCode,
    ErrorResponse,
    api_error,
    bad_request,
    success,
)
from ..models import Event, EventSongSubmission, Partner, Song, SongUpload, Team, User
from ..services import song_builds
from ..services.audio_format import detect_audio_format
from ..song_records import (
    assert_managed_partnership_owned,
    assert_partner_owned,
    map_song,
)
from ..submissions import (
    DEFAULT_ROUND,
    ROUND_SPLIT_DIVISION,
    SUBMISSION_ROUNDS,
    entity_slot_taken,
    fetch_user_submission_rows,
    is_open_event,
    lock_entity_slot,
    map_submission_row,
    slot_taken_message,
)
from ..zod_coerce import js_number, js_trim

logger = get_logger()

router = APIRouter(prefix="/v1/songs", tags=["songs"])

CHUNK_TMP_BASE = Path("/tmp/dj-upload-chunks")
CHUNK_DONE_BASE = Path("/tmp/dj-upload-done")
"""One file per finished upload, naming its song: what a resent chunk is
answered from. Swept after ``CHUNK_TTL_SECONDS``, as staging is."""
ASSEMBLING_SUFFIX = ".assembling"
"""An upload's staging directory is renamed to this while its final chunk is
assembled, so only one request assembles it."""
ASSEMBLY_WAIT_SECONDS = 30.0
"""How long a resent chunk waits for an assembly already under way."""
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


class ChunkResponse(Envelope[ChunkReceived]):
    """Chunk acknowledgement envelope."""

    data: ChunkReceived = Field(..., description="Acknowledgement.")


def _sweep_stale_dirs() -> None:
    """Remove upload directories and finished-upload records untouched for
    two hours. Never raises."""
    cutoff = time.time() - CHUNK_TTL_SECONDS
    for base in (CHUNK_TMP_BASE, CHUNK_DONE_BASE):
        try:
            entries = list(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.stat().st_mtime < cutoff:
                    if entry.is_dir():
                        shutil.rmtree(entry, ignore_errors=True)
                    else:
                        entry.unlink(missing_ok=True)
            except OSError:
                continue


def _remove(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)


def _read_done(marker: Path) -> dict[str, Any] | None:
    """The finished upload ``marker`` records, or None."""
    try:
        return json.loads(marker.read_text())
    except (OSError, ValueError):
        return None


def _write_done(marker: Path, done: dict[str, Any]) -> None:
    """Record a finished upload. Never raises: a resend then gets the
    ordinary answer for a chunk that was not staged."""
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(done))
    except OSError as exc:
        logger.error(with_log_prefix(LOG_FAILURE, f"upload_done_write_failed: {exc!r}"))


async def _wait_for_assembly(assembling: Path) -> bool:
    """Wait while another request assembles this upload; True if it still is."""
    deadline = time.monotonic() + ASSEMBLY_WAIT_SECONDS
    while await asyncio.to_thread(assembling.exists):
        if time.monotonic() >= deadline:
            return True
        await asyncio.sleep(0.1)
    return False


async def _finished_answer(
    db: AsyncSession, done: dict[str, Any]
) -> dict[str, Any] | None:
    """The final chunk's answer again, for a finished upload; None when its
    song is gone (its build failed and removed it)."""
    song = await db.get(Song, done["song_id"])
    if song is None:
        return None
    answer: dict[str, Any] = {
        "received": True,
        "complete": True,
        "song": map_song(song),
    }
    if done.get("submission_id") is not None:
        rows = await fetch_user_submission_rows(
            db, done["user_id"], submission_id=done["submission_id"]
        )
        if rows:
            answer["submission"] = map_submission_row(rows[0])
    return answer


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
        "ENTITY_SLOT_TAKEN before anything is saved when the slot is taken; "
        "its optional round (default prelims_and_finals) splits only for "
        "Classic at The Open, as POST /v1/event-song-submissions does."
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
    """Stage one chunk; on the final one, create the song and start its build.

    Resending a chunk is safe: a staged chunk is overwritten, and any chunk
    of an upload already finished answers as it did the first time, without
    a second song.
    """
    claimed: list[Path] = []
    try:
        return await _upload_chunk(request, caller, db, claimed)
    finally:
        # The assembling directory goes once the final chunk is answered,
        # after the finished-upload record is written.
        for directory in claimed:
            await asyncio.to_thread(_remove, directory)


async def _upload_chunk(
    request: Request, caller: Caller, db: AsyncSession, claimed: list[Path]
) -> dict[str, Any]:
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
    # The rounds a submit_to_event entry is for; stored only when sent.
    round_sent = _trimmed_or_none(form, "round")
    chunk = form.get("chunk")
    chunk_file = chunk if isinstance(chunk, UploadFile) else None

    if not _UUID.fullmatch(upload_id):
        raise bad_request("Invalid upload_id")
    if not division:
        raise bad_request("division is required")
    if not _is_int(chunk_index) or chunk_index < 0:
        raise bad_request("Invalid chunk_index")
    if not _is_int(total_chunks) or not 1 <= total_chunks <= 30:
        raise bad_request("Invalid total_chunks (max 30)")
    if chunk_index >= total_chunks:
        raise bad_request("chunk_index out of range")
    if chunk_file is None:
        raise bad_request("Missing chunk field")
    if submit_to_event and not (on_behalf_of and event_id):
        raise bad_request("submit_to_event needs on_behalf_of_user_id and event_id")
    if round_sent is not None:
        if not submit_to_event:
            raise bad_request("round needs submit_to_event")
        if round_sent not in SUBMISSION_ROUNDS:
            raise bad_request("Invalid round")
    round_ = round_sent if round_sent is not None else DEFAULT_ROUND
    # Rounds are an Open-only affordance for Classic; The Open is checked
    # once the event is read, on the final chunk.
    if round_ != DEFAULT_ROUND and division != ROUND_SPLIT_DIVISION:
        raise bad_request(
            f"Round selection is only available for the {ROUND_SPLIT_DIVISION} division"
        )
    # The event the song is submitted to, or None when it is not submitted.
    submit_event_id = event_id if submit_to_event else None
    if on_behalf_of:
        # A second, audited decision (ADR-0008), on event_id's event when
        # one is sent (ADR-0012). On every chunk.
        await authorize_delegation(caller, on_behalf_of, db, request, event_id=event_id)

    chunk_bytes = await chunk_file.read()
    if len(chunk_bytes) > MAX_CHUNK_BYTES:
        raise bad_request("Chunk exceeds 10 MB limit")

    # Keyed by the caller, even when uploading for someone else.
    upload_dir = CHUNK_TMP_BASE / f"{caller.user_id}_{upload_id}"
    index, total = int(chunk_index), int(total_chunks)
    # Every chunk but the last is the same size, so a non-final chunk says
    # how big the file will at least be: one that is already over the limit
    # is refused before anything of it is staged.
    if index != total - 1 and len(chunk_bytes) * (total - 1) + 1 > MAX_ASSEMBLED_BYTES:
        _remove(upload_dir)
        raise bad_request("File exceeds 100 MB limit")

    # A resent chunk of an upload whose final chunk is being assembled waits
    # for it; of a finished one, it is answered as before, and not staged.
    assembling = upload_dir.with_name(upload_dir.name + ASSEMBLING_SUFFIX)
    done_marker = CHUNK_DONE_BASE / upload_dir.name
    if await _wait_for_assembly(assembling):
        raise api_error(
            409,
            "UPLOAD_IN_PROGRESS",
            "This upload's final chunk is still being processed. Please retry.",
        )
    done = await asyncio.to_thread(_read_done, done_marker)
    if done is not None:
        if index != total - 1:
            return success({"received": True, "complete": False})
        replay = await _finished_answer(db, done)
        if replay is not None:
            return success(replay)

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
        raise bad_request("File exceeds 100 MB limit")
    if index != total - 1:
        return success({"received": True, "complete": False})

    def refuse(message: str) -> Exception:
        _remove(upload_dir)
        return bad_request(message)

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
        event = await db.get(Event, submit_event_id)
        if event is None:
            _remove(upload_dir)
            raise api_error(404, ErrorCode.NOT_FOUND, "Event not found")
        if round_ != DEFAULT_ROUND and not is_open_event(event.name):
            raise refuse("Round selection is only available for The Open")
        slot_entity = song_entity_key(
            effective_user_id,
            None if managed_partnership_id else partner_id,
            managed_partnership_id,
        )
        # Held until the commit that inserts the submission.
        await lock_entity_slot(db, event_id=submit_event_id, entity=slot_entity)
        if await entity_slot_taken(
            db,
            event_id=submit_event_id,
            entity=slot_entity,
            division=division,
            round_=round_,
        ):
            _remove(upload_dir)
            raise api_error(409, "ENTITY_SLOT_TAKEN", slot_taken_message(division))

    # Claimed by renaming: of two requests with the final chunk, one assembles.
    try:
        await asyncio.to_thread(upload_dir.rename, assembling)
    except FileNotFoundError:
        if await _wait_for_assembly(assembling):
            raise api_error(
                409,
                "UPLOAD_IN_PROGRESS",
                "This upload's final chunk is still being processed. Please retry.",
            ) from None
        done = await asyncio.to_thread(_read_done, done_marker)
        replay = await _finished_answer(db, done) if done is not None else None
        if replay is not None:
            return success(replay)
        raise api_error(
            409,
            "CHUNK_MISSING",
            f"Expected {total} chunks but only received 0. Please retry the upload.",
        ) from None
    except OSError as exc:
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"chunk_claim_failed user={caller.user_id} upload={upload_id}: {exc!r}",
            )
        )
        raise api_error(500, "CHUNK_ERROR", "Failed to read uploaded chunks") from exc
    claimed.append(assembling)
    upload_dir = assembling
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
        return b"".join((upload_dir / name).read_bytes() for name in names)

    assembled = await asyncio.to_thread(assemble)
    if len(assembled) > MAX_ASSEMBLED_BYTES:
        raise bad_request("File exceeds 100 MB limit")
    mime_type = detect_audio_format(assembled)
    if mime_type is None:
        raise api_error(
            400,
            "UNSUPPORTED_FORMAT",
            "That file doesn't look like a supported audio format. "
            "Please upload an MP3, WAV, FLAC, or M4A.",
        )

    now = now_ms()
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
        # The song's own division (no override) and the round sent, if any.
        submission_id = str(uuid.uuid4())
        db.add(
            EventSongSubmission(
                id=submission_id,
                event_id=submit_event_id,
                song_id=song.id,
                submitted_by_user_id=effective_user_id,
                division=None,
                round=round_sent,
                created_at=now,
            )
        )
    await db.commit()
    await asyncio.to_thread(
        _write_done,
        done_marker,
        {
            "song_id": song.id,
            "submission_id": submission_id,
            "user_id": effective_user_id,
        },
    )

    song_builds.start_build(song.id)
    # Partner names are null in this answer whatever the partner.
    answer: dict[str, Any] = {
        "received": True,
        "complete": True,
        "song": map_song(song),
    }
    if submission_id is not None:
        # The build queues the copy when it records the file.
        rows = await fetch_user_submission_rows(
            db, effective_user_id, submission_id=submission_id
        )
        answer["submission"] = map_submission_row(rows[0])
    return success(answer)
