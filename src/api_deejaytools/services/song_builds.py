"""Building an uploaded song: name it, tag it, upload it to Drive, record it.

deejaytools-api DRIVE.md "Background build" is the behaviour. The steps,
the filename rule, the tag contents, the sharing and the re-queued event
copies are the same. What is different is that the build is durable, which
fixes the four known defects DRIVE.md lists for it:

- **A restart mid-build** no longer leaves a song with no file and nothing
  to sweep it. The bytes wait in ``song_uploads`` (migration 003), and the
  scheduler resumes any build that is due or whose lease ran out.
- **A failed build of a song already submitted or checked in** is retried
  with backoff instead of leaving the song without a file. Its row cannot
  be deleted (foreign keys), and now it does not need to be.
- **A failure after the Drive upload** no longer orphans the file. The file
  id is recorded as soon as the upload returns, a retry reuses it, a file
  found by its ``deejaytools_song_id`` appProperty is reused after a crash in
  between, and a song deleted meanwhile has its file deprecated.
- **Two uploads in flight for the same slot** no longer get the same
  ``vNN``. The filename is reserved under a per-slot advisory lock, counting
  the filenames other builds have reserved as well as finished songs.

A song nothing references is still deleted when its build fails, as in
deejaytools-api: the uploader sees it vanish and uploads again. The web app
and the conformance suite rely on that.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
import uuid
from datetime import datetime
from typing import Any

import sentry_sdk
from mini_app_polis.logger import LOG_FAILURE, LOG_WARNING, get_logger, with_log_prefix
from sqlalchemy import delete, exists, select, text, union_all, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..database import get_sessionmaker
from ..models import (
    EventSongSubmission,
    ManagedPartnership,
    Partner,
    Song,
    SongUpload,
    User,
)
from ..submissions import is_follower_am_division
from ..zod_coerce import js_trim, js_words
from . import drive
from .drive_jobs import MAX_ATTEMPTS, backoff_ms, enqueue_drive_job
from .tagging import tag_song_bytes

logger = get_logger()

LEASE_MS = 10 * 60_000
"""A build that has not renewed its lease for this long is presumed dead and
taken over."""

HEARTBEAT_S = 60.0
"""How often a running build renews its lease."""

SWEEP_LIMIT = 5
"""Builds the scheduler starts per tick."""

MAX_IN_FLIGHT = 8
"""Builds the scheduler lets run at once in this process (each holds its
file in memory)."""

SONG_APP_PROPERTY = "deejaytools_song_id"
"""Drive appProperty naming the song a file was uploaded for."""

_VERSION_RE = re.compile(r"_v(\d+)(?:\.[^.]*)?$")
_NON_ALNUM = re.compile(r"[^a-zA-Z0-9]")
_SEASON_ROLLOVER_MONTH = 10

_in_flight: set[asyncio.Task[bool]] = set()


class LostClaim(Exception):
    """Another build took this one over; stop without touching anything."""


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Naming (DRIVE.md "Background build", steps 2-5)
# ---------------------------------------------------------------------------


def season_year_now() -> str:
    """The upload-time season, in the server's local timezone as Node took it."""
    now = datetime.now()
    return str(now.year + 1 if now.month >= _SEASON_ROLLOVER_MONTH else now.year)


def sanitize_segment(value: str | None) -> str:
    """Each whitespace-separated word stripped to ASCII alphanumerics,
    lowercased, first letter capitalised, joined: ``mary-jo o'neil`` →
    ``MaryjoOneil``."""
    if not value:
        return ""
    words = []
    for word in js_words(value):
        clean = _NON_ALNUM.sub("", word)
        if clean:
            lower = clean.lower()
            words.append(lower[0].upper() + lower[1:])
    return "".join(words)


def static_segment(value: str | None) -> str:
    """ASCII alphanumerics only, case kept: ``ProAm LeaderAm`` → ``ProAmLeaderAm``."""
    return _NON_ALNUM.sub("", value) if value else ""


