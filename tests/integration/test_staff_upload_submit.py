"""A staff upload on behalf: the dancer's partners, and submitting the song.

An event's DJ reads one dancer's partner list for the event
(``GET /v1/events/{id}/people/{user_id}/partners``) and uploads a song for
them with ``submit_to_event``, which enters it in the event in the same
request: the dancer as submitter, the DJ as uploader.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import asyncpg
import httpx

from api_deejaytools.services import song_builds

from .content_helpers import insert_song, new_id
from .queue_helpers import insert_partner
from .test_song_uploads import MP3, FakeDrive, fake_drive  # noqa: F401 - fixture

UPLOAD_URL = "/v1/songs/upload/chunk"


async def _event(client: httpx.AsyncClient, admin: Any, name: str) -> str:
    today = date.today()
    res = await client.post(
        "/v1/events",
        json={
            "name": name,
            "start_date": (today - timedelta(days=1)).isoformat(),
            "end_date": (today + timedelta(days=1)).isoformat(),
            "timezone": "UTC",
        },
        headers=admin.headers,
    )
    assert res.status_code == 201, res.text
    event_id: str = res.json()["data"]["id"]
    return event_id


async def _grant(
    client: httpx.AsyncClient, admin: Any, event_id: str, user_id: str, role: str
) -> None:
    res = await client.put(
        f"/v1/events/{event_id}/staff/{user_id}/{role}", headers=admin.headers
    )
    assert res.status_code == 200, res.text


async def _connect(
    db: asyncpg.Connection, event_id: str, user_id: str, partner_id: str
) -> str:
    """Put the dancer on the event: a Showcase song of theirs, submitted."""
    song_id = await insert_song(db, user_id, partner_id=partner_id, division="Showcase")
    await db.execute(
        "INSERT INTO event_song_submissions (id, event_id, song_id,"
        " submitted_by_user_id, created_at) VALUES ($1, $2, $3, $4, 1)",
        new_id("sub"),
        event_id,
        song_id,
        user_id,
    )
    return song_id


async def _world(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> dict[str, Any]:
    admin = await person("admin", admin=True)
    dj = await person("dj")
    dancer = await person("dancer")
    event = await _event(client, admin, "Spring Swing")
    other_event = await _event(client, admin, "Fall Swing")
    await _grant(client, admin, event, dj.id, "dj")
    partner = await insert_partner(db, dancer.id, first="Bea", last="Lead")
    team = await insert_partner(db, dancer.id, first="Crew", last="", kind="team")
    await _connect(db, event, dancer.id, partner)
    return {
        "admin": admin,
        "dj": dj,
        "dancer": dancer,
        "event": event,
        "other_event": other_event,
        "partner": partner,
        "team": team,
    }


def _url(event_id: str, user_id: str) -> str:
    return f"/v1/events/{event_id}/people/{user_id}/partners"


async def _upload(client: httpx.AsyncClient, who: Any, **extra: str) -> httpx.Response:
    form = {
        "upload_id": str(uuid.uuid4()),
        "chunk_index": "0",
        "total_chunks": "1",
        "original_filename": "routine.mp3",
        "division": "Classic",
        **extra,
    }
    return await client.post(
        UPLOAD_URL, data=form, files={"chunk": ("chunk", MP3)}, headers=who.headers
    )


# --- the partner read ---------------------------------------------------------


async def test_the_events_staff_read_a_dancers_partners(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    w = await _world(client, person, db)
    res = await client.get(_url(w["event"], w["dancer"].id), headers=w["dj"].headers)
    assert res.status_code == 200, res.text
    # Kind partner only: the team placeholder is not on the list.
    assert [p["id"] for p in res.json()["data"]] == [w["partner"]]
    assert res.json()["data"][0]["display_name"] == "Bea Lead"


async def test_the_partner_read_is_refused_to_anyone_else(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    w = await _world(client, person, db)
    stranger = await person("stranger")
    res = await client.get(_url(w["event"], w["dancer"].id), headers=stranger.headers)
    assert res.status_code == 403
    # The DJ's grant is on this event, not the other one.
    res = await client.get(
        _url(w["other_event"], w["dancer"].id), headers=w["dj"].headers
    )
    assert res.status_code == 403


async def test_a_dancer_not_at_the_event_is_not_found(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    w = await _world(client, person, db)
    elsewhere = await person("elsewhere")
    await insert_partner(db, elsewhere.id)
    res = await client.get(_url(w["event"], elsewhere.id), headers=w["dj"].headers)
    assert res.status_code == 404
    assert res.json()["error"]["message"] == "Person not found at this event"


# --- upload and submit --------------------------------------------------------


async def test_an_upload_on_behalf_is_submitted_to_the_event(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    fake_drive: FakeDrive,  # noqa: F811 - the fixture
) -> None:
    w = await _world(client, person, db)
    res = await _upload(
        client,
        w["dj"],
        on_behalf_of_user_id=w["dancer"].id,
        event_id=w["event"],
        partner_id=w["partner"],
        submit_to_event="true",
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    song, submission = data["song"], data["submission"]
    assert song["user_id"] == w["dancer"].id
    assert song["partner_id"] == w["partner"]
    assert submission["event_id"] == w["event"]
    assert submission["song_id"] == song["id"]
    assert submission["division"] == "Classic"
    assert submission["round"] == "prelims_and_finals"

    row = await db.fetchrow(
        "SELECT submitted_by_user_id, division, round FROM event_song_submissions "
        "WHERE id = $1",
        submission["id"],
    )
    assert dict(row) == {
        "submitted_by_user_id": w["dancer"].id,
        "division": None,
        "round": None,
    }
    uploader = await db.fetchval(
        "SELECT uploaded_by_user_id FROM song_uploads WHERE song_id = $1",
        song["id"],
    )
    await song_builds.wait_for_builds()
    assert uploader == w["dj"].id


async def test_a_taken_slot_is_409_and_nothing_is_saved(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    fake_drive: FakeDrive,  # noqa: F811 - the fixture
) -> None:
    w = await _world(client, person, db)
    first = await _upload(
        client,
        w["dj"],
        on_behalf_of_user_id=w["dancer"].id,
        event_id=w["event"],
        partner_id=w["partner"],
        submit_to_event="true",
    )
    assert first.status_code == 200, first.text
    await song_builds.wait_for_builds()
    songs = await db.fetchval("SELECT count(*) FROM songs")
    submissions = await db.fetchval("SELECT count(*) FROM event_song_submissions")

    again = await _upload(
        client,
        w["dj"],
        on_behalf_of_user_id=w["dancer"].id,
        event_id=w["event"],
        partner_id=w["partner"],
        submit_to_event="true",
    )
    assert again.status_code == 409
    assert again.json()["error"] == {
        "code": "ENTITY_SLOT_TAKEN",
        "message": (
            "This entity already has a song submitted for Classic. "
            "Remove it before adding another."
        ),
    }
    assert await db.fetchval("SELECT count(*) FROM songs") == songs
    assert (
        await db.fetchval("SELECT count(*) FROM event_song_submissions") == submissions
    )


async def test_a_partner_not_on_the_dancers_list_is_refused(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    fake_drive: FakeDrive,  # noqa: F811 - the fixture
) -> None:
    w = await _world(client, person, db)
    someone_elses = await insert_partner(db, w["dj"].id, first="Not", last="Theirs")
    for partner_id, message in (
        (someone_elses, "Partner not found or does not belong to you"),
        (w["team"], "Partner is not on the dancer's partner list"),
    ):
        res = await _upload(
            client,
            w["dj"],
            on_behalf_of_user_id=w["dancer"].id,
            event_id=w["event"],
            partner_id=partner_id,
            submit_to_event="true",
        )
        assert res.status_code == 400, res.text
        assert res.json()["error"]["message"] == message
    assert (
        await db.fetchval(
            "SELECT count(*) FROM songs WHERE user_id = $1", w["dancer"].id
        )
        == 1
    )  # the Showcase song _connect made


async def test_submit_to_event_needs_the_dancer_and_the_event(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    fake_drive: FakeDrive,  # noqa: F811 - the fixture
) -> None:
    w = await _world(client, person, db)
    res = await _upload(
        client, w["dj"], on_behalf_of_user_id=w["dancer"].id, submit_to_event="true"
    )
    assert res.status_code == 400
    assert res.json()["error"]["message"] == (
        "submit_to_event needs on_behalf_of_user_id and event_id"
    )


async def test_an_upload_without_submit_to_event_is_not_submitted(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    fake_drive: FakeDrive,  # noqa: F811 - the fixture
) -> None:
    w = await _world(client, person, db)
    res = await _upload(
        client,
        w["dj"],
        on_behalf_of_user_id=w["dancer"].id,
        event_id=w["event"],
        partner_id=w["partner"],
    )
    assert res.status_code == 200, res.text
    assert "submission" not in res.json()["data"]
    await song_builds.wait_for_builds()
    assert await db.fetchval("SELECT count(*) FROM event_song_submissions") == 1
