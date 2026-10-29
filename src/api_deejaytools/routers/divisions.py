"""``/v1/divisions``: the competition divisions, in their groups.

Intentionally public (API-008; listed in ``auth``): the list is the same for
everyone and names no one. It is served from ``domain.DIVISION_GROUPS``, the
single source of truth, so the web app reads it here rather than keeping a
copy of its own.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..domain import DIVISION_GROUPS, DIVISIONS
from ..errors import Meta, success

router = APIRouter(prefix="/v1/divisions", tags=["divisions"])


class DivisionsData(BaseModel):
    """The divisions, grouped and flat, in display order."""

    groups: list[list[str]] = Field(
        ..., description="The divisions in their display groups, in order."
    )
    divisions: list[str] = Field(
        ..., description="Every division in display order: the groups, flattened."
    )


class DivisionsResponse(BaseModel):
    """The division list."""

    data: DivisionsData = Field(..., description="The divisions.")
    meta: Meta = Field(..., description="Response metadata.")


@router.get(
    "",
    response_model=DivisionsResponse,
    summary="List divisions",
    description=(
        "The competition divisions in their display groups, and flat in the "
        "same order. Intentionally public."
    ),
)
async def list_divisions() -> dict[str, Any]:
    """List the divisions. Intentionally public."""
    return success(
        {
            "groups": [list(group) for group in DIVISION_GROUPS],
            "divisions": list(DIVISIONS),
        }
    )