def file_extension(filename: str) -> str:
    """The original file's extension: after the last dot unless that dot is
    first or last, alphanumerics only, lowercased."""
    trimmed = js_trim(filename)
    dot = trimmed.rfind(".")
    if dot <= 0 or dot == len(trimmed) - 1:
        return ""
    return _NON_ALNUM.sub("", trimmed[dot + 1 :]).lower()


def _name(*parts: str | None) -> str:
    return js_trim(" ".join(p for p in parts if p))


def entity_names(
    *,
    user: User,
    partner: Partner | None,
    managed: ManagedPartnership | None,
    division: str | None,
) -> tuple[str, str | None]:
    """(first, second) for the filename and title, after the ProAm swap."""
    user_name = _name(user.first_name, user.last_name) or user.id
    leader: str
    follower: str | None
    if managed is not None:
        leader = _name(managed.leader_first_name, managed.leader_last_name)
        follower = _name(managed.follower_first_name, managed.follower_last_name)
    elif partner is None:
        leader, follower = user_name, None
    elif partner.kind and partner.kind != "partner":
        leader, follower = _name(partner.first_name, partner.last_name), None
    elif partner.partner_role == "leader":
        leader, follower = _name(partner.first_name, partner.last_name), user_name
    else:
        leader, follower = user_name, _name(partner.first_name, partner.last_name)
    if follower is not None and is_follower_am_division(division):
        return follower, leader
    return leader, follower


def processed_stem(
    *,
    first: str,
    second: str | None,
    user_id: str,
    division: str | None,
    season_year: str,
    routine_name: str | None,
    personal_descriptor: str | None,
) -> str:
    """The filename without its version and extension."""
    partnership = (
        f"{sanitize_segment(first)}_{sanitize_segment(second)}"
        if second
        else sanitize_segment(first)
    )
    segments = [
        partnership or sanitize_segment(user_id) or "user",
        static_segment(division),
        sanitize_segment(season_year),
        sanitize_segment(routine_name),
        sanitize_segment(personal_descriptor),
    ]
    return "_".join(s for s in segments if s)


def versioned_filename(stem: str, version: int, extension: str) -> str:
    """``<stem>_vNN[.<ext>]``, at least two digits."""
    name = f"{stem}_v{version:02d}"
    return f"{name}.{extension}" if extension else name


# ---------------------------------------------------------------------------
# Starting builds
# ---------------------------------------------------------------------------


def start_build(song_id: str) -> None:
    """Run a build in the background, after the upload's response is sent.

    The scheduler resumes it if this process dies first.
    """
    task = asyncio.ensure_future(build_song(song_id))
    _in_flight.add(task)
    task.add_done_callback(_in_flight.discard)


async def wait_for_builds() -> None:
    """Wait for every background build this process started (for tests)."""
    while _in_flight:
        await asyncio.gather(*list(_in_flight), return_exceptions=True)


async def process_song_uploads(db: AsyncSession) -> int:
    """Start builds that are due or whose lease ran out, in the background.
    Returns how many were started. Called by the scheduler each tick.

    Not awaited: a build can take minutes (a large file, a slow Drive), and
    the tick (and ``/internal/tick``) must not wait on it. At most
    ``MAX_IN_FLIGHT`` builds run in this process at once, uploads included;
    the rest wait for a later tick.
    """
    room = MAX_IN_FLIGHT - len(_in_flight)
    if room <= 0:
        return 0
    now = _now_ms()
    ids = list(
        (
            await db.execute(
                select(SongUpload.song_id)
                .where(
                    (
                        (SongUpload.status == "pending")
                        & (SongUpload.next_attempt_at <= now)
                    )
                    | (
                        (SongUpload.status == "running")
                        & (SongUpload.updated_at < now - LEASE_MS)
                    )
                )
                .order_by(SongUpload.next_attempt_at)
                .limit(min(room, SWEEP_LIMIT))
            )
        ).scalars()
    )
    await db.rollback()
    for song_id in ids:
        start_build(song_id)
    return len(ids)


