"""/v1/feedback: public site feedback, emailed through Brevo when configured.

Brevo is never called for real (TEST-007): every test runs under a respx
mock that refuses unmocked hosts, and the tests that send assert the exact
request made.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from api_deejaytools.config import get_settings

URL = "/v1/feedback"
BREVO = "https://api.brevo.com/v3/smtp/email"
RULE = "═" * 31
ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")


@pytest.fixture
def brevo(monkeypatch: pytest.MonkeyPatch) -> Iterator[respx.Router]:
    """No Brevo key by default; Brevo's endpoint mocked, nothing else reachable."""
    settings = get_settings()
    monkeypatch.setattr(settings, "DEEJAYTOOLS_BREVO_API_KEY", None)
    monkeypatch.setattr(settings, "BREVO_API_KEY", None)
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
    assert res.json() == {"data": None, "meta": {"version": "v1"}}
    assert brevo.routes["brevo"].call_count == 0


async def test_with_a_key_it_emails_the_feedback(
    client: httpx.AsyncClient, brevo: respx.Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    _key(monkeypatch, "DEEJAYTOOLS_BREVO_API_KEY", "key-new")
    _key(monkeypatch, "BREVO_API_KEY", "key-legacy")
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
    assert res.json() == {"data": None, "meta": {"version": "v1"}}

    request, payload = _sent(brevo)
    assert request.method == "POST"
    assert request.headers["api-key"] == "key-new"
    assert request.headers["content-type"] == "application/json"
    text = payload.pop("textContent")
    assert payload == {
        "sender": {"name": "DeejayTools Feedback", "email": "kaiano@kaianolevine.com"},
        "to": [{"email": "kaiano.levine@gmail.com"}],
        "subject": "[DeejayTools Feedback] feature: Dark mode",
    }
    submitted = ISO.search(text)
    assert submitted is not None
    assert text == (
        f"{RULE}\nFEEDBACK DETAILS\n{RULE}\n"
        "Type:    feature\n"
        "Subject: Dark mode\n"
        "\nPlease\nadd it.\n"
        f"\n{RULE}\nCONTACT\n{RULE}\n"
        "Name:  Ada\n"
        "Email: ada@example.test\n"
        f"\n{RULE}\nMETADATA\n{RULE}\n"
        f"Submitted: {submitted.group(0)}\n"
        f"{RULE}"
    )


async def test_legacy_key_and_blank_contact_fields(
    client: httpx.AsyncClient, brevo: respx.Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An empty prefixed key falls through to the legacy one, as `||` does.
    _key(monkeypatch, "DEEJAYTOOLS_BREVO_API_KEY", "")
    _key(monkeypatch, "BREVO_API_KEY", "key-legacy")
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
    assert request.headers["api-key"] == "key-legacy"
    assert "Name:  Not provided\nEmail: Not provided\n" in payload["textContent"]
    assert "attachment" not in payload


@pytest.mark.parametrize(
    ("screenshot", "name", "content"),
    [
        ("data:image/png;base64,iVBORw0KGgo=", "screenshot.png", "iVBORw0KGgo="),
        ("data:image/jpeg;base64,/9j/4AAQ", "screenshot.jpg", "/9j/4AAQ"),
        # The prefix check ignores case; the JPEG check does not, as in Node.
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
    _key(monkeypatch, "DEEJAYTOOLS_BREVO_API_KEY", "key")
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
    _key(monkeypatch, "DEEJAYTOOLS_BREVO_API_KEY", "key")
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


async def test_brevo_failure_is_502(
    client: httpx.AsyncClient, brevo: respx.Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    _key(monkeypatch, "DEEJAYTOOLS_BREVO_API_KEY", "key")
    brevo.routes["brevo"].mock(
        return_value=httpx.Response(401, json={"code": "unauthorized"})
    )
    res = await client.post(URL, json={"type": "bug", "subject": "s", "message": "m"})
    assert res.status_code == 502
    assert res.json() == {
        "error": {
            "code": "EMAIL_FAILED",
            "message": "Failed to send email. Please try again.",
        }
    }
    assert brevo.routes["brevo"].call_count == 1


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
    _key(monkeypatch, "DEEJAYTOOLS_BREVO_API_KEY", "key")
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
    client: httpx.AsyncClient, brevo: respx.Router
) -> None:
    res = await client.post(
        URL,
        json={"type": "bug", "subject": "s", "message": "m"},
        headers={"Authorization": "Bearer not.a.jwt"},
    )
    assert res.status_code == 201
