"""/v1/feedback: public site feedback, saved first, then emailed through
Brevo when configured, and retried by the scheduler when Brevo refuses it.

Brevo is never called for real (TEST-007): every test runs under a respx
mock that refuses unmocked hosts, and the tests that send assert the exact
request made.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

import asyncpg
import httpx
import pytest
import respx

from api_deejaytools.config import get_settings
from api_deejaytools.database import get_sessionmaker
from api_deejaytools.services import feedback_delivery

from .conftest import Clerk, bearer

URL = "/v1/feedback"
BREVO = "https://api.brevo.com/v3/smtp/email"
RULE = "═" * 31
FROM = "feedback@example.test"
TO = "maintainer@example.test"
ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@pytest.fixture
def brevo(monkeypatch: pytest.MonkeyPatch) -> Iterator[respx.Router]:
    """No Brevo key by default, both addresses set; Brevo's endpoint mocked,
    nothing else reachable."""
    settings = get_settings()
    monkeypatch.setattr(settings, "BREVO_API_KEY", None)
    monkeypatch.setattr(settings, "FEEDBACK_FROM_EMAIL", FROM)
    monkeypatch.setattr(settings, "FEEDBACK_TO_EMAIL", TO)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.post(BREVO, name="brevo").mock(
            return_value=httpx.Response(201, json={"messageId": "<m@brevo>"})
        )
        yield router


def _key(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setattr(get_settings(), name, value)


def _sent(router: respx.Router) -> tuple[httpx.Request, dict[str, Any]]:
    route = router.routes["brevo"]
    assert route.call_count == 1
    request = route.calls.last.request
    return request, json.loads(request.content)


async def test_without_a_key_it_is_accepted_and_nothing_is_sent(
    client: httpx.AsyncClient, brevo: respx.Router
) -> None:
    res = await client.post(
        URL, json={"type": "bug", "subject": "Queue froze", "message": "Stopped."}
    )
    assert res.status_code == 201
    assert res.json() == {
        "data": None,
        "meta": {"version": "v1", "count": 1, "total": 1},
    }
    assert brevo.routes["brevo"].call_count == 0


async def test_with_a_key_it_emails_the_feedback(
    client: httpx.AsyncClient, brevo: respx.Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    _key(monkeypatch, "BREVO_API_KEY", "key-new")
    res = await client.post(
        URL,
        json={
            "type": "feature",
            "subject": "Dark mode",
            "message": "Please\nadd it.",
            "contactName": "Ada",
            "contactEmail": "ada@example.test",
        },
    )
    assert res.status_code == 201, res.text
    assert res.json() == {
        "data": None,
        "meta": {"version": "v1", "count": 1, "total": 1},
    }

    request, payload = _sent(brevo)
    assert request.method == "POST"
    assert request.headers["api-key"] == "key-new"
    assert request.headers["content-type"] == "application/json"
    text = payload.pop("textContent")
    assert payload == {
        "sender": {"name": "DeejayTools Feedback", "email": FROM},
        "to": [{"email": TO}],
        "subject": "[DeejayTools Feedback] feature: Dark mode",
    }
    submitted = ISO.search(text)
    reference = UUID.search(text)
    assert submitted is not None and reference is not None
    assert text == (
        f"{RULE}\nFEEDBACK DETAILS\n{RULE}\n"
        "Type:    feature\n"
        "Subject: Dark mode\n"
        "\nPlease\nadd it.\n"
        f"\n{RULE}\nCONTACT\n{RULE}\n"
        "Name:  Ada\n"
        "Email: ada@example.test\n"
        "Signed in as: Not signed in\n"
        f"\n{RULE}\nMETADATA\n{RULE}\n"
        f"Submitted: {submitted.group(0)}\n"
        f"Reference: {reference.group(0)}\n"
        f"{RULE}"
    )


@pytest.mark.parametrize("missing", ["FEEDBACK_FROM_EMAIL", "FEEDBACK_TO_EMAIL"])
async def test_without_both_addresses_it_is_accepted_and_nothing_is_sent(
    client: httpx.AsyncClient,
    brevo: respx.Router,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    _key(monkeypatch, "BREVO_API_KEY", "key")
    monkeypatch.setattr(get_settings(), missing, None)
    res = await client.post(
        URL, json={"type": "bug", "subject": "Queue froze", "message": "Stopped."}
    )
    assert res.status_code == 201, res.text
    assert res.json() == {
        "data": None,
        "meta": {"version": "v1", "count": 1, "total": 1},
    }
    assert brevo.routes["brevo"].call_count == 0


async def test_blank_contact_fields(
    client: httpx.AsyncClient, brevo: respx.Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    _key(monkeypatch, "BREVO_API_KEY", "key")
    res = await client.post(
        URL,
        json={
            "type": "general",
            "subject": "Hi",
            "message": "Thanks",
            "contactName": "",
            "contactEmail": None,
            "screenshot": "",
        },
    )
    assert res.status_code == 201, res.text
    request, payload = _sent(brevo)
    assert request.headers["api-key"] == "key"
    assert "Name:  Not provided\nEmail: Not provided\n" in payload["textContent"]
    assert "attachment" not in payload


@pytest.mark.parametrize(
    ("screenshot", "name", "content"),
    [
        ("data:image/png;base64,iVBORw0KGgo=", "screenshot.png", "iVBORw0KGgo="),
        ("data:image/jpeg;base64,/9j/4AAQ", "screenshot.jpg", "/9j/4AAQ"),
        # The prefix check ignores case; the JPEG check does not.
        ("DATA:IMAGE/JPEG;BASE64,/9j/4AAQ", "screenshot.png", "/9j/4AAQ"),
    ],
)
async def test_screenshot_is_attached(
    client: httpx.AsyncClient,
    brevo: respx.Router,
    monkeypatch: pytest.MonkeyPatch,
    screenshot: str,
    name: str,
    content: str,
) -> None:
    _key(monkeypatch, "BREVO_API_KEY", "key")
    res = await client.post(
        URL,
        json={"type": "bug", "subject": "s", "message": "m", "screenshot": screenshot},
    )
    assert res.status_code == 201, res.text
    _, payload = _sent(brevo)
    assert payload["attachment"] == [{"content": content, "name": name}]


async def test_empty_base64_is_not_attached(
    client: httpx.AsyncClient, brevo: respx.Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    _key(monkeypatch, "BREVO_API_KEY", "key")
    res = await client.post(
        URL,
        json={
            "type": "bug",
            "subject": "s",
            "message": "m",
            "screenshot": "data:image/png;base64,",
        },
    )
    assert res.status_code == 201
    _, payload = _sent(brevo)
    assert "attachment" not in payload


async def _rows(db: asyncpg.Connection) -> list[asyncpg.Record]:
    return await db.fetch("SELECT * FROM site_feedback ORDER BY created_at")


async def test_it_is_saved_before_it_is_emailed(
    client: httpx.AsyncClient,
    brevo: respx.Router,
    monkeypatch: pytest.MonkeyPatch,
    db: asyncpg.Connection,
) -> None:
    _key(monkeypatch, "BREVO_API_KEY", "key")
    res = await client.post(
        URL,
        json={
            "type": "bug",
            "subject": "Queue froze",
            "message": "Stopped at 3pm.",
            "contactName": "Ada",
            "contactEmail": "ada@example.test",
            "screenshot": "data:image/png;base64,iVBORw0KGgo=",
        },
    )
    assert res.status_code == 201, res.text
    (row,) = await _rows(db)
    assert (row["type"], row["subject"], row["message"]) == (
        "bug",
        "Queue froze",
        "Stopped at 3pm.",
    )
    assert (row["contact_name"], row["contact_email"]) == ("Ada", "ada@example.test")
    assert row["user_id"] is None
    assert row["status"] == "emailed"
    assert row["emailed_at"] is not None and row["attempts"] == 0
    # Sent: the screenshot is not kept.
    assert row["screenshot"] is None
    _, payload = _sent(brevo)
    assert f"Reference: {row['id']}" in payload["textContent"]


async def test_without_a_key_it_is_kept_for_later(
    client: httpx.AsyncClient, brevo: respx.Router, db: asyncpg.Connection
) -> None:
    res = await client.post(URL, json={"type": "bug", "subject": "s", "message": "m"})
    assert res.status_code == 201
    (row,) = await _rows(db)
    assert row["status"] == "pending" and row["attempts"] == 0
    # Nothing is tried while email is not configured.
    async with get_sessionmaker()() as session:
        assert await feedback_delivery.process_pending_feedback(session) == 0
    assert brevo.routes["brevo"].call_count == 0


async def test_a_brevo_refusal_is_kept_and_accepted(
    client: httpx.AsyncClient,
    brevo: respx.Router,
    monkeypatch: pytest.MonkeyPatch,
    db: asyncpg.Connection,
) -> None:
    _key(monkeypatch, "BREVO_API_KEY", "key")
    brevo.routes["brevo"].mock(
        return_value=httpx.Response(401, json={"code": "unauthorized"})
    )
    res = await client.post(URL, json={"type": "bug", "subject": "s", "message": "m"})
    assert res.status_code == 201, res.text
    assert brevo.routes["brevo"].call_count == 1
    (row,) = await _rows(db)
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert "status=401" in row["last_error"]
    assert row["next_attempt_at"] > row["created_at"]


async def test_the_scheduler_retries_until_it_is_sent(
    client: httpx.AsyncClient,
    brevo: respx.Router,
    monkeypatch: pytest.MonkeyPatch,
    db: asyncpg.Connection,
) -> None:
    _key(monkeypatch, "BREVO_API_KEY", "key")
    brevo.routes["brevo"].mock(return_value=httpx.Response(401, json={}))
    await client.post(URL, json={"type": "bug", "subject": "s", "message": "m"})

    # Not due yet: nothing is retried.
    async with get_sessionmaker()() as session:
        assert await feedback_delivery.process_pending_feedback(session) == 0
    assert brevo.routes["brevo"].call_count == 1

    # Due, and Brevo takes it now.
    await db.execute("UPDATE site_feedback SET next_attempt_at = 0")
    brevo.routes["brevo"].mock(
        return_value=httpx.Response(201, json={"messageId": "<m@brevo>"})
    )
    async with get_sessionmaker()() as session:
        assert await feedback_delivery.process_pending_feedback(session) == 1
    assert brevo.routes["brevo"].call_count == 2
    (row,) = await _rows(db)
    assert row["status"] == "emailed" and row["last_error"] is None


async def test_it_gives_up_after_the_last_attempt(
    client: httpx.AsyncClient,
    brevo: respx.Router,
    monkeypatch: pytest.MonkeyPatch,
    db: asyncpg.Connection,
) -> None:
    _key(monkeypatch, "BREVO_API_KEY", "key")
    brevo.routes["brevo"].mock(return_value=httpx.Response(500, json={}))
    await client.post(URL, json={"type": "bug", "subject": "s", "message": "m"})
    await db.execute(
        "UPDATE site_feedback SET next_attempt_at = 0, attempts = $1",
        feedback_delivery.MAX_ATTEMPTS - 1,
    )
    async with get_sessionmaker()() as session:
        assert await feedback_delivery.process_pending_feedback(session) == 0
    (row,) = await _rows(db)
    # Kept, marked failed, and no longer retried.
    assert row["status"] == "failed"
    assert row["attempts"] == feedback_delivery.MAX_ATTEMPTS
    assert row["message"] == "m"


def test_retries_back_off() -> None:
    delays = [feedback_delivery.backoff_ms(n) for n in range(1, 8)]
    assert delays == sorted(delays)
    assert delays[0] == 120_000 and delays[-1] == 30 * 60_000


async def test_feedback_is_limited_per_address_far_below_the_global_limit(
    client: httpx.AsyncClient, brevo: respx.Router
) -> None:
    from api_deejaytools.middleware import PATH_RATE_LIMITS

    allowed = PATH_RATE_LIMITS[URL]
    body = {"type": "bug", "subject": "s", "message": "m"}
    for _ in range(allowed):
        assert (await client.post(URL, json=body)).status_code == 201
    res = await client.post(URL, json=body)
    assert res.status_code == 429
    assert res.json()["error"]["code"] == "too_many_requests"


async def test_a_brevo_timeout_is_kept_and_reported(
    client: httpx.AsyncClient,
    brevo: respx.Router,
    monkeypatch: pytest.MonkeyPatch,
    db: asyncpg.Connection,
) -> None:
    captured: list[BaseException] = []
    monkeypatch.setattr(
        feedback_delivery.sentry_sdk, "capture_exception", captured.append
    )
    _key(monkeypatch, "BREVO_API_KEY", "key")
    brevo.routes["brevo"].mock(side_effect=httpx.ReadTimeout("slow"))
    res = await client.post(URL, json={"type": "bug", "subject": "s", "message": "m"})
    assert res.status_code == 201
    assert len(captured) == 1 and isinstance(captured[0], httpx.ReadTimeout)
    (row,) = await _rows(db)
    assert row["status"] == "pending" and "brevo_unreachable" in row["last_error"]


def test_the_brevo_call_is_bounded() -> None:
    assert 0 < feedback_delivery.BREVO_TIMEOUT_SECONDS <= 30


@pytest.mark.parametrize(
    "body",
    [
        {"type": "bug", "subject": "x", "message": "y", "screenshot": "not-a-data-url"},
        {
            "type": "bug",
            "subject": "x",
            "message": "y",
            "screenshot": "data:image/gif;base64,R0lG",
        },
        {"type": "praise", "subject": "x", "message": "y"},
        {"subject": "x", "message": "y"},
        {"type": "bug", "subject": "", "message": "y"},
        {"type": "bug", "subject": "x" * 256, "message": "y"},
        {"type": "bug", "subject": "x", "message": ""},
        {"type": "bug", "subject": "x", "message": "y" * 20_001},
        # JavaScript lengths: an emoji is two code units.
        {"type": "bug", "subject": "😀" * 128, "message": "y"},
        {"type": "bug", "subject": "x", "message": "y", "contactName": "n" * 256},
        {"type": "bug", "subject": "x", "message": "y", "contactName": 5},
        {"type": "bug", "subject": "x", "message": "y", "contactEmail": "nope"},
        {"type": "bug", "subject": "x", "message": "y", "screenshot": None},
        {"type": "bug", "subject": None, "message": "y"},
        {
            "type": "bug",
            "subject": "x",
            "message": "y",
            "screenshot": "data:image/png;base64," + "A" * (3 * 1024 * 1024),
        },
    ],
)
async def test_validation(
    client: httpx.AsyncClient,
    brevo: respx.Router,
    monkeypatch: pytest.MonkeyPatch,
    body: dict[str, Any],
) -> None:
    _key(monkeypatch, "BREVO_API_KEY", "key")
    res = await client.post(URL, json=body)
    assert res.status_code == 400, res.text
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"
    assert brevo.routes["brevo"].call_count == 0


async def test_limits_are_inclusive(
    client: httpx.AsyncClient, brevo: respx.Router
) -> None:
    res = await client.post(
        URL,
        json={
            "type": "bug",
            "subject": "x" * 255,
            "message": "y" * 20_000,
            "contactName": "n" * 255,
        },
    )
    assert res.status_code == 201, res.text


async def test_a_token_is_not_needed_or_checked(
    client: httpx.AsyncClient, brevo: respx.Router, db: asyncpg.Connection
) -> None:
    res = await client.post(
        URL,
        json={"type": "bug", "subject": "s", "message": "m"},
        headers={"Authorization": "Bearer not.a.jwt"},
    )
    assert res.status_code == 201
    # A token that does not verify is simply anonymous.
    (row,) = await _rows(db)
    assert row["user_id"] is None


async def test_a_signed_in_sender_is_recorded(
    client: httpx.AsyncClient,
    brevo: respx.Router,
    monkeypatch: pytest.MonkeyPatch,
    clerk: Clerk,
    db: asyncpg.Connection,
) -> None:
    _key(monkeypatch, "BREVO_API_KEY", "key")
    res = await client.post(
        URL,
        json={"type": "bug", "subject": "s", "message": "m"},
        headers=bearer(clerk.token("user_feedback")),
    )
    assert res.status_code == 201, res.text
    (row,) = await _rows(db)
    assert row["user_id"] == "user_feedback"
    _, payload = _sent(brevo)
    assert "Signed in as: user_feedback\n" in payload["textContent"]
