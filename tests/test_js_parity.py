"""Edge cases where Python's defaults differ from JavaScript's, answered as
deejaytools-api answers them (ADR-009): body parsing and middleware order,
regex anchors and digits, non-finite numbers, trim(), Intl time zones and
localeCompare."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from api_deejaytools.domain import canonical_timezone, locale_key
from api_deejaytools.zod_coerce import js_trim, js_words

PAT = {"first_name": "Pat", "last_name": "Partner", "partner_role": "follower"}
EVENT = {"name": "Swing Fling", "start_date": "2026-05-01", "end_date": "2026-05-03"}
JSON = {"Content-Type": "application/json"}


# --- body parsing (hono's zValidator("json")) -------------------------------------


async def test_malformed_json_without_a_token_is_401(client: httpx.AsyncClient) -> None:
    res = await client.post("/v1/partners", content=b"{nope", headers=JSON)
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHORIZED"


async def test_malformed_json_with_a_token_is_500(
    client: httpx.AsyncClient, person: Callable
) -> None:
    alice = await person("alice")
    for raw in (b"{nope", b"", b'{"first_name": NaN}'):
        res = await client.post(
            "/v1/partners", content=raw, headers={**alice.headers, **JSON}
        )
        assert res.status_code == 500, raw
        assert res.json() == {
            "error": {"code": "INTERNAL", "message": "Malformed JSON in request body"}
        }


async def test_sync_validates_its_body_before_the_token(
    client: httpx.AsyncClient,
) -> None:
    # /sync checks the token in its handler, after the validator.
    res = await client.post("/v1/auth/sync", content=b"{nope", headers=JSON)
    assert res.status_code == 500
    res = await client.post("/v1/auth/sync", json={"email": 5})
    assert res.status_code == 400


async def test_a_body_without_a_json_content_type_is_empty(
    client: httpx.AsyncClient, person: Callable
) -> None:
    alice = await person("alice")
    created = await client.post("/v1/partners", json=PAT, headers=alice.headers)
    partner = created.json()["data"]
    # No body at all: {} for an all-optional PATCH, so nothing changes.
    res = await client.patch(f"/v1/partners/{partner['id']}", headers=alice.headers)
    assert res.status_code == 200, res.text
    assert res.json()["data"]["first_name"] == "Pat"
    # A JSON body sent as text/plain is ignored the same way.
    res = await client.patch(
        f"/v1/partners/{partner['id']}",
        content=b'{"first_name": "Sam"}',
        headers={**alice.headers, "Content-Type": "text/plain"},
    )
    assert res.json()["data"]["first_name"] == "Pat"
    # And a required body is then missing its fields: 400, not 500.
    res = await client.post("/v1/partners", headers=alice.headers)
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_json_content_type_variants_are_parsed(
    client: httpx.AsyncClient, person: Callable
) -> None:
    alice = await person("alice")
    res = await client.post(
        "/v1/partners",
        content=b'\xef\xbb\xbf{"first_name":"Pat","last_name":"P","partner_role":"leader"}',
        headers={**alice.headers, "Content-Type": "application/json; charset=utf-8"},
    )
    assert res.status_code == 201, res.text


def test_request_bodies_are_documented() -> None:
    from api_deejaytools.main import app

    schema = app.openapi()
    bodies = [
        op
        for path in schema["paths"].values()
        for op in path.values()
        if "requestBody" in op
    ]
    assert len(bodies) == 27  # every zValidator("json") route in deejaytools-api
    patch = schema["paths"]["/v1/partners/{id}"]["patch"]["requestBody"]
    assert patch["required"] is False  # all optional: an empty body is fine


# --- regex anchors and digits ----------------------------------------------------


async def test_trailing_newline_and_unicode_digits_are_refused(
    client: httpx.AsyncClient, person: Callable
) -> None:
    alice = await person("alice")
    admin = await person("root", admin=True)
    res = await client.post(
        "/v1/partners", json={**PAT, "email": "p@q.com\n"}, headers=alice.headers
    )
    assert res.status_code == 400
    for bad in (
        {"start_date": "2026-05-01\n"},
        {"end_date": "٢٠٢٦-05-03"},  # Arabic-Indic digits
        {"season_year": "2027\n"},
    ):
        res = await client.post(
            "/v1/events", json={**EVENT, **bad}, headers=admin.headers
        )
        assert res.status_code == 400, bad


# --- non-finite numbers ----------------------------------------------------------


async def test_infinite_number_is_a_400(
    client: httpx.AsyncClient, person: Callable
) -> None:
    admin = await person("root", admin=True)
    res = await client.post(
        "/v1/sessions",
        content=b'{"name":"S","checkin_opens_at":1e400,'
        b'"floor_trial_starts_at":1,"floor_trial_ends_at":2}',
        headers={**admin.headers, **JSON},
    )
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"


# --- repeated query keys ---------------------------------------------------------


async def test_repeated_event_id_on_sessions_list_is_refused(
    client: httpx.AsyncClient,
) -> None:
    res = await client.get("/v1/sessions?event_id=a&event_id=b")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"
    assert (await client.get("/v1/sessions?event_id=a")).status_code == 200


# --- trim() ----------------------------------------------------------------------


def test_js_trim_uses_javascripts_whitespace() -> None:
    assert js_trim("﻿ a ﻿") == "a"
    assert js_trim("\x1ca\x1c") == "\x1ca\x1c"
    assert js_trim("\x85a") == "\x85a"
    assert js_words("  jt　swing  team ") == ["jt", "swing", "team"]
    assert js_words(" \t ") == []


async def test_bom_only_name_is_empty_after_trim(
    client: httpx.AsyncClient, person: Callable
) -> None:
    alice = await person("alice")
    res = await client.post(
        "/v1/managed-partnerships",
        json={
            "leader_first_name": "﻿",
            "leader_last_name": "L",
            "follower_first_name": "F",
            "follower_last_name": "F",
        },
        headers=alice.headers,
    )
    assert res.status_code == 400


# --- Intl time zones and localeCompare --------------------------------------------


@pytest.mark.parametrize(
    ("name", "valid"),
    [
        ("+05:00", True),
        ("-0330", True),
        ("+05", True),
        ("+5", False),
        ("+24:00", False),
        ("Factory", False),
        ("localtime", False),
        ("posixrules", False),
        ("america/chicago", True),
        ("UTC", True),
    ],
)
def test_timezones_match_intl(name: str, valid: bool) -> None:
    assert (canonical_timezone(name) is not None) is valid


def test_locale_key_matches_node_localecompare() -> None:
    # Node 22's [...].sort((a, b) => a.localeCompare(b)) for these labels.
    node = [
        "_x", "!", "~", "1", "10", "9", "a", "A", "á", "à", "ä", "a 1", "a-b",
        "a~", "a1", "ab", "Ab", "ab ", "ada", "Ada", "Angstrom", "Ångström",
        "b", "B", "Ea", "éa", "eb", "emile", "Emile", "Émile", "Jo & Al",
        "Jo and Al", "Mary Jo", "Mary-Jo", "MaryJo", "o neil", "O'Neil", "Oneil",
    ]  # fmt: skip
    assert sorted(reversed(node), key=locale_key) == node
