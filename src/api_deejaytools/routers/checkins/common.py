"""The error responses every ``/v1/checkins`` route documents."""

from __future__ import annotations

from typing import Any

from ...errors import ErrorResponse

AUTH: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid token."},
    403: {"model": ErrorResponse, "description": "Lacks the scope."},
}
