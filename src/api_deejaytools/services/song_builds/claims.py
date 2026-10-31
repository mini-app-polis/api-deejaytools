"""A build's claim on its ``song_uploads`` row: taking it, renewing its
lease while the build runs, and writing only while the claim is still held.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from mini_app_polis.logger import LOG_WARNING, get_logger, with_log_prefix
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...clock import now_ms
from ...models import SongUpload

logger = get_logger()

LEASE_MS = 10 * 60_000
"""A build that has not renewed its lease for this long is presumed dead and
taken over."""

HEARTBEAT_S = 60.0
"""How often a running build renews its lease."""


class LostClaim(Exception):
    """Another build took this one over; stop without touching anything."""


async def _claim(db: AsyncSession, song_id: str) -> str | None:
    """Take the build, or None if it is not due or another build holds it."""
    now = now_ms()
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
                    .values(updated_at=now_ms())
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
            .values(**values, updated_at=now_ms())
            .returning(SongUpload.song_id)
        )
    ).first()
    if updated is None:
        await db.rollback()
        raise LostClaim(song_id)
