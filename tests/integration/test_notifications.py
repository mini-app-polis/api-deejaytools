"""Discord notifications (services/notifications.py) over the real app and database.

Every post is captured at the library's transport (``discord.post_webhook``),
so the whole path runs — the middleware, the ORM listeners, the commit hooks,
webhook resolution from Settings — and nothing reaches the network.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

import asyncpg
import httpx
import pytest
from mini_app_polis import activity, discord
from sqlalchemy.ext.asyncio import AsyncSession

from api_deejaytools.config import get_settings
from api_deejaytools.database import get_sessionmaker
from api_deejaytools.errors import ErrorCode, api_error
from api_deejaytools.models import Song
from api_deejaytools.routers import auth as auth_router
from api_deejaytools.routers import teams
from api_deejaytools.services import (
    drive_jobs,
    notifications,
    scheduler,
    session_tick,
    song_builds,
)

from .conftest import Clerk, bearer
from .test_drive_jobs_processor import (
    FakeDrive as FakeJobDrive,
)
from .test_drive_jobs_processor import (
    Sentry,
    add_job,
    seed_submission,
    session,  # noqa: F401 - fixture
)
from .test_song_uploads import (
    MP3,
    FakeDrive,
    _partner,
    _send,
    _upload,
    fake_drive,  # noqa: F401 - fixture
)

ERRORS_URL = "https://discord.test/api/webhooks/1/errors"
ACTIVITY_URL = "https://discord.test/api/webhooks/2/activity"


class Posts:
    """Messages handed to the Discord transport, by channel."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def post_webhook(self, url: str, **kwargs: Any) -> bool:
        self.sent.append({"url": url, **kwargs})
        return True

    async def channel(self, name: str) -> list[dict[str, Any]]:
        """The embeds posted to ``name``, once every delivery has finished."""
        await song_builds.wait_for_builds()
        await activity.wait_for_deliveries()
        return [
            embed
            for post in self.sent
            if post["channel"] == name
            for embed in post["json"]["embeds"]
        ]

    async def changes(self) -> list[dict[str, Any]]:
        return [
            e
            for e in await self.channel(discord.CHANNEL_ACTIVITY)
            if e["title"].endswith("data changed")
        ]

    async def songs_added(self) -> list[dict[str, Any]]:
        return [
            e
            for e in await self.channel(discord.CHANNEL_ACTIVITY)
            if e["title"].endswith("song added")
        ]


@pytest.fixture
def discord_posts(monkeypatch: pytest.MonkeyPatch) -> Posts:
    """Webhooks configured for errors and activity, posts captured."""
    settings = get_settings()
    monkeypatch.setattr(settings, "DISCORD_WEBHOOK_URL_ERRORS", ERRORS_URL)
    monkeypatch.setattr(settings, "DISCORD_WEBHOOK_URL_ACTIVITY", ACTIVITY_URL)
    posts = Posts()
    monkeypatch.setattr(discord, "post_webhook", posts.post_webhook)
    return posts


async def _named(db: asyncpg.Connection, who: Any, first: str, last: str) -> None:
    await db.execute(
        "UPDATE users SET first_name = $2, last_name = $3 WHERE id = $1",
        who.id,
        first,
        last,
    )


# --- song added -------------------------------------------------------------------


async def test_successful_build_announces_one_song_added(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    fake_drive: FakeDrive,  # noqa: F811
    discord_posts: Posts,
) -> None:
    jane = await person("jane")
    await _named(db, jane, "Jane", "Doe")
    partner = await _partner(client, jane)
    song = await _upload(client, jane, routine_name="Ballad", partner_id=partner)

    added = await discord_posts.songs_added()
    assert len(added) == 1
    assert added[0]["description"] == (
        "Jane Doe added 'Ballad' (Classic, with Pat Partner)\n"
        "[Drive file](https://drive.google.com/file/d/file-1/view)"
    )
    assert added[0]["footer"]["text"] == "api-deejaytools · test"
    assert await discord_posts.channel(discord.CHANNEL_ERRORS) == []
    # The upload itself is not in the change feed: the build announced it.
    changes = await discord_posts.changes()
    assert ["`partners` +1"] == [c["description"] for c in changes]
    assert await db.fetchval(
        "SELECT drive_file_id FROM songs WHERE id = $1", song["id"]
    )


