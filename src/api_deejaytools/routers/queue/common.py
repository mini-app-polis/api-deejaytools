"""What the queue's floor-manager actions share: the event a request is
decided on, their request bodies, their response, and their error responses.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from ...auth import EventRef
from ...errors import Envelope, ErrorResponse
from ...validation import NonEmptyStr, ZodModel

# The event an action is decided on (ADR-0012): the body's queue entry's. The
# scope through a role (an admin's) allows anywhere.
ENTRY_EVENT = EventRef(source="body", name="queueEntryId", kind="queue_entry")

# Out of range of any real position, for rotate and move-down.
SWAP_SENTINEL = 2_000_000


class PromoteBody(ZodModel):
    """Body of ``POST /v1/queue/promote`` and ``/move-down``."""

    queueEntryId: NonEmptyStr = Field(..., description="Queue entry id.")


class EntryActionBody(ZodModel):
    """Body of ``/complete``, ``/incomplete`` and ``/withdraw``."""

    _NULLABLE: ClassVar[frozenset[str]] = frozenset({"reason"})

    queueEntryId: NonEmptyStr = Field(..., description="Queue entry id.")
    reason: str | None = Field(None, description="Recorded on the audit row.")


class ActionResponse(Envelope[dict[str, bool]]):
    """Action confirmation: {'promoted'|'completed'|'rotated'|'moved'|'withdrawn': true}."""

    data: dict[str, bool] = Field(..., description="Which action succeeded.")


MANAGE: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid token."},
    403: {"model": ErrorResponse, "description": "Lacks the scope on this event."},
    409: {"model": ErrorResponse, "description": "Concurrent activity; retry."},
}


class _EntryMissing(Exception):
    """The entry left the active queue between the read and the lock."""