# ---------------------------------------------------------------------------
# The build
# ---------------------------------------------------------------------------


async def build_song(song_id: str) -> bool:
    """Claim and run one build. True when the song now has its file.

    Never raises: a failure deletes an unreferenced song (and deprecates any
    file already uploaded) or schedules a retry.
    """
    maker = get_sessionmaker()
    async with maker() as db:
        claim = await _claim(db, song_id)
    if claim is None:
        return False
    heartbeat = asyncio.ensure_future(_heartbeat(maker, song_id, claim))
    try:
        return await _build(maker, song_id, claim)
    except LostClaim:
        logger.warning(
            with_log_prefix(LOG_WARNING, f"song_build_claim_lost song={song_id}")
        )
        return False
    except Exception as exc:  # noqa: BLE001 - every failure is handled below
        await _on_failure(maker, song_id, claim, exc)
        return False
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat


async def _claim(db: AsyncSession, song_id: str) -> str | None:
    """Take the build, or None if it is not due or another build holds it."""
    now = _now_ms()
    claim = str(uuid.uuid4())
    claimed = (
        await db.execute(
            update(SongUpload)
            .where(
                SongUpload.song_id == song_id,
                ((SongUpload.status == "pending") & (SongUpload.next_attempt_at <= now))
                | (
                    (SongUpload.status == "running")
                    & (SongUpload.updated_at < now - LEASE_MS)
                ),
            )
            .values(status="running", claim_id=claim, updated_at=now)
            .returning(SongUpload.song_id)
        )
    ).first()
    await db.commit()
    return claim if claimed is not None else None


def _mine(song_id: str, claim: str) -> list[Any]:
    """WHERE clause matching the upload row while this build still owns it."""
    return [
        SongUpload.song_id == song_id,
        SongUpload.status == "running",
        SongUpload.claim_id == claim,
    ]


async def _heartbeat(
    maker: async_sessionmaker[AsyncSession], song_id: str, claim: str
) -> None:
    """Renew the lease while the build runs, so a slow build (a large file,
    a slow Drive) is not taken over and run twice."""
    while True:
        await asyncio.sleep(HEARTBEAT_S)
        try:
            async with maker() as db:
                await db.execute(
                    update(SongUpload)
                    .where(*_mine(song_id, claim))
                    .values(updated_at=_now_ms())
                )
                await db.commit()
        except Exception as exc:  # noqa: BLE001 - the next beat tries again
            logger.warning(
                with_log_prefix(
                    LOG_WARNING, f"song_build_heartbeat_failed song={song_id}: {exc!r}"
                )
            )


async def _update_mine(
    db: AsyncSession, song_id: str, claim: str, **values: Any
) -> None:
    """Update the upload row this build owns, or raise ``LostClaim``."""
    updated = (
        await db.execute(
            update(SongUpload)
            .where(*_mine(song_id, claim))
            .values(**values, updated_at=_now_ms())
            .returning(SongUpload.song_id)
        )
    ).first()
    if updated is None:
        await db.rollback()
        raise LostClaim(song_id)


async def _load_people(
    db: AsyncSession, song: Song
) -> tuple[User, Partner | None, ManagedPartnership | None]:
    user = await db.get(User, song.user_id)
    if user is None:
        raise RuntimeError("User not found")
    if song.managed_partnership_id:
        managed = (
            await db.execute(
                select(ManagedPartnership).where(
                    ManagedPartnership.id == song.managed_partnership_id,
                    ManagedPartnership.user_id == song.user_id,
                )
            )
        ).scalar_one_or_none()
        if managed is None:
            raise RuntimeError("Managed partnership not found")
        return user, None, managed
    partner = None
    if song.partner_id:
        partner = (
            await db.execute(
                select(Partner).where(
                    Partner.id == song.partner_id, Partner.user_id == song.user_id
                )
            )
        ).scalar_one_or_none()
    return user, partner, None