async def test_upload_for_names_both_people(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    fake_drive: FakeDrive,  # noqa: F811
    discord_posts: Posts,
) -> None:
    admin = await person("admin", admin=True)
    await _named(db, admin, "Org", "Anizer")
    target = await person("target")
    await _named(db, target, "John", "Smith")
    await _upload(client, admin, routine_name="Show", on_behalf_of_user_id=target.id)
    await _upload(client, target, routine_name="Own")

    added = sorted(
        e["description"].split("\n")[0] for e in await discord_posts.songs_added()
    )
    assert added == [
        "John Smith added 'Own' (Classic)",
        "Org Anizer uploaded 'Show' for John Smith (Classic)",
    ]


async def test_upload_for_survives_a_resumed_build(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    fake_drive: FakeDrive,  # noqa: F811
    discord_posts: Posts,
) -> None:
    """The uploader is staged with the bytes (migration 004), so a build the
    scheduler resumes long after the request still names both people."""
    admin = await person("admin", admin=True)
    await _named(db, admin, "Org", "Anizer")
    target = await person("target")
    fake_drive.fail_upload = RuntimeError("Drive is down")
    fake_drive.gate = asyncio.Event()
    song = await _upload(
        client, admin, routine_name="Show", on_behalf_of_user_id=target.id
    )
    assert await db.fetchval("SELECT uploaded_by_user_id FROM song_uploads") == admin.id
    # Submitted while building, so the failed build is kept for a retry.
    event = await client.post(
        "/v1/events",
        json={"name": "Open", "start_date": "2026-05-01", "end_date": "2026-05-02"},
        headers=admin.headers,
    )
    res = await client.post(
        "/v1/event-song-submissions",
        json={"event_id": event.json()["data"]["id"], "song_id": song["id"]},
        headers=target.headers,
    )
    assert res.status_code == 201, res.text
    fake_drive.gate.set()
    await song_builds.wait_for_builds()
    assert await discord_posts.songs_added() == []
    # A retried failure is not a fault until it gives up.
    assert await discord_posts.channel(discord.CHANNEL_ERRORS) == []

    fake_drive.fail_upload = None
    await db.execute("UPDATE song_uploads SET next_attempt_at = 0")
    async with get_sessionmaker()() as s:
        assert await song_builds.process_song_uploads(s) == 1
    (added,) = await discord_posts.songs_added()
    assert added["description"].startswith(
        "Org Anizer uploaded 'Show' for Target (Classic)\n"
    )


async def test_portal_and_managed_details() -> None:
    assert (
        notifications.song_added_text(
            owner="Jane",
            uploader=None,
            routine="R",
            division="Team",
            partner="Jt Swing",
            partner_kind="team",
        )
        == "Jane added 'R' (Team, team Jt Swing)"
    )
    assert (
        notifications.song_added_text(
            owner="Jane",
            uploader=None,
            routine=None,
            division=None,
            partner="Formation",
            partner_kind="other",
        )
        == "Jane added a song (as Formation)"
    )
    assert (
        notifications.song_added_text(
            owner="Coach",
            uploader=None,
            routine="R*",
            division="ProAm",
            managed="Lee Lead & Fay Follow",
        )
        == "Coach added 'R\\*' (ProAm, managed partnership Lee Lead & Fay Follow)"
    )
    assert notifications.person_name(" ", None, "", "Disp") == "Disp"
    assert notifications.person_name(None, None) == "someone"


async def test_failed_build_reports_a_fault_and_announces_nothing(
    client: httpx.AsyncClient,
    person: Callable,
    fake_drive: FakeDrive,  # noqa: F811
    discord_posts: Posts,
) -> None:
    fake_drive.fail_upload = RuntimeError("bucket secret-name refused")
    jane = await person("jane")
    song = await _upload(client, jane, routine_name="Ballad")

    assert await discord_posts.songs_added() == []
    (fault,) = await discord_posts.channel(discord.CHANNEL_ERRORS)
    # Labelled: ENVIRONMENT=test is not production, and the errors channel
    # is shared with production.
    assert fault["title"] == "[DEVELOPMENT] fault · background"
    assert fault["description"] == (
        f"`song build {song['id']} · failed, song removed`\n```RuntimeError```"
    )
    assert "secret" not in str(fault)
    assert fault["footer"]["text"] == "api-deejaytools · test"


