"""/v1/admin/events/:id/registrations and /v1/admin/event-registrations/:id."""

from __future__ import annotations

from collections.abc import Callable

import asyncpg
import httpx

from .content_helpers import insert_event, insert_song


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


async def _submit(
    db: asyncpg.Connection, sid: str, event_id: str, song_id: str, user_id: str
) -> None:
    await db.execute(
        "INSERT INTO event_song_submissions (id, event_id, song_id,"
        " submitted_by_user_id, created_at) VALUES ($1, $2, $3, $4, 1)",
        sid,
        event_id,
        song_id,
        user_id,
    )


async def _entity_count(client: httpx.AsyncClient, event_id: str, headers: dict) -> int:
    res = await client.get(f"/v1/events/{event_id}/entities", headers=headers)
    assert res.status_code == 200
    return sum(len(d["entities"]) for d in res.json()["data"])


async def test_scratch_hides_its_submissions_from_the_entry_list_until_unscratched(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    alice = await person("alice")
    event = await insert_event(db)
    other = await insert_event(db, name="Elsewhere")
    song = await insert_song(db, alice.id, division="Classic")
    await _submit(db, "sub1", event, song, alice.id)
    await _submit(db, "sub_other", other, song, alice.id)
    row = (
        await client.put(_url(event), json={"entries": ENTRIES}, headers=admin.headers)
    ).json()["data"][0]
    assert row["scratched"] is False
    assert row["hidden_submission_ids"] == []
    url = f"/v1/admin/event-registrations/{row['id']}"
    assert await _entity_count(client, event, alice.headers) == 1

    res = await client.patch(
        url,
        json={"scratched": True, "hide_submission_ids": ["sub1", "sub_other"]},
        headers=admin.headers,
    )
    assert res.status_code == 200
    # Another event's submission is ignored.
    assert res.json()["data"]["scratched"] is True
    assert res.json()["data"]["hidden_submission_ids"] == ["sub1"]
    assert await _entity_count(client, event, alice.headers) == 0
    assert await _entity_count(client, other, alice.headers) == 1
    # Still submitted: the admin list shows it.
    subs = await client.get(
        f"/v1/admin/event-song-submissions?event_id={event}", headers=admin.headers
    )
    assert [s["id"] for s in subs.json()["data"]] == ["sub1"]
    # A re-import that keeps the entry keeps the scratch and the hide.
    listed = (
        await client.put(_url(event), json={"entries": ENTRIES}, headers=admin.headers)
    ).json()["data"][0]
    assert (listed["scratched"], listed["hidden_submission_ids"]) == (True, ["sub1"])

    res = await client.patch(url, json={"scratched": False}, headers=admin.headers)
    assert (
        res.json()["data"]["scratched"],
        res.json()["data"]["hidden_submission_ids"],
    ) == (
        False,
        [],
    )
    assert await _entity_count(client, event, alice.headers) == 1


async def test_hide_needs_a_scratch_and_dropping_the_entry_unhides(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    alice = await person("alice")
    event = await insert_event(db)
    song = await insert_song(db, alice.id, division="Classic")
    await _submit(db, "sub1", event, song, alice.id)
    row = (
        await client.put(_url(event), json={"entries": ENTRIES}, headers=admin.headers)
    ).json()["data"][0]
    url = f"/v1/admin/event-registrations/{row['id']}"
    res = await client.patch(
        url, json={"hide_submission_ids": ["sub1"]}, headers=admin.headers
    )
    assert res.status_code == 400
    await client.patch(
        url,
        json={"scratched": True, "hide_submission_ids": ["sub1"]},
        headers=admin.headers,
    )
    assert await _entity_count(client, event, alice.headers) == 0
    await client.put(_url(event), json={"entries": ENTRIES[1:]}, headers=admin.headers)
    assert await _entity_count(client, event, alice.headers) == 1
