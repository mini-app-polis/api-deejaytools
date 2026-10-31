"""Starting builds in the background: right after an upload, and on each
scheduler tick for builds that are due or whose lease ran out, at most
``MAX_IN_FLIGHT`` at once in this process.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...clock import now_ms
from ...models import SongUpload
from .claims import LEASE_MS
from .pipeline import build_song

SWEEP_LIMIT = 5
"""Builds the scheduler starts per tick."""

MAX_IN_FLIGHT = 8
"""Builds the scheduler lets run at once in this process (each holds its
file in memory)."""

_in_flight: set[asyncio.Task[bool]] = set()


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
    now = now_ms()
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