async def _reserve_filename(
    maker: async_sessionmaker[AsyncSession],
    song: Song,
    claim: str,
    stem: str,
    extension: str,
    season: str,
) -> str:
    """Pick the next version for the song's slot and record it on the upload.

    Under a transaction-scoped advisory lock on the slot, so two builds for
    the same slot never pick the same number. Counts finished songs of the
    slot (soft-deleted included, so a deleted version's number is not handed
    out again) and filenames other unfinished builds have reserved, in one
    statement: one snapshot, so a build finishing in between (its name moving
    from ``song_uploads`` to ``songs``) is seen in exactly one of the two.
    """
    slot = "|".join(
        [
            song.user_id,
            song.division or "",
            song.routine_name or "",
            season,
            song.partner_id or "",
            song.managed_partnership_id or "",
        ]
    )
    same_slot = (
        (Song.user_id == song.user_id)
        & (text("coalesce(songs.division, '') = :division"))
        & (text("coalesce(songs.routine_name, '') = :routine"))
        & (text("coalesce(songs.partner_id, '') = :partner"))
        & (text("coalesce(songs.managed_partnership_id, '') = :managed"))
        & (Song.id != song.id)
    )
    params = {
        "division": song.division or "",
        "routine": song.routine_name or "",
        "partner": song.partner_id or "",
        "managed": song.managed_partnership_id or "",
    }
    async with maker() as db:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:slot))"), {"slot": slot}
        )
        names = (
            await db.execute(
                union_all(
                    select(Song.processed_filename.label("name")).where(
                        same_slot, Song.season_year == season
                    ),
                    select(SongUpload.processed_filename.label("name"))
                    .join(Song, Song.id == SongUpload.song_id)
                    .where(same_slot, SongUpload.season_year == season),
                ),
                params,
            )
        ).scalars()
        highest = 0
        for name in names:
            match = _VERSION_RE.search(name or "")
            if match:
                highest = max(highest, int(match.group(1)))
        filename = versioned_filename(stem, highest + 1, extension)
        await _update_mine(
            db, song.id, claim, season_year=season, processed_filename=filename
        )
        await db.commit()
    return filename


async def _build(
    maker: async_sessionmaker[AsyncSession], song_id: str, claim: str
) -> bool:
    async with maker() as db:
        song = await db.get(Song, song_id)
        upload = await db.get(SongUpload, song_id)
        if song is None or upload is None:
            return False  # deleted meanwhile; the cascade took the upload with it
        user, partner, managed = await _load_people(db, song)
        season = upload.season_year or season_year_now()
        processed_filename = upload.processed_filename
        drive_file_id, drive_folder_id = upload.drive_file_id, upload.drive_folder_id
        mime_type, original_filename = upload.mime_type, upload.original_filename

    first, second = entity_names(
        user=user, partner=partner, managed=managed, division=song.division
    )
    if processed_filename is None:
        stem = processed_stem(
            first=first,
            second=second,
            user_id=user.id,
            division=song.division,
            season_year=season,
            routine_name=song.routine_name,
            personal_descriptor=song.personal_descriptor,
        )
        processed_filename = await _reserve_filename(
            maker, song, claim, stem, file_extension(original_filename), season
        )

    if drive_file_id is None or drive_folder_id is None:
        found = await drive.find_song_file(
            key=SONG_APP_PROPERTY,
            value=song.id,
            season_year=season,
            division=song.division or "",
        )
        if found is None:
            async with maker() as db:
                data = (
                    await db.execute(
                        select(SongUpload.data).where(SongUpload.song_id == song.id)
                    )
                ).scalar_one()
            tagged = await asyncio.to_thread(
                tag_song_bytes,
                data,
                title=f"{first} & {second}" if second else first,
                artist=" | ".join(p for p in (song.division, song.routine_name) if p),
                year=season,
                mime_type=mime_type,
            )
            found = await drive.upload_song_file(
                tagged,
                filename=processed_filename,
                mime_type=mime_type,
                season_year=season,
                division=song.division or "",
                app_properties={SONG_APP_PROPERTY: song.id},
            )
        drive_file_id, drive_folder_id = found
        async with maker() as db:
            await _update_mine(
                db,
                song.id,
                claim,
                drive_file_id=drive_file_id,
                drive_folder_id=drive_folder_id,
            )
            await db.commit()

    await _share(song, user, partner, drive_file_id)
    return await _finish(
        maker,
        song.id,
        claim,
        original_filename=original_filename,
        processed_filename=processed_filename,
        season=season,
        drive_file_id=drive_file_id,
        drive_folder_id=drive_folder_id,
    )


