"""The scheduler's queue work (deejaytools-api src/services/cron.ts).

- ``tick_session_statuses`` advances the stored ``sessions.status`` along
  scheduled → checkin_open → in_progress → completed as the clock passes each
  boundary.
- ``fill_running_sessions`` auto-fills the active queue of every session
  inside its floor-trial window. Nothing else fills a session when its trial
  opens, so this is the trigger at start and the top-up on every tick.
"""

from __future__ import annotations

import time

from mini_app_polis.logger import LOG_FAILURE, LOG_SUCCESS, get_logger, with_log_prefix
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..cache import invalidate_queue_cache
from ..models import Session
from ..queue import fill_active_queue, lock_session_for_fill

logger = get_logger()

_ADVANCING = ("scheduled", "checkin_open", "in_progress")


def _now_ms() -> int:
    return int(time.time() * 1000)


async def tick_session_statuses(db: AsyncSession) -> int:
    """Advance stored session statuses one step each; returns how many changed.

    Commits its own work.
    """
    now = _now_ms()
    rows = (
        await db.execute(
            select(
                Session.id,
                Session.status,
                Session.checkin_opens_at,
                Session.floor_trial_starts_at,
                Session.floor_trial_ends_at,
            ).where(Session.status.in_(_ADVANCING))
        )
    ).all()

    updated = 0
    for s in rows:
        new_status: str | None = None
        if s.status == "scheduled" and now >= s.checkin_opens_at:
            new_status = "checkin_open"
        elif s.status == "checkin_open" and now >= s.floor_trial_starts_at:
            new_status = "in_progress"
        elif s.status == "in_progress" and now >= s.floor_trial_ends_at:
            new_status = "completed"
        if new_status:
            await db.execute(
                update(Session)
                .where(Session.id == s.id)
                .values(status=new_status)
                .execution_options(synchronize_session=False)
            )
            # Each update commits on its own, as deejaytools-api's autocommit
            # did: a later failure keeps the earlier advances, and no session
            # row stays locked while the rest of the pass runs.
            await db.commit()
            updated += 1
            logger.info(
                with_log_prefix(
                    LOG_SUCCESS,
                    f"session_status_updated session_id={s.id} status={new_status}",
                )
            )
    await db.commit()  # ends the read's transaction when nothing changed
    # Logged on every pass so operators can see the scheduler is alive even
    # when nothing changes.
    logger.info(
        with_log_prefix(
            LOG_SUCCESS,
            f"tick_completed sessions_checked={len(rows)} sessions_updated={updated}",
        )
    )
    return updated


async def fill_running_sessions(db: AsyncSession) -> int:
    """Auto-fill every session inside its floor-trial window; returns how many
    entries were promoted in all.

    One transaction per session: lock, fill, commit. A failure is rolled
    back and logged, and the next session still runs. A session that
    promoted anything has its queue cache invalidated.
    """
    now = _now_ms()
    session_ids = list(
        (
            await db.execute(
                select(Session.id).where(
                    Session.status != "cancelled",
                    Session.floor_trial_starts_at <= now,
                    Session.floor_trial_ends_at > now,
                )
            )
        ).scalars()
    )
    # End the read so each session's transaction starts with its lock.
    await db.commit()

    total = 0
    for session_id in session_ids:
        try:
            locked = await lock_session_for_fill(db, session_id)
            promoted = await fill_active_queue(db, locked, None, now) if locked else 0
            await db.commit()
        except Exception as exc:  # noqa: BLE001 - one session's failure must not stop the rest
            await db.rollback()
            logger.error(
                with_log_prefix(
                    LOG_FAILURE, f"auto_fill_failed session_id={session_id}: {exc!r}"
                )
            )
            continue
        if promoted > 0:
            total += promoted
            invalidate_queue_cache(session_id)

    logger.info(
        with_log_prefix(
            LOG_SUCCESS,
            f"auto_fill_completed sessions_checked={len(session_ids)} "
            f"entries_promoted={total}",
        )
    )
    return total
