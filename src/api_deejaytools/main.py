"""The api-deejaytools FastAPI app.

Replaces deejaytools-api (Hono/Node) on the same database, answering the
web app exactly as it does (deejaytools-api ADR-006, ADR-009). Package
identity (DOC-009): the repository is api-deejaytools, the distribution is
``api-deejaytools`` and the import package is ``api_deejaytools``.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress

import sentry_sdk
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from mini_app_polis import activity
from mini_app_polis.logger import LOG_START, LOG_WARNING, get_logger, with_log_prefix
from mini_app_polis.request_metrics import RequestMetricsMiddleware
from sentry_sdk.integrations.fastapi import FastApiIntegration

from . import __version__
from .config import get_settings
from .errors import install_error_handlers
from .middleware import BodyLimitMiddleware, DeadlineMiddleware, RateLimitMiddleware
from .routers import (
    admin_checkins,
    admin_drive_jobs,
    admin_event_submissions,
    admin_songs,
    admin_users,
    auth,
    checkins,
    event_song_submissions,
    events,
    feedback,
    internal,
    managed_partnerships,
    meta,
    pairs,
    partners,
    queue,
    runs,
    sessions,
    song_uploads,
    songs,
    teams,
)
from .services import cloudwatch, notifications
from .services.scheduler import start_scheduler
from .zod_types import document_zod_bodies

logger = get_logger()

NOTIFICATION_DRAIN_SECONDS = 5.0
"""How long shutdown waits for Discord messages still in flight."""


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialize and tear down process-level app resources."""
    settings = get_settings()
    if settings.SENTRY_DSN_APIS:
        sentry_sdk.init(
            dsn=settings.SENTRY_DSN_APIS,
            integrations=[FastApiIntegration()],
            environment=settings.ENVIRONMENT,
            traces_sample_rate=1.0,
        )
        sentry_sdk.get_global_scope().set_tag("service", "api-deejaytools")
    logger.info(
        with_log_prefix(
            LOG_START,
            f"api-deejaytools starting (env={settings.ENVIRONMENT}, "
            f"sentry={'on' if settings.SENTRY_DSN_APIS else 'off'})",
        )
    )
    # Off under DISABLE_SCHEDULER=1 and ENVIRONMENT=test (services/scheduler.py).
    scheduler = start_scheduler(settings)
    yield
    if scheduler is not None:
        await scheduler.stop()
    # Let Discord messages already dispatched (a last fault, a song added)
    # go out, briefly: a deploy must not wait on Discord.
    with suppress(TimeoutError):
        await asyncio.wait_for(
            activity.wait_for_deliveries(), NOTIFICATION_DRAIN_SECONDS
        )
    logger.info(with_log_prefix(LOG_WARNING, "api-deejaytools shutting down"))


def _build_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="api-deejaytools",
        version=__version__,
        lifespan=lifespan,
        # Hono answers /v1/events/ with 404, not a redirect to /v1/events.
        redirect_slashes=False,
    )

    # First, so its 500 handling sits inside every middleware below.
    install_error_handlers(app)

    # Starlette runs the last-added middleware first. Inside out: the
    # deadline, the rate limit (both /v1/* only), the body limit, then CORS
    # around all of them so its headers reach every answer, as in
    # deejaytools-api (cors, bodyLimit, rateLimit, timeout).
    app.add_middleware(DeadlineMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(BodyLimitMiddleware)

    # As deejaytools-api: origins from DEEJAYTOOLS_CORS_ORIGINS, these
    # methods and headers, no credentials (the web app sends a bearer token).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.DEEJAYTOOLS_CORS_ORIGINS,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    # Discord: the request's committed changes to activity, a 5xx or an
    # unhandled exception to errors. Added after CORS so it wraps every
    # layer above, and sees the status that went on the wire (a deadline's
    # 503 included) with the fault detail the error middleware recorded.
    # Importing notifications registered the listeners that do the tally.
    app.middleware("http")(notifications.activity_middleware)

    # Outermost, so its clock covers everything the client waits for.
    app.add_middleware(
        RequestMetricsMiddleware,
        service="api-deejaytools",
        client_factory=lambda: cloudwatch.client_factory(settings),
        exclude_paths=["/health", "/version"],
    )

    routers = (
        meta.router,
        internal.router,
        auth.router,
        events.router,
        sessions.router,
        partners.router,
        pairs.router,
        teams.router,
        managed_partnerships.router,
        event_song_submissions.router,
        song_uploads.router,
        songs.router,
        feedback.router,
        admin_users.router,
        admin_songs.router,
        admin_event_submissions.router,
        admin_drive_jobs.router,
        checkins.router,
        queue.router,
        runs.router,
        admin_checkins.router,
    )
    for router in routers:
        app.include_router(router)
    document_zod_bodies(app, routers)
    return app


app = _build_app()