async def _share(song: Song, user: User, partner: Partner | None, file_id: str) -> None:
    """Reader access for the uploader and, for a partner song, the partner's
    email. Best-effort, as in deejaytools-api."""
    targets: list[str | None] = [user.email]
    if partner is not None:
        targets.append(partner.email)
    try:
        result = await drive.share_song_file(file_id, targets)
    except Exception as exc:  # noqa: BLE001 - sharing never fails a build
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"song_drive_share_failed song={song.id} file={file_id}: {exc!r}",
            )
        )
        return
    if result.failed:
        failed = [(email, repr(err)) for email, err in result.failed]
        logger.warning(
            with_log_prefix(
                LOG_WARNING,
                f"song_drive_share_partial_failure song={song.id} file={file_id} "
                f"shared={len(result.shared)} failed={failed}",
            )
        )


async def _finish(
    maker: async_sessionmaker[AsyncSession],
    song_id: str,
    claim: str,
    *,
    original_filename: str,
    processed_filename: str,
    season: str,
    drive_file_id: str,
    drive_folder_id: str,
) -> bool:
    """Record the file on the song and drop the staged bytes, in one transaction."""
    async with maker() as db:
        song = (
            await db.execute(select(Song).where(Song.id == song_id).with_for_update())
        ).scalar_one_or_none()
        if song is None:
            await db.rollback()
            # Hard-deleted mid-build: nothing will ever reference the file.
            await _discard_file(maker, drive_file_id, song_id)
            return False
        song.original_filename = original_filename
        song.processed_filename = processed_filename
        song.season_year = season
        song.drive_file_id = drive_file_id
        song.drive_folder_id = drive_folder_id
        song.updated_at = _now_ms()
        deleted_meanwhile = song.deleted_at is not None
        dropped = (
            await db.execute(
                delete(SongUpload)
                .where(*_mine(song_id, claim))
                .returning(SongUpload.song_id)
            )
        ).first()
        if dropped is None:
            await db.rollback()
            raise LostClaim(song_id)
        await db.commit()

    if deleted_meanwhile:
        # Deleted by its owner while building: its delete saw no file to
        # deprecate, so deprecate it now.
        await _discard_file(maker, drive_file_id, song_id)
    else:
        await _requeue_event_copies(maker, song_id)
    return True


async def _requeue_event_copies(
    maker: async_sessionmaker[AsyncSession], song_id: str
) -> None:
    """A copy job for each submission of the song still without a copy. A
    submission made mid-build may have had its copy job find no file."""
    submission_ids: list[str] = []
    try:
        async with maker() as db:
            submission_ids = list(
                (
                    await db.execute(
                        select(EventSongSubmission.id).where(
                            EventSongSubmission.song_id == song_id,
                            EventSongSubmission.drive_copy_file_id.is_(None),
                        )
                    )
                ).scalars()
            )
            for submission_id in submission_ids:
                await enqueue_drive_job(db, "copy", submission_id=submission_id)
    except Exception as exc:  # noqa: BLE001 - best-effort, reported
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"drive_copy_requeue_failed song={song_id} submissions={submission_ids}: {exc!r}",
            )
        )
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("subsystem", "drive_jobs")
            scope.set_tag("drive_job_kind", "copy")
            scope.set_context(
                "drive_job",
                {
                    "song_id": song_id,
                    "submission_ids": submission_ids,
                    "stage": "requeue",
                },
            )
            sentry_sdk.capture_exception(exc)


