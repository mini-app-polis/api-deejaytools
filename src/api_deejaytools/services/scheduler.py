"""The in-process scheduler (deejaytools-api src/services/scheduler.ts, index.ts).

One pass (``run_tick``) advances session statuses, auto-fills the active queue
of every running floor trial, then drains one batch of the Drive job queue.
The background loop runs a pass at startup and then every ``TICK_INTERVAL_MS``
(default 30 s); ``GET /internal/tick`` runs the same pass on demand.

**When the loop runs.** Never with ``DISABLE_SCHEDULER=1`` (read as
deejaytools-api reads it: only ``"1"``), and never when ``ENVIRONMENT`` is
``test``, so no test, and no suite that builds the app, ever starts a
background loop; tests drive ``run_tick`` directly. Deploy with the flag set
and turn the loop on only at cutover, after deejaytools-api's scheduler is
stopped: two schedulers would both advance sessions and drain the Drive
queue (ADR-006 point 5).

Several replicas are safe: session status updates are idempotent, the queue
fill locks each session row, and Drive jobs are claimed with SKIP LOCKED.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

import sentry_sdk
from mini_app_polis.logger import (
    LOG_FAILURE,
    LOG_START,
    LOG_WARNING,
    get_logger,
    with_log_prefix,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings, get_settings
from ..database import get_sessionmaker
from . import drive, drive_jobs, session_tick, song_builds

logger = get_logger()

SHUTDOWN_GRACE_SECONDS = 5.0
"""How long shutdown waits for a pass in flight before cancelling it. A Drive
job cut off here stays ``running`` until its lease is reclaimed."""


_drive_skip_logged = False


def _session_factory() -> async_sessionmaker[AsyncSession]:
    """The app's sessionmaker, the one request handlers use."""
    return get_sessionmaker()


async def run_tick() -> None:
    """One scheduler pass: session work, song builds, then Drive jobs. Never raises.

    The parts fail independently, in separate try blocks: a persistent
    session-side failure must not stop the Drive queue draining, nor the
    reverse. Each step gets its own fresh session. Without Drive
    credentials the Drive half is skipped, not failed.
    """
    maker = _session_factory()
    try:
        async with maker() as db:
            await session_tick.tick_session_statuses(db)
        async with maker() as db:
            await session_tick.fill_running_sessions(db)
    except Exception as exc:  # noqa: BLE001 - logged; the pass goes on
        logger.error(with_log_prefix(LOG_FAILURE, f"tick_failed: {exc!r}"))

    if not drive.drive_configured():
        # Without Drive credentials every build and Drive job would fail and
        # burn its retries; while deejaytools-api serves the same database,
        # that would exhaust jobs it could still run. They wait instead.
        global _drive_skip_logged
        if not _drive_skip_logged:
            _drive_skip_logged = True
            logger.warning(
                with_log_prefix(
                    LOG_WARNING,
                    "drive_work_skipped: Google Drive is not configured; song "
                    "builds and Drive jobs stay queued",
                )
            )
        return

    try:
        # Song builds first: a finished build queues its event copies,
        # which the Drive pass below can then run in the same tick.
        async with maker() as db:
            await song_builds.process_song_uploads(db)
    except Exception as exc:  # noqa: BLE001 - logged and reported
        logger.error(with_log_prefix(LOG_FAILURE, f"song_builds_tick_failed: {exc!r}"))
        with sentry_sdk.new_scope() as scope:
            scope.set_level("error")
            scope.set_tag("subsystem", "song_builds")
            sentry_sdk.capture_exception(exc)

    try:
        async with maker() as db:
            await drive_jobs.process_drive_jobs(db)
    except Exception as exc:  # noqa: BLE001 - logged and reported
        logger.error(with_log_prefix(LOG_FAILURE, f"drive_jobs_tick_failed: {exc!r}"))
        with sentry_sdk.new_scope() as scope:
            scope.set_level("error")
            scope.set_tag("subsystem", "drive_jobs")
            sentry_sdk.capture_exception(exc)


def scheduler_enabled(settings: Settings) -> bool:
    """Whether the background loop may run: not disabled, and not under test."""
    return not settings.DISABLE_SCHEDULER and settings.ENVIRONMENT != "test"


class Scheduler:
    """A background loop calling ``tick`` now and then every ``interval_ms``.

    A tick that comes due while the previous one is still running is skipped
    (overlap guard), as deejaytools-api's setInterval loop skips it.
    """

    def __init__(
        self,
        interval_ms: int,
        tick: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.interval_ms = interval_ms
        # Looked up per call, not bound here, so the pass is always the
        # module's current run_tick.
        self._tick = tick if tick is not None else (lambda: run_tick())
        self._running = False
        self._loop_task: asyncio.Task[None] | None = None
        self._tick_task: asyncio.Task[bool] | None = None

    @property
    def started(self) -> bool:
        """Whether the loop is running."""
        return self._loop_task is not None

    async def tick_once(self) -> bool:
        """Run one tick unless one is in flight; False when skipped."""
        if self._running:
            return False
        self._running = True
        try:
            await self._tick()
        finally:
            self._running = False
        return True

    def _fire(self) -> None:
        # Not awaited by the loop, so the interval keeps its cadence and a
        # slow pass is what the overlap guard sees.
        if self._running:
            return
        self._tick_task = asyncio.create_task(self.tick_once())

    async def _loop(self) -> None:
        while True:
            self._fire()
            await asyncio.sleep(self.interval_ms / 1000)

    def start(self) -> None:
        """Start the loop on the running event loop; the first tick runs now."""
        if self._loop_task is not None:
            return
        self._loop_task = asyncio.create_task(self._loop())
        logger.info(
            with_log_prefix(
                LOG_START, f"scheduler_started interval_ms={self.interval_ms}"
            )
        )

    async def stop(self) -> None:
        """Stop the loop, give a pass in flight a short grace, then cancel it."""
        if self._loop_task is None:
            return
        self._loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._loop_task
        self._loop_task = None
        tick = self._tick_task
        if tick is not None and not tick.done():
            try:
                await asyncio.wait_for(asyncio.shield(tick), SHUTDOWN_GRACE_SECONDS)
            except TimeoutError:
                tick.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await tick
        logger.info(with_log_prefix(LOG_WARNING, "scheduler_stopped"))


def start_scheduler(settings: Settings | None = None) -> Scheduler | None:
    """Start the background loop if enabled; None when it is off."""
    settings = settings or get_settings()
    if not scheduler_enabled(settings):
        logger.info(
            with_log_prefix(
                LOG_WARNING,
                f"scheduler_disabled disable_scheduler={settings.DISABLE_SCHEDULER} "
                f"env={settings.ENVIRONMENT}",
            )
        )
        return None
    scheduler = Scheduler(settings.TICK_INTERVAL_MS)
    scheduler.start()
    return scheduler