async def test_song_deleted_mid_build_announces_nothing(
    client: httpx.AsyncClient,
    person: Callable,
    fake_drive: FakeDrive,  # noqa: F811
    discord_posts: Posts,
) -> None:
    jane = await person("jane")
    fake_drive.gate = asyncio.Event()
    song = await _upload(client, jane)
    deleted = await client.delete(f"/v1/songs/{song['id']}", headers=jane.headers)
    fake_drive.gate.set()
    assert deleted.status_code == 204, deleted.text
    assert await discord_posts.songs_added() == []


@pytest.mark.parametrize("broken", ["song_added_text", "announce_song_added"])
async def test_a_broken_announcement_does_not_fail_the_build(
    broken: str,
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    fake_drive: FakeDrive,  # noqa: F811
    discord_posts: Posts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Composing or registering "song added" raises after the file is in
    Drive: the song stays, built, and simply goes unannounced."""

    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("announcement bug")

    target = song_builds if broken == "song_added_text" else notifications
    monkeypatch.setattr(target, broken, boom)
    jane = await person("jane")
    song = await _upload(client, jane, routine_name="Ballad")

    assert await discord_posts.songs_added() == []
    assert await discord_posts.channel(discord.CHANNEL_ERRORS) == []
    assert (
        await db.fetchval("SELECT drive_file_id FROM songs WHERE id = $1", song["id"])
        == "file-1"
    )
    assert await db.fetchval("SELECT count(*) FROM song_uploads") == 0
    assert fake_drive.soft_deleted == []


async def test_announcement_on_a_rolled_back_transaction_is_dropped(
    db: asyncpg.Connection, discord_posts: Posts
) -> None:
    async with get_sessionmaker()() as s:
        notifications.announce_song_added(s, "never happened")
        await s.get(Song, "nothing")
        await s.rollback()
        # Nor does it ride on the session's next, unrelated commit.
        await s.commit()
    assert await discord_posts.channel(discord.CHANNEL_ACTIVITY) == []


async def test_announcement_before_any_query_does_not_outlive_a_close(
    db: asyncpg.Connection, discord_posts: Posts
) -> None:
    """On an AsyncSession over Postgres: registered before the session did
    anything, then closed — it must not post on the session's next commit."""
    s = get_sessionmaker()()
    notifications.announce_song_added(s, "never happened")
    await s.close()
    await s.get(Song, "nothing")
    await s.commit()
    await s.close()
    assert await discord_posts.channel(discord.CHANNEL_ACTIVITY) == []


# --- the change feed --------------------------------------------------------------


async def test_rollup_names_the_caller_and_leaves_out_bookkeeping(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    discord_posts: Posts,
) -> None:
    jane = await person("jane")  # sync: not in the feed
    created = await client.post(
        "/v1/teams", json={"identifier": "Jt Swing"}, headers=jane.headers
    )
    assert created.status_code == 201
    listed = await client.get("/v1/teams", headers=jane.headers)  # audit row only
    assert listed.status_code == 200

    # Even a principal whose display name is their email (as provisioning
    # sets it) is named by Clerk user id: the channel is shared.
    await db.execute(
        "UPDATE identity_principals SET display_name = 'jane@example.test', "
        "email = 'jane@example.test' WHERE subject = $1",
        jane.id,
    )
    await client.post("/v1/teams", json={"identifier": "Two"}, headers=jane.headers)

    first, second = await discord_posts.changes()
    assert first["title"] == "[DEVELOPMENT] data changed"
    assert first["description"] == "`teams` +1"
    assert "identity_audit_events" not in str(first)
    assert first["footer"]["text"] == (
        f"POST /v1/teams · {jane.id} · api-deejaytools · test"
    )
    assert second["footer"]["text"] == (
        f"POST /v1/teams · {jane.id} · api-deejaytools · test"
    )
    assert "@" not in str(first) + str(second)


async def test_rolled_back_request_reports_no_change(
    client: httpx.AsyncClient,
    person: Callable,
    discord_posts: Posts,
) -> None:
    jane = await person("jane")
    first = await client.post(
        "/v1/teams", json={"identifier": "One"}, headers=jane.headers
    )
    second = await client.post(
        "/v1/teams", json={"identifier": "Two"}, headers=jane.headers
    )
    # Renaming onto an existing name: the UPDATE runs, fails, rolls back.
    clash = await client.patch(
        f"/v1/teams/{second.json()['data']['id']}",
        json={"identifier": "One"},
        headers=jane.headers,
    )
    assert clash.status_code == 409, clash.text
    assert first.status_code == 201
    paths = [c["footer"]["text"].split(" · ")[0] for c in await discord_posts.changes()]
    assert paths == ["POST /v1/teams", "POST /v1/teams"]
    assert (
        await discord_posts.channel(discord.CHANNEL_ERRORS) == []
    )  # a 4xx is not a fault


async def test_operator_queue_changes_are_reported(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    discord_posts: Posts,
) -> None:
    admin = await person("admin", admin=True)
    await add_job(db, status="failed", attempts=10, submission_id=None, kind="trash")
    res = await client.post("/v1/admin/drive-jobs/job_1/retry", headers=admin.headers)
    assert res.status_code == 200, res.text
    (change,) = await discord_posts.changes()
    assert change["description"] == "`drive_jobs` *1"


# --- faults -----------------------------------------------------------------------


async def test_unhandled_exception_posts_its_type_not_its_text(
    client: httpx.AsyncClient,
    person: Callable,
    monkeypatch: pytest.MonkeyPatch,
    discord_posts: Posts,
) -> None:
    jane = await person("jane")

    async def broken(*_: Any) -> Any:
        raise RuntimeError("SELECT * FROM users WHERE email = 'jane@secret'")

    monkeypatch.setattr(teams, "_load_owned", broken)
    res = await client.patch(
        "/v1/teams/t1", json={"identifier": "X"}, headers=jane.headers
    )
    assert res.status_code == 500
    assert res.json()["error"]["code"] == "INTERNAL"  # the envelope, as before

    (fault,) = await discord_posts.channel(discord.CHANNEL_ERRORS)
    assert fault["title"] == "[DEVELOPMENT] fault · 500"
    assert fault["description"] == "`PATCH /v1/teams/t1`\n```RuntimeError```"
    assert "secret" not in str(fault)
    assert fault["footer"]["text"] == f"{jane.id} · api-deejaytools · test"


async def test_sync_fault_names_the_verified_subject_not_the_body_email(
    client: httpx.AsyncClient,
    clerk: Clerk,
    monkeypatch: pytest.MonkeyPatch,
    discord_posts: Posts,
) -> None:
    """The body's email is unverified text the caller chose; the footer
    carries the subject the token proved."""

    async def broken(*_: Any, **__: Any) -> Any:
        raise RuntimeError("provisioning failed")

    monkeypatch.setattr(auth_router, "provision_principal", broken)
    uid = f"user_{uuid.uuid4().hex[:24]}"
    res = await client.post(
        "/v1/auth/sync",
        json={"email": "someone.else@spoof.test"},
        headers=bearer(clerk.token(uid)),
    )
    assert res.status_code == 500

    (fault,) = await discord_posts.channel(discord.CHANNEL_ERRORS)
    assert fault["footer"]["text"] == f"{uid} · api-deejaytools · test"
    assert "spoof" not in str(fault)


async def test_server_error_answer_posts_its_code(
    client: httpx.AsyncClient,
    person: Callable,
    monkeypatch: pytest.MonkeyPatch,
    discord_posts: Posts,
) -> None:
    jane = await person("jane")

    async def failing(*_: Any) -> Any:
        raise api_error(500, ErrorCode.INTERNAL, "Failed to load jane@secret")

    monkeypatch.setattr(teams, "_load_owned", failing)
    res = await client.patch(
        "/v1/teams/t1", json={"identifier": "X"}, headers=jane.headers
    )
    assert res.status_code == 500
    (fault,) = await discord_posts.channel(discord.CHANNEL_ERRORS)
    assert fault["description"] == "`PATCH /v1/teams/t1`\n```ApiError INTERNAL```"


async def test_song_build_that_gives_up_posts_once(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    fake_drive: FakeDrive,  # noqa: F811
    discord_posts: Posts,
) -> None:
    """A submitted song is kept and retried; only giving up is a fault."""
    admin = await person("admin", admin=True)
    jane = await person("jane")
    fake_drive.fail_upload = RuntimeError("Drive is down")
    fake_drive.gate = asyncio.Event()
    song = await _upload(client, jane)
    event = await client.post(
        "/v1/events",
        json={"name": "Open", "start_date": "2026-05-01", "end_date": "2026-05-02"},
        headers=admin.headers,
    )
    await client.post(
        "/v1/event-song-submissions",
        json={"event_id": event.json()["data"]["id"], "song_id": song["id"]},
        headers=jane.headers,
    )
    fake_drive.gate.set()
    await song_builds.wait_for_builds()
    await db.execute(
        "UPDATE song_uploads SET next_attempt_at = 0, attempts = $1",
        song_builds.MAX_ATTEMPTS - 1,
    )
    async with get_sessionmaker()() as s:
        assert await song_builds.process_song_uploads(s) == 1

    (fault,) = await discord_posts.channel(discord.CHANNEL_ERRORS)
    assert fault["description"] == (
        f"`song build {song['id']} · gave up after 10 attempts`\n```RuntimeError```"
    )
    assert await db.fetchval("SELECT status FROM song_uploads") == "failed"


def _lose_claim_after_first_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_mine`` matches for the first read, then as if another build had
    taken the claim: the guarded write that follows matches no row."""
    real = song_builds._mine
    calls = {"n": 0}

    def mine(song_id: str, claim: str) -> list[Any]:
        calls["n"] += 1
        return real(song_id, claim if calls["n"] == 1 else "taken-over")

    monkeypatch.setattr(song_builds, "_mine", mine)


async def _held_build(
    client: httpx.AsyncClient, who: Any, db: asyncpg.Connection, drive: FakeDrive
) -> tuple[str, str]:
    """A song whose build is running (held at the Drive upload): id, claim."""
    drive.gate = asyncio.Event()
    song = await _upload(client, who)
    await asyncio.sleep(0.05)
    claim = await db.fetchval("SELECT claim_id FROM song_uploads")
    return song["id"], claim


async def test_failure_cleanup_that_lost_its_claim_reports_nothing(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    fake_drive: FakeDrive,  # noqa: F811
    discord_posts: Posts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guarded delete removed nothing, so no "failed, song removed"."""
    jane = await person("jane")
    song_id, claim = await _held_build(client, jane, db, fake_drive)
    with monkeypatch.context() as m:
        _lose_claim_after_first_read(m)
        await song_builds._on_failure(
            get_sessionmaker(), song_id, claim, RuntimeError("x")
        )
    assert await db.fetchval("SELECT count(*) FROM songs WHERE id = $1", song_id) == 1
    row = await db.fetchrow("SELECT status, attempts FROM song_uploads")
    assert dict(row) == {"status": "running", "attempts": 0}

    fake_drive.gate.set()  # the build that owns it carries on
    assert len(await discord_posts.songs_added()) == 1
    assert await discord_posts.channel(discord.CHANNEL_ERRORS) == []
    assert fake_drive.soft_deleted == []


async def test_retry_that_lost_its_claim_does_not_give_up(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    fake_drive: FakeDrive,  # noqa: F811
    discord_posts: Posts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At the last attempt, a guarded update that hit no row is not "gave
    up": the run that took the claim over owns that report (and Sentry's)."""
    sentry = Sentry()
    monkeypatch.setattr(song_builds, "sentry_sdk", sentry)
    jane = await person("jane")
    song_id, claim = await _held_build(client, jane, db, fake_drive)
    await db.execute(
        "UPDATE song_uploads SET attempts = $1", song_builds.MAX_ATTEMPTS - 1
    )
    with monkeypatch.context() as m:
        _lose_claim_after_first_read(m)
        await song_builds._schedule_retry(
            get_sessionmaker(), song_id, claim, RuntimeError("x")
        )
    assert sentry.captured == []
    assert await db.fetchval("SELECT status FROM song_uploads") == "running"

    fake_drive.gate.set()
    await song_builds.wait_for_builds()
    assert await discord_posts.channel(discord.CHANNEL_ERRORS) == []


async def test_superseded_drive_job_failure_reports_nothing(
    db: asyncpg.Connection,
    session: AsyncSession,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    discord_posts: Posts,
) -> None:
    """The lease ran out mid-run and the job was reclaimed: this run's
    guarded update matches no row, so it must not post "gave up" or reach
    Sentry — the run that now owns the job does."""
    sentry = Sentry()
    monkeypatch.setattr(drive_jobs, "sentry_sdk", sentry)
    from api_deejaytools.services import drive

    async def reclaimed_then_failed(*_: Any, **__: Any) -> Any:
        await db.execute("UPDATE drive_jobs SET status = 'pending' WHERE id = 'job_1'")
        raise RuntimeError("Drive down")

    monkeypatch.setattr(drive, "copy_song_to_event_folder", reclaimed_then_failed)
    await seed_submission(db)
    await add_job(db, attempts=drive_jobs.MAX_ATTEMPTS - 1)
    await drive_jobs.process_drive_jobs(session)

    assert await discord_posts.channel(discord.CHANNEL_ERRORS) == []
    assert sentry.captured == []
    row = await db.fetchrow("SELECT status, attempts FROM drive_jobs")
    assert dict(row) == {"status": "pending", "attempts": drive_jobs.MAX_ATTEMPTS - 1}


async def test_terminal_drive_job_failure_posts_once(
    db: asyncpg.Connection,
    session: AsyncSession,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    discord_posts: Posts,
) -> None:
    fake = FakeJobDrive()
    fake.copy_error = RuntimeError("Drive down: token abc")
    from api_deejaytools.services import drive

    monkeypatch.setattr(
        drive, "copy_song_to_event_folder", fake.copy_song_to_event_folder
    )
    await seed_submission(db)
    await add_job(db, attempts=0)
    await add_job(db, job_id="job_2", attempts=drive_jobs.MAX_ATTEMPTS - 1)
    await drive_jobs.process_drive_jobs(session)  # job_1 retries, job_2 gives up
    await drive_jobs.process_drive_jobs(session)  # nothing due

    (fault,) = await discord_posts.channel(discord.CHANNEL_ERRORS)
    assert fault["description"] == (
        "`drive job job_2 (copy) · gave up after 10 attempts`\n```RuntimeError```"
    )
    assert "token" not in str(fault)


async def test_failing_scheduler_step_posts_once_per_run_of_failures(
    monkeypatch: pytest.MonkeyPatch, discord_posts: Posts
) -> None:
    broken = {"on": True}

    async def statuses(_db: Any) -> None:
        if broken["on"]:
            raise RuntimeError("connection refused to 10.0.0.1")

    async def nothing(_db: Any) -> None:
        return None

    monkeypatch.setattr(session_tick, "tick_session_statuses", statuses)
    monkeypatch.setattr(session_tick, "fill_running_sessions", nothing)
    monkeypatch.setattr(scheduler.drive, "drive_configured", lambda: False)

    await scheduler.run_tick()
    await scheduler.run_tick()
    assert len(await discord_posts.channel(discord.CHANNEL_ERRORS)) == 1
    broken["on"] = False
    await scheduler.run_tick()
    broken["on"] = True
    await scheduler.run_tick()
    faults = await discord_posts.channel(discord.CHANNEL_ERRORS)
    assert [f["description"] for f in faults] == [
        "`scheduler · sessions`\n```RuntimeError```"
    ] * 2


# --- off ----------------------------------------------------------------------------


async def test_nothing_is_sent_without_webhooks(
    client: httpx.AsyncClient,
    person: Callable,
    monkeypatch: pytest.MonkeyPatch,
    fake_drive: FakeDrive,  # noqa: F811
) -> None:
    posted: list[Any] = []

    async def post_webhook(url: str, **kwargs: Any) -> bool:
        posted.append(url)
        return True

    monkeypatch.setattr(discord, "post_webhook", post_webhook)
    jane = await person("jane")
    await client.post("/v1/teams", json={"identifier": "A"}, headers=jane.headers)
    await client.post("/v1/teams", json={"identifier": "B"}, headers=jane.headers)
    await _upload(client, jane)  # song added
    fake_drive.fail_upload = RuntimeError("down")
    await _send(client, jane, str(uuid.uuid4()), 0, 1, MP3)  # a fault
    await song_builds.wait_for_builds()
    await activity.wait_for_deliveries()

    assert posted == []
    # Said once per channel in the log, not once per message.
    assert notifications._unconfigured_logged == {"activity", "errors"}
