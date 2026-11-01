"""Routes outside the API proper: the root redirect to the docs, liveness,
the deployed version, and the catch-all OPTIONS answer under /v1/.

All intentionally public; all but the OPTIONS answer are unversioned. They are on a module-level router
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
    """Liveness answer. Not wrapped in the envelope."""

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
        "Redirects to /docs (Swagger UI). Intentionally public — the redirect "
        "target is itself publicly browsable documentation. The web app never "
        "calls it."
    ),
    response_model=None,
    response_class=RedirectResponse,
)
async def root() -> RedirectResponse:
    """Redirect to the Swagger UI at /docs. Intentionally public."""
    return RedirectResponse(url="/docs")


@router.head(
    "/health",
    include_in_schema=False,
    summary="Liveness probe (HEAD)",
    description="The GET liveness probe without a body. Intentionally public.",
    response_model=HealthResponse,
)
@router.get(
    "/health",
    tags=["meta"],
    summary="Liveness probe",
    description=(
        "Always 200 {'status': 'ok'} while the process serves: no auth and "
        "no database query (API-010). Intentionally public. It answers 200 "
        "even when the database is down (ADR-0010)."
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
        "deploy it was triggered by. The web app reads it to show the API's "
        "version."
    ),
    response_model=VersionResponse,
)
async def version() -> dict[str, str | None]:
    """The deployed version and commit. Intentionally public."""
    return {
        "version": __version__,
        "commit": os.environ.get("RAILWAY_GIT_COMMIT_SHA"),
    }


@router.options(
    "/v1/{path:path}",
    include_in_schema=False,
    summary="Any OPTIONS request under /v1/",
    description=(
        "204 with no body for any path under /v1/. Intentionally public: a "
        "CORS preflight carries no credentials."
    ),
    response_class=Response,
)
async def options(path: str) -> Response:
    """Any OPTIONS request under /v1/, preflight or not, answers 204 with no
    body. Real preflights are answered by CORSMiddleware first."""
    return Response(status_code=204)
