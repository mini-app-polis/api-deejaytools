"""The api-deejaytools FastAPI app.

Replaces deejaytools-api (Hono/Node) on the same database, answering the
web app exactly as it does (deejaytools-api ADR-006, ADR-009). Package
identity (DOC-009): the repository is api-deejaytools, the distribution is
``api-deejaytools`` and the import package is ``api_deejaytools``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from mini_app_polis.logger import LOG_START, LOG_WARNING, get_logger, with_log_prefix
from mini_app_polis.request_metrics import RequestMetricsMiddleware
from pydantic import BaseModel, Field
from sentry_sdk.integrations.fastapi import FastApiIntegration

from . import __version__
from .config import get_settings
from .errors import install_error_handlers
from .routers import auth
from .services import cloudwatch

logger = get_logger()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialize and tear down process-level app resources."""
    settings = get_settings()
    if settings.SENTRY_DSN_API_DEEJAYTOOLS:
        sentry_sdk.init(
            dsn=settings.SENTRY_DSN_API_DEEJAYTOOLS,
            integrations=[FastApiIntegration()],
            environment=settings.ENVIRONMENT,
            traces_sample_rate=1.0,
        )
    logger.info(
        with_log_prefix(
            LOG_START,
            f"api-deejaytools starting (env={settings.ENVIRONMENT}, "
            f"sentry={'on' if settings.SENTRY_DSN_API_DEEJAYTOOLS else 'off'})",
        )
    )
    yield
    logger.info(with_log_prefix(LOG_WARNING, "api-deejaytools shutting down"))


class HealthResponse(BaseModel):
    """Liveness answer. Not wrapped in the envelope, as in deejaytools-api."""

    status: str = Field("ok", description="Always 'ok' while the process serves.")


def _build_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="api-deejaytools",
        version=__version__,
        lifespan=lifespan,
    )

    # As deejaytools-api: origins from DEEJAYTOOLS_CORS_ORIGINS, these
    # methods and headers, no credentials (the web app sends a bearer token).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.DEEJAYTOOLS_CORS_ORIGINS,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    # Outermost, so its clock covers everything the client waits for.
    app.add_middleware(
        RequestMetricsMiddleware,
        service="api-deejaytools",
        client_factory=lambda: cloudwatch.client_factory(settings),
        exclude_paths=["/health"],
    )

    install_error_handlers(app)

    @app.get(
        "/health",
        tags=["meta"],
        summary="Liveness probe",
        description=(
            "Always 200 {'status': 'ok'} while the process serves: no auth and "
            "no database query (API-010). Intentionally public. Unlike "
            "deejaytools-api it does not answer 503 when the database is down "
            "(ADR-009)."
        ),
        response_model=HealthResponse,
    )
    async def health() -> dict[str, str]:
        """Liveness probe. Intentionally public — no auth, no DB access."""
        return {"status": "ok"}

    app.include_router(auth.router)
    return app


app = _build_app()
