"""Emailing site feedback through Brevo, and retrying it until it is sent.

``POST /v1/feedback`` saves each submission to ``site_feedback`` first and
then tries to email it at once (``deliver``). A refused or unreachable Brevo
leaves the row ``pending`` with a later ``next_attempt_at``, and the
scheduler's pass retries it (``process_pending_feedback``) with backoff until
it is sent or ``MAX_ATTEMPTS`` is reached, when it is marked ``failed`` and
reported. While Brevo or the feedback addresses are not configured, rows
wait: nothing is tried and nothing is counted against them.

The message is kept in the database, never in a log line or a Discord post:
the route is public and those channels are shared.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import sentry_sdk
from mini_app_polis.logger import (
    LOG_FAILURE,
    LOG_SUCCESS,
    get_logger,
    with_log_prefix,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..clock import now_ms
from ..config import get_settings
from ..domain import full_name
from ..models import SiteFeedback, User
from . import notifications

logger = get_logger()

#: How long one Brevo call may take. Bounded: the route waits on it, and the
#: scheduler's pass holds its batch while it runs.
BREVO_TIMEOUT_SECONDS = 10.0

BREVO_URL = "https://api.brevo.com/v3/smtp/email"
SENDER_NAME = "DeejayTools Feedback"
RULE = "═" * 31

MAX_ATTEMPTS = 10
"""Tries before a row is marked ``failed`` and reported: about three hours
with the backoff below, long enough to outlast a Brevo incident or a fix
to its settings."""

FIRST_RETRY_GRACE_MS = 5 * 60_000
"""When a new row is first due for the scheduler. The route emails it at
once; this only matters if the process stops before the route records the
outcome, and keeps the scheduler from emailing it a second time meanwhile."""

DEFAULT_BATCH_SIZE = 5
"""Rows retried per pass. Each holds its row lock for up to one Brevo call."""

FAULT_KEY = "feedback.email"


def backoff_ms(attempts: int) -> int:
    """Delay before the next try, given the attempts made so far:
    ``min(60 s × 2^attempts, 30 min)``."""
    return min(60_000 * 2**attempts, 30 * 60_000)


def configured() -> bool:
    """Whether feedback can be emailed: a Brevo key and both addresses."""
    settings = get_settings()
    return bool(settings.brevo_api_key and settings.feedback_addresses)


def _iso(ms: int) -> str:
    """``new Date(ms).toISOString()``: UTC, milliseconds, a Z suffix."""
    at = datetime.fromtimestamp(ms / 1000, UTC)
    return at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"


def signed_in_as(user_id: str | None, user: User | None) -> str:
    """Who sent it, for the email: their name and email from ``users`` with
    the id after them, the bare id when no such user is on file, or that
    nobody was signed in."""
    if user_id is None:
        return "Not signed in"
    if user is None:
        return user_id
    name = full_name(user.first_name, user.last_name) or (user.display_name or "")
    who = " ".join(
        part for part in (name, f"<{user.email}>" if user.email else "") if part
    )
    return f"{who} ({user_id})" if who else user_id


async def _signed_in_as(db: AsyncSession, row: SiteFeedback) -> str:
    """``signed_in_as`` for ``row``, looking its sender up in ``users``."""
    user = await db.get(User, row.user_id) if row.user_id is not None else None
    return signed_in_as(row.user_id, user)


def email_body(row: SiteFeedback, signed_in: str | None = None) -> str:
    """The plain-text email for one piece of feedback. ``signed_in`` is who
    sent it (``signed_in_as``); without it, the stored id as it is."""
    if signed_in is None:
        signed_in = signed_in_as(row.user_id, None)
    text = (
        f"\n{RULE}\nFEEDBACK DETAILS\n{RULE}\n"
        f"Type:    {row.type}\n"
        f"Subject: {row.subject}\n"
        f"\n{row.message}\n"
        f"\n{RULE}\nCONTACT\n{RULE}\n"
        f"Name:  {row.contact_name if row.contact_name is not None else 'Not provided'}\n"  # noqa: E501
        f"Email: {row.contact_email if row.contact_email is not None else 'Not provided'}\n"  # noqa: E501
        f"Signed in as: {signed_in}\n"
        f"\n{RULE}\nMETADATA\n{RULE}\n"
        f"Submitted: {_iso(row.created_at)}\n"
        f"Reference: {row.id}\n"
        f"{RULE}\n"
    )
    # The template starts and ends with a newline that trim() drops.
    return text[1:-1]


def _payload(
    row: SiteFeedback, sender: str, recipient: str, signed_in: str
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "sender": {"name": SENDER_NAME, "email": sender},
        "to": [{"email": recipient}],
        "subject": f"[DeejayTools Feedback] {row.type}: {row.subject}",
        "textContent": email_body(row, signed_in),
    }
    screenshot = row.screenshot or None
    content = screenshot.split(",")[1] if screenshot and "," in screenshot else None
    if screenshot and content:
        is_jpeg = screenshot.startswith("data:image/jpeg")
        payload["attachment"] = [
            {
                "content": content,
                "name": "screenshot.jpg" if is_jpeg else "screenshot.png",
            }
        ]
    return payload


async def _send(row: SiteFeedback, signed_in: str) -> str | None:
    """Email ``row`` through Brevo. None when sent, else why not.

    Never raises. A refusal or an unreachable Brevo is reported to Sentry
    here; the caller records the outcome.
    """
    settings = get_settings()
    key = settings.brevo_api_key
    addresses = settings.feedback_addresses
    if not key or addresses is None:
        return "not configured"
    sender, recipient = addresses
    try:
        async with httpx.AsyncClient(timeout=BREVO_TIMEOUT_SECONDS) as client:
            res = await client.post(
                BREVO_URL,
                headers={"api-key": key, "Content-Type": "application/json"},
                json=_payload(row, sender, recipient, signed_in),
            )
    except httpx.HTTPError as exc:
        sentry_sdk.capture_exception(exc)
        return f"brevo_unreachable: {exc!r}"
    if not res.is_success:
        sentry_sdk.capture_message(
            f"Brevo refused feedback email: {res.status_code}", level="error"
        )
        return f"brevo_error status={res.status_code} body={res.text!r}"
    return None


class FeedbackEmailFailed(Exception):
    """Brevo did not take a feedback email. Its text is the reason."""


def _record(row: SiteFeedback, failure: str | None, now: int) -> None:
    """Write one attempt's outcome onto ``row`` (not committed)."""
    row.updated_at = now
    if failure is None:
        row.status = "emailed"
        row.emailed_at = now
        row.last_error = None
        # Sent: the screenshot has done its job and need not be kept.
        row.screenshot = None
        notifications.clear_fault(FAULT_KEY)
        logger.info(with_log_prefix(LOG_SUCCESS, f"feedback_emailed id={row.id}"))
        return
    row.attempts += 1
    row.last_error = failure
    logger.error(
        with_log_prefix(
            LOG_FAILURE,
            f"feedback_email_failed id={row.id} attempts={row.attempts} {failure}",
        )
    )
    if row.attempts >= MAX_ATTEMPTS:
        row.status = "failed"
        notifications.report_fault(
            "feedback · email (gave up; kept in site_feedback)",
            FeedbackEmailFailed(failure),
        )
        return
    row.next_attempt_at = now + backoff_ms(row.attempts)
    notifications.report_fault_once(
        FAULT_KEY,
        "feedback · email (kept; will retry)",
        FeedbackEmailFailed(failure),
    )


