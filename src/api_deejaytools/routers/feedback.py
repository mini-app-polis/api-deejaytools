"""``/v1/feedback`` (docs/API.md).

Public (API-008; listed in ``auth``): anyone can send site feedback. Each
submission is saved to ``site_feedback`` before anything else, with the
sender's verified user id when they send a token that verifies. With a
Brevo key and both feedback addresses configured (``FEEDBACK_FROM_EMAIL``,
``FEEDBACK_TO_EMAIL``) it is then emailed, with an optional PNG or JPEG
screenshot attached; a refused email is retried by the scheduler
(services/feedback_delivery.py), so it never loses the feedback.
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Any, ClassVar, Literal

from fastapi import APIRouter, Depends, Request
from mini_app_polis.logger import LOG_WARNING, get_logger, with_log_prefix
from pydantic import AfterValidator, BeforeValidator, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import optional_subject
from ..clock import now_ms
from ..database import get_db_session
from ..errors import Envelope, ErrorResponse, success
from ..models import SiteFeedback
from ..services import feedback_delivery, notifications
from ..validation import Email, ZodModel
from ..zod_types import documents_zod_body, parse_zod_body

logger = get_logger()

router = APIRouter(prefix="/v1/feedback", tags=["feedback"])

DATA_URL_PREFIX = re.compile(r"^data:image/(png|jpeg);base64,", re.IGNORECASE)
SCREENSHOT_MAX = 3 * 1024 * 1024


def _js_length(value: str) -> int:
    """``String.prototype.length``: UTF-16 code units, as zod measures."""
    return len(value.encode("utf-16-le")) // 2


def _length(min_length: int | None, max_length: int) -> AfterValidator:
    """zod's ``z.string().min().max()``; None passes through."""

    def _check(value: str | None) -> str | None:
        if value is None:
            return value
        n = _js_length(value)
        if min_length is not None and n < min_length:
            raise ValueError(
                f"Too small: expected string to have >={min_length} characters"
            )
        if n > max_length:
            raise ValueError(
                f"Too big: expected string to have <={max_length} characters"
            )
        return value

    return AfterValidator(_check)


def _blank_to_absent(value: Any) -> Any:
    """zod's preprocess: "" (and null, undefined) count as not sent."""
    return None if value == "" else value


class FeedbackBody(ZodModel):
    """Body of ``POST /v1/feedback``. Field names are the web app's (camelCase)."""

    # The preprocess turns null into "not sent" for these two.
    _NULLABLE: ClassVar[frozenset[str]] = frozenset({"contactName", "contactEmail"})

    type: Literal["bug", "feature", "general"] = Field(
        ..., description="bug, feature or general."
    )
    subject: Annotated[str, _length(1, 255)] = Field(
        ..., description="Subject, 1 to 255 characters."
    )
    message: Annotated[str, _length(1, 20_000)] = Field(
        ..., description="Message, 1 to 20000 characters."
    )
    contactName: Annotated[
        str | None, BeforeValidator(_blank_to_absent), _length(None, 255)
    ] = Field(None, description="Sender's name; empty means not given.")
    contactEmail: Annotated[Email | None, BeforeValidator(_blank_to_absent)] = Field(
        None, description="Sender's email; empty means not given."
    )
    screenshot: Annotated[str | None, _length(None, SCREENSHOT_MAX)] = Field(
        None, description="A PNG or JPEG data URL; empty means none."
    )

    @model_validator(mode="after")
    def _screenshot_is_data_url(self) -> FeedbackBody:
        s = self.screenshot
        if s is not None and len(s) > 0 and not DATA_URL_PREFIX.match(s):
            raise ValueError("screenshot: Screenshot must be a PNG or JPEG data URL")
        return self


class FeedbackResponse(Envelope[None]):
    """Accepted. ``data`` is always null."""

    data: None = Field(None, description="Always null.")


@router.post(
    "",
    status_code=201,
    response_model=FeedbackResponse,
    summary="Send site feedback",
    description=(
        "Intentionally public. Saves the feedback, with the sender's user id "
        "when a valid token is sent, then emails it through Brevo when a key and "
        "both feedback addresses are configured, with the screenshot attached. "
        "Accepted once saved: an email Brevo refuses is retried later."
    ),
    responses={
        400: {"model": ErrorResponse, "description": "Invalid body or screenshot."},
    },
)
@documents_zod_body(FeedbackBody)
async def send_feedback(
    request: Request, db: Annotated[AsyncSession, Depends(get_db_session)]
) -> Any:
    """Send site feedback. Intentionally public."""
    body = await parse_zod_body(request, FeedbackBody)
    # Read, never required: no token or a bad one is simply anonymous. The
    # rate limit has already verified it, and this reuses that outcome.
    subject = await optional_subject(request.headers.get("authorization"))
    now = now_ms()
    row = SiteFeedback(
        id=str(uuid.uuid4()),
        type=body.type,
        subject=body.subject,
        message=body.message,
        contact_name=body.contactName,
        contact_email=body.contactEmail,
        screenshot=body.screenshot or None,
        user_id=subject.subject if subject is not None else None,
        status="pending",
        attempts=0,
        next_attempt_at=now + feedback_delivery.FIRST_RETRY_GRACE_MS,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    await db.commit()

    delivery: notifications.FeedbackDelivery
    if not feedback_delivery.configured():
        logger.warning(
            with_log_prefix(
                LOG_WARNING,
                "feedback BREVO_API_KEY, FEEDBACK_FROM_EMAIL or FEEDBACK_TO_EMAIL "
                f"not set; kept id={row.id} without emailing it",
            )
        )
        delivery = "unconfigured"
    elif await feedback_delivery.deliver(db, row):
        delivery = "emailed"
    else:
        delivery = "retrying"

    notifications.announce_feedback(
        body.type,
        body.subject,
        delivery=delivery,
        screenshot=bool(body.screenshot),
    )
    return success(None)
