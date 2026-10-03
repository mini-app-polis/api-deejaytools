"""Routes outside the API proper: the root redirect to the docs, liveness,
the deployed version, and the catch-all OPTIONS answer.

All intentionally public and unversioned. They are on a module-level router
rather than registered inside ``main._build_app`` so a static audit of route
guards (AUTH-003) can see that they carry none.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from .. import __version__

router = APIRouter()


class HealthResponse(BaseModel):
    """Liveness answer. Not wrapped in the envelope, as in deejaytools-api."""

    status: str = Field("ok", description="Always 'ok' while the process serves.")


class VersionResponse(BaseModel):
    """The running build. Not wrapped in the envelope, like /health."""

    version: str = Field(..., description="Package version.")
    commit: str | None = Field(
        None, description="Git commit Railway built from; null outside Railway."
    )


@router.get(
    "/",
    include_in_schema=False,
    summary="Root redirect to interactive docs",
    description=(
        "Redirects to /docs (Swagger UI), as api-kaianolevine-com does. "
        "Intentionally public — the redirect target is itself publicly "
        "browsable documentation. deejaytools-api answered 404 here; the web "
        "app never calls it."
    ),
    response_model=None,
)
async def root() -> RedirectResponse:
    """Redirect to the Swagger UI at /docs. Intentionally public."""
    return RedirectResponse(url="/docs")


@router.head("/health", include_in_schema=False)
@router.get(
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


@router.get(
    "/version",
    tags=["meta"],
    summary="Deployed version",
    description=(
        "The package version and the commit this deploy was built from. "
        "Intentionally public and unversioned: the post-deploy smoke test "
        "(.github/workflows/deployed.yml) waits for `commit` to be the "
        "deploy it was triggered by. Not a deejaytools-api route; the web "
        "app never calls it."
    ),
    response_model=VersionResponse,
)
async def version() -> dict[str, str | None]:
    """The deployed version and commit. Intentionally public."""
    return {
        "version": __version__,
        "commit": os.environ.get("RAILWAY_GIT_COMMIT_SHA"),
    }


@router.options("/{path:path}", include_in_schema=False)
async def options(path: str) -> Response:
    """Any OPTIONS request, preflight or not, answers 204 as Hono's cors
    middleware does. Real preflights are answered by CORSMiddleware first."""
    return Response(status_code=204)
