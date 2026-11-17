"""The activity feed (services/change_feed.py, services/activity_feed.py):
each person's minute as one message, in words, over the real app and database.

Posts are captured at the library's transport, as in test_notifications.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import asyncpg
import httpx
import pytest

from api_deejaytools.clock import now_ms
from api_deejaytools.database import get_sessionmaker
from api_deejaytools.services import activity_feed, change_feed

from .test_events import _create as create_event
from .test_notifications import (
    Posts,
    _named,
    discord_posts,  # noqa: F401 - fixture
)
from .test_song_uploads import (
    FakeDrive,
    _partner,
    _upload,
    fake_drive,  # noqa: F401 - fixture
)


async def _by_title(posts: Posts) -> dict[str, list[str]]:
    return {e["title"]: e["description"].split("\n") for e in await posts.changes()}


async def _submit(
    client: httpx.AsyncClient, who: Any, event_id: str, song_id: str
) -> str:
    res = await client.post(
        "/v1/event-song-submissions",
        json={"event_id": event_id, "song_id": song_id},
        headers=who.headers,
    )
    assert res.status_code == 201, res.text
    return res.json()["data"]["id"]


# --- what a minute says ---------------------------------------------------------


async def test_a_minute_of_signing_up_is_one_message(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    fake_drive: FakeDrive,  # noqa: F811
    discord_posts: Posts,  # noqa: F811
) -> None:
    """The channel's own example: names, the event, and "song added" joined
    to the submission that followed it, whichever worker took each request."""
    admin = await person("admin", admin=True)
    event = await create_event(client, admin, name="The Open")
    greg = await person("greg")
    await _named(db, greg, "Gregory", "Rollinscott")

    made = await client.post(
        "/v1/managed-partnerships",
        json={
            "leader_first_name": "leigh",
            "leader_last_name": "yu",
            "follower_first_name": "lemery",
            "follower_last_name": "rollinscott",
        },
        headers=greg.headers,
    )
    assert made.status_code == 201, made.text
    team = await client.post(
        "/v1/teams", json={"identifier": "Future WOW"}, headers=greg.headers
    )
    assert team.status_code == 201, team.text
    named = await client.patch(
        "/v1/auth/me",
        json={"firstName": "Gregory", "lastName": "Rollinscott"},
        headers=greg.headers,
    )
    assert named.status_code == 200, named.text
    song = await _upload(client, greg, routine_name="What's up Danger")
    await _submit(client, greg, event["id"], song["id"])

    messages = await _by_title(discord_posts)
    assert messages["Gregory Rollinscott"] == [
        "🤝 created managed partnership Leigh Yu & Lemery Rollinscott",
        "👥 created team 'Future WOW'",
        "🎵 added 'What's up Danger' (Classic) to The Open · "
        "[Drive file](https://drive.google.com/file/d/file-1/view)",
    ]
    assert messages["Admin"] == ["📅 created event The Open (2026-05-01 – 2026-05-03)"]


async def test_a_name_change_is_said(
    client: httpx.AsyncClient,
    person: Callable,
    discord_posts: Posts,  # noqa: F811
) -> None:
    jane = await person("jane")
    res = await client.patch(
        "/v1/auth/me",
        json={"firstName": "Janet", "lastName": "Doe"},
        headers=jane.headers,
    )
    assert res.status_code == 200, res.text
    assert await discord_posts.lines() == ["✏️ updated their name"]


async def test_a_song_delete_says_the_song_and_its_cascade(
    client: httpx.AsyncClient,
    person: Callable,
    fake_drive: FakeDrive,  # noqa: F811
    discord_posts: Posts,  # noqa: F811
) -> None:
    admin = await person("admin", admin=True)
    event = await create_event(client, admin)
    pj = await person("pj")
    first = await _upload(
        client, pj, routine_name="Do it it with Soul", division="Showcase"
    )
    await _submit(client, pj, event["id"], first["id"])
    await discord_posts.changes()  # that minute posts on its own

    gone = await client.delete(f"/v1/songs/{first['id']}", headers=pj.headers)
    assert gone.status_code == 204, gone.text
    messages = await _by_title(discord_posts)
    # Named by title, never by id, and no bulk statements for the cascade.
    assert messages["Pj"] == [
        "🗑️ deleted 'Do it it with Soul' (Showcase), withdrawn from 1 event"
    ]


async def test_a_withdrawn_submission_and_an_edit(
    client: httpx.AsyncClient,
    person: Callable,
    fake_drive: FakeDrive,  # noqa: F811
    discord_posts: Posts,  # noqa: F811
) -> None:
    admin = await person("admin", admin=True)
    event = await create_event(client, admin)
    jane = await person("jane")
    song = await _upload(client, jane, routine_name="Ballad")
    submission = await _submit(client, jane, event["id"], song["id"])
    await discord_posts.changes()

    edited = await client.patch(
        f"/v1/songs/{song['id']}",
        json={"routine_name": "Ballad II", "division": "Showcase"},
        headers=jane.headers,
    )
    assert edited.status_code == 200, edited.text
    withdrawn = await client.delete(
        f"/v1/event-song-submissions/{submission}", headers=jane.headers
    )
    assert withdrawn.status_code == 204, withdrawn.text
    assert (await _by_title(discord_posts))["Jane"] == [
        "✏️ edited 'Ballad II' (division, routine name)",
        "↩️ withdrew 'Ballad II' from Swing Fling (Showcase)",
    ]


async def test_an_admin_move_names_both_divisions(
    client: httpx.AsyncClient,
    person: Callable,
    fake_drive: FakeDrive,  # noqa: F811
    discord_posts: Posts,  # noqa: F811
) -> None:
    admin = await person("admin", admin=True)
    event = await create_event(client, admin)
    jane = await person("jane")
    song = await _upload(client, jane, routine_name="Ballad")
    submission = await _submit(client, jane, event["id"], song["id"])
    moved = await client.patch(
        f"/v1/admin/event-song-submissions/{submission}",
        json={"division": "Showcase"},
        headers=admin.headers,
    )
    assert moved.status_code == 200, moved.text
    assert (await _by_title(discord_posts))["Admin"][-1] == (
        "🔀 moved 'Ballad' at Swing Fling from Classic to Showcase"
    )


async def test_partners_teams_and_partnerships_by_name(
    client: httpx.AsyncClient,
    person: Callable,
    discord_posts: Posts,  # noqa: F811
) -> None:
    jane = await person("jane")
    partner = await _partner(client, jane)
    edited = await client.patch(
        f"/v1/partners/{partner}", json={"last_name": "Person"}, headers=jane.headers
    )
    assert edited.status_code == 200, edited.text
    removed = await client.delete(f"/v1/partners/{partner}", headers=jane.headers)
    assert removed.status_code == 204, removed.text

    team = (
        await client.post("/v1/teams", json={"identifier": "One"}, headers=jane.headers)
    ).json()["data"]["id"]
    await client.patch(
        f"/v1/teams/{team}", json={"identifier": "Two"}, headers=jane.headers
    )
    await client.delete(f"/v1/teams/{team}", headers=jane.headers)

    names = {
        "leader_first_name": "Lee",
        "leader_last_name": "Lead",
        "follower_first_name": "Fay",
        "follower_last_name": "Follow",
    }
    mp = (
        await client.post("/v1/managed-partnerships", json=names, headers=jane.headers)
    ).json()["data"]["id"]
    renamed = await client.patch(
        f"/v1/managed-partnerships/{mp}",
        json={**names, "follower_first_name": "Flo"},
        headers=jane.headers,
    )
    assert renamed.status_code == 200, renamed.text
    deleted = await client.delete(
        f"/v1/managed-partnerships/{mp}", headers=jane.headers
    )
    assert deleted.status_code == 204, deleted.text

    assert await discord_posts.lines() == [
        "👤 added partner Pat Partner",
        "✏️ edited partner Pat Person (last name)",
        "🗑️ removed partner Pat Person",
        "👥 created team 'One'",
        "✏️ renamed team 'One' to 'Two'",
        "🗑️ deleted team 'Two'",
        "🤝 created managed partnership Lee Lead & Fay Follow",
        "✏️ renamed managed partnership Lee Lead & Fay Follow to Lee Lead & Flo Follow",
        "🗑️ deleted managed partnership Lee Lead & Flo Follow",
    ]


async def test_a_registration_list_and_its_marks(
    client: httpx.AsyncClient,
    person: Callable,
    discord_posts: Posts,  # noqa: F811
) -> None:
    admin = await person("admin", admin=True)
    event = await create_event(client, admin)
    await discord_posts.changes()
    entries = [
        {"division": "Classic", "dancer_1": f"Lead {i}", "dancer_2": f"Follow {i}"}
        for i in range(5)
    ]
    imported = await client.put(
        f"/v1/admin/events/{event['id']}/registrations",
        json={"entries": entries},
        headers=admin.headers,
    )
    assert imported.status_code == 200, imported.text
    first = imported.json()["data"][0]["id"]
    marked = await client.patch(
        f"/v1/events/{event['id']}/registrations/{first}",
        json={"handled": True},
        headers=admin.headers,
    )
    assert marked.status_code == 200, marked.text
    removed = await client.delete(
        f"/v1/events/{event['id']}/registrations/{first}", headers=admin.headers
    )
    assert removed.status_code == 200, removed.text
    # Re-imported without the second entry: the list says what changed.
    again = await client.put(
        f"/v1/admin/events/{event['id']}/registrations",
        json={"entries": entries[2:]},
        headers=admin.headers,
    )
    assert again.status_code == 200, again.text

    assert (await discord_posts.lines())[1:] == [
        "📋 imported Swing Fling's registration list: 5 added, 0 removed, 0 unchanged",
        "📋 marked Lead 0 & Follow 0 (Classic) at Swing Fling: handled",
        "📋 removed Lead 0 & Follow 0 (Classic) from Swing Fling's registration list",
        "📋 imported Swing Fling's registration list: 0 added, 1 removed, 3 unchanged",
    ]


async def test_staff_and_admin_roles(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    discord_posts: Posts,  # noqa: F811
) -> None:
    admin = await person("admin", admin=True)
    event = await create_event(client, admin)
    dee = await person("dee")
    await _named(db, dee, "Dee", "Jay")
    granted = await client.put(
        f"/v1/events/{event['id']}/staff/{dee.id}/dj", headers=admin.headers
    )
    assert granted.status_code == 200, granted.text
    revoked = await client.delete(
        f"/v1/events/{event['id']}/staff/{dee.id}/dj", headers=admin.headers
    )
    assert revoked.status_code == 200, revoked.text
    promoted = await client.patch(
        f"/v1/admin/users/{dee.id}/role", json={"role": "admin"}, headers=admin.headers
    )
    assert promoted.status_code == 200, promoted.text

    assert (await discord_posts.lines())[1:] == [
        "🎟️ made Dee Jay a DJ at Swing Fling",
        "🎟️ removed Dee Jay as a DJ at Swing Fling",
        "🛡️ made Dee Jay an admin",
    ]


async def test_typed_names_are_escaped(
    client: httpx.AsyncClient,
    person: Callable,
    discord_posts: Posts,  # noqa: F811
) -> None:
    jane = await person("jane")
    res = await client.post(
        "/v1/partners",
        json={
            "first_name": "**Loud**",
            "last_name": "@everyone",
            "partner_role": "leader",
        },
        headers=jane.headers,
    )
    assert res.status_code == 201, res.text
    (change,) = await discord_posts.changes()
    assert change["description"] == "👤 added partner \\*\\*Loud\\*\\* @everyone"
    (post,) = discord_posts.sent
    assert post["json"]["allowed_mentions"] == {"parse": []}


# --- when a minute is posted ----------------------------------------------------


async def test_nothing_posts_until_the_person_is_quiet(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    discord_posts: Posts,  # noqa: F811
) -> None:
    jane = await person("jane")
    await client.post("/v1/teams", json={"identifier": "One"}, headers=jane.headers)
    maker = get_sessionmaker()
    assert await activity_feed.post_due(maker, now=now_ms()) == 0
    assert (
        await db.fetchval("SELECT count(*) FROM activity_feed WHERE posted_at IS NULL")
        == 1
    )
    later = now_ms() + change_feed.QUIET_MS + 1
    assert await activity_feed.post_due(maker, now=later) == 1
    # Claimed once: a second pass, or another worker, posts nothing more.
    assert await activity_feed.post_due(maker, now=later) == 0
    assert len(await discord_posts.changes()) == 1


async def test_someone_busy_is_posted_after_the_longest_wait(
    db: asyncpg.Connection,
    person: Callable,
    discord_posts: Posts,  # noqa: F811
) -> None:
    jane = await person("jane")
    now = now_ms()
    for created_at in (now - change_feed.MAX_WAIT_MS - 1, now - 1000):
        await db.execute(
            "INSERT INTO activity_feed (actor_id, table_name, op, facts, created_at)"
            " VALUES ($1, 'teams', 'created', '{\"identifier\": \"T\"}', $2)",
            jane.id,
            created_at,
        )
    assert await activity_feed.post_due(get_sessionmaker(), now=now) == 1


async def test_posted_rows_are_pruned_after_a_week(
    db: asyncpg.Connection,
    discord_posts: Posts,  # noqa: F811
) -> None:
    now = now_ms()
    await db.execute(
        "INSERT INTO activity_feed (actor_id, table_name, op, created_at, posted_at)"
        " VALUES ('old', 'teams', 'created', 1, $1), ('recent', 'teams', 'created', 1, $2)",
        now - change_feed.KEEP_MS - 1,
        now - 1000,
    )
    await activity_feed.post_due(get_sessionmaker(), now=now)
    assert await db.fetchval("SELECT array_agg(actor_id) FROM activity_feed") == [
        "recent"
    ]


async def test_a_rolled_back_request_leaves_no_row(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
) -> None:
    jane = await person("jane")
    await client.post("/v1/teams", json={"identifier": "One"}, headers=jane.headers)
    two = await client.post(
        "/v1/teams", json={"identifier": "Two"}, headers=jane.headers
    )
    clash = await client.patch(
        f"/v1/teams/{two.json()['data']['id']}",
        json={"identifier": "One"},
        headers=jane.headers,
    )
    assert clash.status_code == 409
    assert await db.fetchval("SELECT count(*) FROM activity_feed") == 2


async def test_reads_and_sign_ins_write_no_rows(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
) -> None:
    jane = await person("jane")  # a sign-in
    assert (await client.get("/v1/teams", headers=jane.headers)).status_code == 200
    assert await db.fetchval("SELECT count(*) FROM activity_feed") == 0


# --- the capture and the claim, directly ----------------------------------------


async def test_orm_deletes_are_kept_and_records_need_a_capture(
    db: asyncpg.Connection, person: Callable
) -> None:
    from types import SimpleNamespace

    from api_deejaytools.models import Team

    jane = await person("jane")
    maker = get_sessionmaker()
    async with maker() as s:
        # Outside a request: nothing is kept, recorded or captured.
        change_feed.record(s, "teams", change_feed.DELETED, {"identifier": "X"})
        s.add(
            Team(id="t1", user_id=jane.id, identifier="One", created_at=1, updated_at=1)
        )
        await s.commit()
    assert await db.fetchval("SELECT count(*) FROM activity_feed") == 0

    # A request whose caller is not known yet keeps nothing either.
    token = change_feed.begin(SimpleNamespace(state=SimpleNamespace()))
    try:
        async with maker() as s:
            change_feed.record(s, "teams", change_feed.DELETED, {"identifier": "X"})
            await s.commit()
    finally:
        change_feed.end(token)
    assert await db.fetchval("SELECT count(*) FROM activity_feed") == 0

    token = change_feed.begin(SimpleNamespace(state=SimpleNamespace(caller=jane.id)))
    try:
        async with maker() as s:
            team = await s.get(Team, "t1")
            await s.delete(team)
            await s.commit()
    finally:
        change_feed.end(token)
    row = await db.fetchrow("SELECT actor_id, table_name, op, facts FROM activity_feed")
    assert (row["actor_id"], row["table_name"], row["op"]) == (
        jane.id,
        "teams",
        "deleted",
    )
    assert '"identifier": "One"' in row["facts"]


async def test_another_worker_claiming_gets_nothing(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    database: str,
) -> None:
    jane = await person("jane")
    await client.post("/v1/teams", json={"identifier": "One"}, headers=jane.headers)
    other = await asyncpg.connect(database)
    try:
        await other.execute("BEGIN")
        await other.execute("SELECT pg_advisory_xact_lock($1)", change_feed._LOCK_KEY)
        later = now_ms() + change_feed.QUIET_MS + 1
        assert await change_feed.claim_due(get_sessionmaker(), now=later) == {}
        await other.execute("ROLLBACK")
    finally:
        await other.close()
    assert (
        await db.fetchval("SELECT count(*) FROM activity_feed WHERE posted_at IS NULL")
        == 1
    )


async def test_one_broken_message_does_not_hold_up_the_rest(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    discord_posts: Posts,  # noqa: F811
) -> None:
    jane = await person("jane")
    ada = await person("ada")
    await client.post("/v1/teams", json={"identifier": "One"}, headers=jane.headers)
    await client.post("/v1/teams", json={"identifier": "Two"}, headers=ada.headers)
    real = activity_feed.message_for

    async def broken(actor_id: str, entries: Any, look: Any) -> Any:
        if actor_id == jane.id:
            raise RuntimeError("render bug")
        return await real(actor_id, entries, look)

    monkeypatch.setattr(activity_feed, "message_for", broken)
    (only,) = await discord_posts.changes()
    assert only["title"] == "Ada"
