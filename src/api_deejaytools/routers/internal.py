"""``GET /internal/tick`` — the operator route (deejaytools-api API.md, ADR-007).

Runs one scheduler pass (``services.scheduler.run_tick``: session status
advance, active-queue auto-fill, one batch of the Drive job queue) and answers
once it has finished. Errors inside the pass are logged, never returned.

Unversioned on purpose: operators, monitors and the conformance suite call it
at this stable path. ``evaluator.yaml`` carries the API-004 exemption for it
(deejaytools-api ADR-009). Being outside ``/v1/*`` it is also outside the rate
limit and the request deadline, which cover only ``/v1/*`` (middleware.py), as
in deejaytools-api.

Gate: the ``x-tick-secret`` header must equal ``TICK_SECRET``, compared in
constant time; otherwise ``403 FORBIDDEN "Admin access required"``. An empty
``TICK_SECRET`` is a set secret, as in deejaytools-api. **Difference from
deejaytools-api (ADR-007):** with ``TICK_SECRET`` unset the route fails closed
with the same 403, where deejaytools-api left it open.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field

from ..config import get_settings
from ..errors import ErrorResponse, Meta, forbidden, success
from ..services import scheduler

router = APIRouter(tags=["internal"])


class Ticked(BaseModel):
    """What a finished pass reports."""

    ticked: bool = Field(True, description="Always true once the pass has run.")


class TickResponse(BaseModel):
    """The success envelope around ``Ticked``."""

    data: Ticked = Field(..., description="The pass's result.")
    meta: Meta = Field(..., description="Envelope metadata.")


def _secret_matches(given: str | None) -> bool:
    secret = get_settings().TICK_SECRET
    if secret is None or given is None:
        return False
    return hmac.compare_digest(given.encode(), secret.encode())


def require_tick_secret() -> Callable[[str | None], None]:
    """The route's gate as a dependency: 403 unless ``x-tick-secret`` matches
    ``TICK_SECRET``. Declared at the registration so the guard is readable
    where the route is (AUTH-003)."""

    def _dependency(
        x_tick_secret: Annotated[str | None, Header()] = None,
    ) -> None:
        if not _secret_matches(x_tick_secret):
            raise forbidden()

    return _dependency


@router.get(
    "/internal/tick",
    summary="Run one scheduler pass",
    description=(
        "Operator route: one scheduler pass (session status advance, queue "
        "auto-fill, one Drive job batch), answered after it finishes. Requires "
        "x-tick-secret equal to TICK_SECRET; refuses every call when "
        "TICK_SECRET is unset."
    ),
    response_model=TickResponse,
    responses={403: {"model": ErrorResponse, "description": "Bad or no secret."}},
)
async def tick(
    _gate: None = Depends(require_tick_secret()),
) -> dict[str, object]:
    """Run ``run_tick()`` for a caller holding the tick secret."""
    await scheduler.run_tick()
    return success({"ticked": True})