async def _discard_file(
    maker: async_sessionmaker[AsyncSession], file_id: str, song_id: str
) -> None:
    """Deprecate an uploaded file nothing will reference; queue it if Drive says no."""
    try:
        await drive.soft_delete(file_id)
    except Exception as exc:  # noqa: BLE001 - the queue retries it
        logger.warning(
            with_log_prefix(
                LOG_WARNING,
                f"song_orphan_file_trash_queued song={song_id} file={file_id}: {exc!r}",
            )
        )
        async with maker() as db:
            await enqueue_drive_job(db, "trash", file_id=file_id)


async def _on_failure(
    maker: async_sessionmaker[AsyncSession], song_id: str, claim: str, exc: Exception
) -> None:
    """Delete an unreferenced song, as deejaytools-api does; otherwise retry."""
    logger.error(
        with_log_prefix(
            LOG_FAILURE, f"song_background_upload_failed song={song_id}: {exc!r}"
        )
    )
    try:
        async with maker() as db:
            owned = (
                await db.execute(
                    select(SongUpload.drive_file_id).where(*_mine(song_id, claim))
                )
            ).first()
            if owned is None:
                return  # taken over, or the song is gone already
            uploaded = owned[0]
            await db.execute(
                delete(Song).where(
                    Song.id == song_id,
                    exists().where(*_mine(song_id, claim)),
                )
            )
            await db.commit()
        if uploaded:
            await _discard_file(maker, uploaded, song_id)
        return
    except IntegrityError:
        # Submitted or checked in already: the song must stay, so its build
        # is retried until it has a file.
        logger.warning(
            with_log_prefix(
                LOG_WARNING,
                f"song_cleanup_delete_failed song={song_id}: build will retry",
            )
        )
    except Exception as delete_exc:  # noqa: BLE001 - fall through to a retry
        logger.warning(
            with_log_prefix(
                LOG_WARNING,
                f"song_cleanup_delete_failed song={song_id}: {delete_exc!r}",
            )
        )
    await _schedule_retry(maker, song_id, claim, exc)


async def _schedule_retry(
    maker: async_sessionmaker[AsyncSession], song_id: str, claim: str, exc: Exception
) -> None:
    try:
        async with maker() as db:
            row = (
                await db.execute(
                    select(SongUpload.attempts).where(*_mine(song_id, claim))
                )
            ).first()
            if row is None:
                return
            attempts = row[0] + 1
            now = _now_ms()
            exhausted = attempts >= MAX_ATTEMPTS
            await db.execute(
                update(SongUpload)
                .where(*_mine(song_id, claim))
                .values(
                    status="failed" if exhausted else "pending",
                    claim_id=None,
                    attempts=attempts,
                    last_error=str(exc)[:2000],
                    next_attempt_at=now + backoff_ms(attempts),
                    updated_at=now,
                )
            )
            await db.commit()
    except Exception as update_exc:  # noqa: BLE001 - the lease reclaims it
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"song_build_retry_update_failed song={song_id}: {update_exc!r}",
            )
        )
        return
    if exhausted:
        logger.error(
            with_log_prefix(
                LOG_FAILURE, f"song_build_exhausted song={song_id}: {exc!r}"
            )
        )
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("subsystem", "song_builds")
            scope.set_context("song_build", {"song_id": song_id, "attempts": attempts})
            sentry_sdk.capture_exception(exc)
    else:
        logger.warning(
            with_log_prefix(
                LOG_WARNING, f"song_build_retrying song={song_id} attempts={attempts}"
            )
        )
