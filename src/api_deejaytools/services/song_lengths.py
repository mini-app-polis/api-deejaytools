"""Each song's length, read from its file in Drive.

Staff see it next to each submission, to check a routine's music against its
division's time limit. A song's build reads it from the uploaded bytes
(``read_duration_ms``, called from ``song_builds``). The scheduler measures
the rest a few a tick (``measure_song_lengths``): every song with a Drive file
and no length yet, those never tried first. That is the songs uploaded before
lengths were kept, as a backlog, and the few builds that never read their
bytes (one that found its file already in Drive) or could not read a length
from them. A file that cannot be read (Drive refuses it, or mutagen cannot
parse it) is tried again a day later, behind every song not tried yet.

The length comes from the file's own headers (mutagen), not from decoding
it: the same formats the upload accepts (``audio_format``).
"""

from __future__ import annotations

import asyncio
import io

from mini_app_polis.logger import LOG_SUCCESS, LOG_WARNING, get_logger, with_log_prefix
from mutagen import File as MutagenFile
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..clock import now_ms
from ..models import Song
from . import drive

logger = get_logger()

LENGTH_BATCH = 2
"""The most songs one tick measures. Each is a whole-file download (up to
~110 MiB for a WAV) inside the tick, so a large batch would hold up queue
fills and Drive jobs."""

RETRY_AFTER_MS = 24 * 60 * 60 * 1000
"""How long a song whose file could not be read waits before another try."""


def read_duration_ms(data: bytes) -> int | None:
    """How long ``data`` plays, in whole milliseconds, or None when its
    format is not recognised or its headers give no length."""
    try:
        audio = MutagenFile(io.BytesIO(data))
    except Exception:  # noqa: BLE001 - any parse failure means "unknown"
        return None
    length = getattr(getattr(audio, "info", None), "length", None)
    if not isinstance(length, int | float) or length <= 0:
        return None
    return round(length * 1000)


async def measure_song_lengths(db: AsyncSession, *, limit: int = LENGTH_BATCH) -> int:
    """Measure up to ``limit`` songs still without a length; how many were
    measured. Never raises for one song's file: that song is stamped as
    tried and waits ``RETRY_AFTER_MS``."""
    retry_before = now_ms() - RETRY_AFTER_MS
    songs = (
        await db.execute(
            select(Song.id, Song.drive_file_id)
            .where(
                Song.drive_file_id.is_not(None),
                Song.deleted_at.is_(None),
                Song.duration_ms.is_(None),
                or_(
                    Song.duration_checked_at.is_(None),
                    Song.duration_checked_at < retry_before,
                ),
            )
            .order_by(Song.duration_checked_at.asc().nulls_first(), Song.created_at)
            .limit(limit)
        )
    ).all()
    measured = 0
    for song_id, file_id in songs:
        duration: int | None = None
        try:
            data = await drive.download_file(str(file_id))
            duration = await asyncio.to_thread(read_duration_ms, data)
            del data
        except Exception as exc:  # noqa: BLE001 - one file never stops the batch
            logger.warning(
                with_log_prefix(
                    LOG_WARNING, f"song_length_download_failed song={song_id}: {exc!r}"
                )
            )
        else:
            if duration is None:
                logger.warning(
                    with_log_prefix(
                        LOG_WARNING, f"song_length_unreadable song={song_id}"
                    )
                )
        await db.execute(
            update(Song)
            .where(Song.id == song_id)
            .values(duration_ms=duration, duration_checked_at=now_ms())
        )
        await db.commit()
        if duration is not None:
            measured += 1
            logger.info(
                with_log_prefix(
                    LOG_SUCCESS, f"song_length_read song={song_id} ms={duration}"
                )
            )
    return measured
