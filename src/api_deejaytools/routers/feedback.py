"""``/v1/feedback`` (deejaytools-api docs/API.md, src/routes/feedback.ts).

Public (API-008; listed in ``auth``): anyone can send site feedback. With a
Brevo key configured it is emailed to the maintainer, with an optional PNG or
JPEG screenshot attached; without one it is accepted and only logged.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Any, ClassVar, Literal

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from mini_app_polis.logger import LOG_FAILURE, LOG_WARNING, get_logger, with_log_prefix
from pydantic import AfterValidator, BaseModel, BeforeValidator, Field, model_validator

from ..config import get_settings
from ..errors import ErrorResponse, Meta, error_body, success
from ..services import notifications
from ..validation import Email, ZodModel
from ..zod_types import documents_zod_body, parse_zod_body

logger = get_logger()

router = APIRouter(prefix="/v1/feedback", tags=["feedback"])

BREVO_URL = "https://api.brevo.com/v3/smtp/email"
SENDER = {"name": "DeejayTools Feedback", "email": "kaiano@kaianolevine.com"}
RECIPIENT = "kaiano.levine@gmail.com"

DATA_URL_PREFIX = re.compile(r"^data:image/(png|jpeg);base64,", re.IGNORECASE)
SCREENSHOT_MAX = 3 * 1024 * 1024
RULE = "═" * 31


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


class FeedbackResponse(BaseModel):
    """Accepted. ``data`` is always null."""

    data: None = Field(None, description="Always null.")
    meta: Meta = Field(..., description="Response metadata.")


def _iso_now() -> str:
    """``new Date().toISOString()``: UTC, milliseconds, a Z suffix."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _email_body(body: FeedbackBody, submitted_at: str) -> str:
    text = (
        f"\n{RULE}\nFEEDBACK DETAILS\n{RULE}\n"
        f"Type:    {body.type}\n"
        f"Subject: {body.subject}\n"
        f"\n{body.message}\n"
        f"\n{RULE}\nCONTACT\n{RULE}\n"
        f"Name:  {body.contactName if body.contactName is not None else 'Not provided'}\n"  # noqa: E501
        f"Email: {body.contactEmail if body.contactEmail is not None else 'Not provided'}\n"  # noqa: E501
        f"\n{RULE}\nMETADATA\n{RULE}\n"
        f"Submitted: {submitted_at}\n"
        f"{RULE}\n"
    )
    # The template starts and ends with a newline that trim() drops.
    return text[1:-1]


@router.post(
    "",
    status_code=201,
    response_model=FeedbackResponse,
    summary="Send site feedback",
    description=(
        "Intentionally public. Emails the feedback through Brevo when a key is configured, "
        "with the screenshot attached; accepted without email otherwise."
    ),
    responses={
        400: {"model": ErrorResponse, "description": "Invalid body or screenshot."},
        502: {"model": ErrorResponse, "description": "Brevo refused the email."},
    },
)
@documents_zod_body(FeedbackBody)
async def send_feedback(request: Request) -> Any:
    """Send site feedback. Intentionally public, as in deejaytools-api."""
    body = await parse_zod_body(request, FeedbackBody)
    screenshot = body.screenshot or None
    screenshot_base64 = (
        screenshot.split(",")[1] if screenshot and "," in screenshot else None
    )
    email_body = _email_body(body, _iso_now())

    brevo_key = get_settings().brevo_api_key
    if brevo_key:
        payload: dict[str, Any] = {
            "sender": SENDER,
            "to": [{"email": RECIPIENT}],
            "subject": f"[DeejayTools Feedback] {body.type}: {body.subject}",
            "textContent": email_body,
        }
        if screenshot and screenshot_base64:
            is_jpeg = screenshot.startswith("data:image/jpeg")
            payload["attachment"] = [
                {
                    "content": screenshot_base64,
                    "name": "screenshot.jpg" if is_jpeg else "screenshot.png",
                }
            ]

        # No client timeout, as fetch has none: the request deadline bounds it.
        async with httpx.AsyncClient(timeout=None) as client:
            res = await client.post(
                BREVO_URL,
                headers={"api-key": brevo_key, "Content-Type": "application/json"},
                json=payload,
            )
        if not res.is_success:
            logger.error(
                with_log_prefix(
                    LOG_FAILURE,
                    f"feedback brevo_error status={res.status_code} body={res.text!r}",
                )
            )
            return JSONResponse(
                status_code=502,
                content=error_body(
                    "EMAIL_FAILED", "Failed to send email. Please try again."
                ),
            )
    else:
        logger.warning(
            with_log_prefix(
                LOG_WARNING,
                "feedback DEEJAYTOOLS_BREVO_API_KEY not set; "
                "skipping transactional email",
            )
        )

    # After the email, so a Brevo refusal (502, already a fault in errors)
    # is not also announced as feedback received.
    notifications.announce_feedback(
        body.type,
        body.subject,
        emailed=bool(brevo_key),
        screenshot=bool(screenshot),
    )
    return success(None)
