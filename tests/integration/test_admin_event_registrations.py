"""/v1/admin/events/:id/registrations and /v1/admin/event-registrations/:id."""

from __future__ import annotations

from collections.abc import Callable

import asyncpg
import httpx

from .content_helpers import insert_event


def _url(event_id: str) -> str:
    return f"/v1/admin/events/{event_id}/registrations"


ENTRIES = [
    {
        "division": "Classic (NASDE)",
        "dancer_1": "Ada Lovelace",
        "dancer_2": "Bob Jones",
    },
    {"division": "Teams", "dancer_1": "Future. WOW.", "dancer_2": ""},
]


async def test_requires_a_token_and_the_admin_scope(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    alice = await person("alice")
    event = await insert_event(db)
    assert (await client.get(_url(event))).status_code == 401
    for res in (
        await client.get(_url(event), headers=alice.headers),
        await client.put(_url(event), json={"entries": []}, headers=alice.headers),
        await client.patch(
            "/v1/admin/event-registrations/x", json={}, headers=alice.headers
        ),
    ):
        assert res.status_code == 403
        assert res.json()["error"]["code"] == "FORBIDDEN"


async def test_unknown_event_is_404(
    client: httpx.AsyncClient, person: Callable
) -> None:
    admin = await person("admin", admin=True)
    res = await client.get(_url("nope"), headers=admin.headers)
    assert res.status_code == 404
    res = await client.put(_url("nope"), json={"entries": []}, headers=admin.headers)
    assert res.status_code == 404


async def test_import_lists_in_order_and_blank_dancer_2_is_null(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    event = await insert_event(db)
    assert (await client.get(_url(event), headers=admin.headers)).json()["data"] == []
    res = await client.put(
        _url(event), json={"entries": ENTRIES}, headers=admin.headers
    )
    assert res.status_code == 200
    data = res.json()["data"]
    assert [(r["position"], r["dancer_1"], r["dancer_2"]) for r in data] == [
        (0, "Ada Lovelace", "Bob Jones"),
        (1, "Future. WOW.", None),
    ]
    assert data[0]["handled"] is False
    assert data[0]["linked_label"] is None
    listed = (await client.get(_url(event), headers=admin.headers)).json()["data"]
    assert listed == data


async def test_reimport_keeps_marks_on_unchanged_entries(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    event = await insert_event(db)
    first = (
        await client.put(_url(event), json={"entries": ENTRIES}, headers=admin.headers)
    ).json()["data"]
    ada = first[0]["id"]
    res = await client.patch(
        f"/v1/admin/event-registrations/{ada}",
        json={"linked_label": " Ada L & Bob J ", "handled": True, "note": "emailed"},
        headers=admin.headers,
    )
    assert res.status_code == 200
    assert res.json()["data"]["linked_label"] == "Ada L & Bob J"
    assert res.json()["data"]["handled"] is True

    # Same couple, different case and spacing, now second; the team is gone.
    again = [
        {"division": "Showcase", "dancer_1": "Cy Dee"},
        {
            "division": "classic (nasde)",
            "dancer_1": "ada  lovelace",
            "dancer_2": "BOB JONES",
        },
    ]
    data = (
        await client.put(_url(event), json={"entries": again}, headers=admin.headers)
    ).json()["data"]
    assert [r["dancer_1"] for r in data] == ["Cy Dee", "ada  lovelace"]
    kept = data[1]
    assert kept["id"] == ada
    assert kept["position"] == 1
    assert (kept["linked_label"], kept["handled"], kept["note"]) == (
        "Ada L & Bob J",
        True,
        "emailed",
    )
    assert data[0]["handled"] is False

    cleared = (
        await client.put(_url(event), json={"entries": []}, headers=admin.headers)
    ).json()["data"]
    assert cleared == []


async def test_patch_clears_with_null_and_leaves_unsent_fields(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    event = await insert_event(db)
    row = (
        await client.put(_url(event), json={"entries": ENTRIES}, headers=admin.headers)
    ).json()["data"][0]
    url = f"/v1/admin/event-registrations/{row['id']}"
    await client.patch(
        url, json={"note": "x", "linked_label": "L"}, headers=admin.headers
    )
    res = await client.patch(url, json={"note": None}, headers=admin.headers)
    assert res.json()["data"]["note"] is None
    assert res.json()["data"]["linked_label"] == "L"
    res = await client.patch(url, json={"handled": None}, headers=admin.headers)
    assert res.status_code == 400
    res = await client.patch(
        "/v1/admin/event-registrations/nope", json={}, headers=admin.headers
    )
    assert res.status_code == 404


async def test_rejects_an_entry_without_dancer_1(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    event = await insert_event(db)
    res = await client.put(
        _url(event),
        json={"entries": [{"division": "Classic", "dancer_1": "  "}]},
        headers=admin.headers,
    )
    assert res.status_code == 400


async def test_event_delete_removes_its_list(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    event = await insert_event(db)
    await client.put(_url(event), json={"entries": ENTRIES}, headers=admin.headers)
    res = await client.delete(f"/v1/events/{event}", headers=admin.headers)
    assert res.status_code in (200, 204)
    left = await db.fetchval(
        "SELECT count(*) FROM event_registrations WHERE event_id = $1", event
    )
    assert left == 0