async def deliver(db: AsyncSession, row: SiteFeedback) -> bool:
    """Try to email a just-saved row now and record the outcome.

    True when it was emailed. Not tried, and False, while email is not
    configured: the row waits for the scheduler.
    """
    if not configured():
        return False
    failure = await _send(row, await _signed_in_as(db, row))
    _record(row, failure, now_ms())
    await db.commit()
    return failure is None


async def process_pending_feedback(
    db: AsyncSession, *, limit: int = DEFAULT_BATCH_SIZE
) -> int:
    """Retry due ``pending`` rows; the number emailed. One pass's batch.

    Rows are locked with ``FOR UPDATE SKIP LOCKED`` for the pass, so the
    scheduler in every worker can run this at once without emailing a row
    twice. Nothing is tried while email is not configured.
    """
    if not configured():
        return 0
    now = now_ms()
    rows = (
        (
            await db.execute(
                select(SiteFeedback)
                .where(
                    SiteFeedback.status == "pending",
                    SiteFeedback.next_attempt_at <= now,
                )
                .order_by(SiteFeedback.next_attempt_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    emailed = 0
    for row in rows:
        failure = await _send(row, await _signed_in_as(db, row))
        _record(row, failure, now_ms())
        emailed += failure is None
    await db.commit()
    return emailed
