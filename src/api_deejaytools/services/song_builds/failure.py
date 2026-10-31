"""A failed build: an unreferenced song is deleted (and any file already
uploaded deprecated); a song already submitted or checked in is kept and
its build retried with backoff until it has a file, or gives up.
"""

from __future__ import annotations

import sentry_sdk
from mini_app_polis.logger import LOG_FAILURE, LOG_WARNING, get_logger, with_log_prefix
from sqlalchemy import delete, exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...clock import now_ms
from ...models import Song, SongUpload
from .. import notifications
from ..drive_jobs import MAX_ATTEMPTS, backoff_ms
from .claims import _mine
from .completion import _discard_file

logger = get_logger()


async def _on_failure(
    maker: async_sessionmaker[AsyncSession], song_id: str, claim: str, exc: Exception
) -> None:
    """Delete an unreferenced song; otherwise retry."""
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
            removed = (
                await db.execute(
                    delete(Song)
                    .where(
                        Song.id == song_id,
                        exists().where(*_mine(song_id, claim)),
                    )
                    .returning(Song.id)
                )
            ).first()
            await db.commit()
        if removed is None:
            # The claim was lost between the read and the delete: the run
            # that took it over owns the song, its file and its report.
            return
        # Final for this song: the uploader sees it vanish and must upload
        # again — the failure a person notices — so it is a Sentry event, as
        # giving up after retries is, and the Discord report carries its id.
        # A song kept for a retry (below) is reported only when it gives up.
        with sentry_sdk.new_scope() as scope:
            scope.set_level("error")
            scope.set_tag("subsystem", "song_builds")
            scope.set_context("song_build", {"song_id": song_id, "removed": True})
            event_id = sentry_sdk.capture_exception(exc)
        notifications.report_fault(
            f"song build {song_id} · failed, song removed", exc, event_id=event_id
        )
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
            now = now_ms()
            exhausted = attempts >= MAX_ATTEMPTS
            recorded = (
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
                    .returning(SongUpload.song_id)
                )
            ).first()
            await db.commit()
    except Exception as update_exc:  # noqa: BLE001 - the lease reclaims it
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"song_build_retry_update_failed song={song_id}: {update_exc!r}",
            )
        )
        return
    if recorded is None:
        # The claim was lost after the read: the run that took it over
        # records (and, at the last attempt, reports) its own outcome.
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
            event_id = sentry_sdk.capture_exception(exc)
        notifications.report_fault(
            f"song build {song_id} · gave up after {attempts} attempts",
            exc,
            event_id=event_id,
        )
    else:
        logger.warning(
            with_log_prefix(
                LOG_WARNING, f"song_build_retrying song={song_id} attempts={attempts}"
            )
        )
