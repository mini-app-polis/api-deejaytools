"""POST /v1/songs/upload/chunk and the durable song build behind it."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import asyncpg
import httpx
import pytest
from mini_app_polis.google.drive import ShareResult

from api_deejaytools.services import song_builds
from api_deejaytools.services.drive import DriveNotConfiguredError

AUDIO = b"audio-frames-" + b"x" * 3000 + b"-end"
MP3 = bytes([0x49, 0x44, 0x33, 0x04, 0, 0, 0, 0, 0, 0]) + AUDIO
UPLOAD_URL = "/v1/songs/upload/chunk"


class FakeDrive:
    """Stands in for services.drive as song_builds uses it."""

    def __init__(self) -> None:
        self.uploads: list[dict[str, Any]] = []
        self.shares: list[tuple[str, list[str | None]]] = []
        self.soft_deleted: list[str] = []
        self.existing: dict[str, tuple[str, str]] = {}
        self.fail_upload: Exception | None = None
        self.gate: asyncio.Event | None = None

    async def find_song_file(
        self, *, key: str, value: str, **_: Any
    ) -> tuple[str, str] | None:
        return self.existing.get(value)

    async def upload_song_file(self, data: bytes, **kwargs: Any) -> tuple[str, str]:
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_upload is not None:
            raise self.fail_upload
        self.uploads.append({"data": data, **kwargs})
        return f"file-{len(self.uploads)}", "division-folder"

    async def share_song_file(self, file_id: str, emails: Any) -> ShareResult:
        self.shares.append((file_id, list(emails)))
        return ShareResult(shared=[], failed=[])

    async def soft_delete(self, file_id: str) -> None:
        self.soft_deleted.append(file_id)


@pytest.fixture
def fake_drive(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDrive]:
    fake = FakeDrive()
    for name in (
        "find_song_file",
        "upload_song_file",
        "share_song_file",
        "soft_delete",
    ):
        monkeypatch.setattr(song_builds.drive, name, getattr(fake, name))
    yield fake


def _form(upload_id: str, index: int, total: int, **extra: str) -> dict[str, str]:
    return {
        "upload_id": upload_id,
        "chunk_index": str(index),
        "total_chunks": str(total),
        "original_filename": "my routine.MP3",
        "mime_type": "audio/mpeg",
        "division": "Classic",
        **extra,
    }


async def _send(
    client: httpx.AsyncClient,
    who: Any,
    upload_id: str,
    index: int,
    total: int,
    data: bytes,
    **extra: str,
) -> httpx.Response:
    return await client.post(
        UPLOAD_URL,
        data=_form(upload_id, index, total, **extra),
        files={"chunk": ("chunk", data)},
        headers=who.headers,
    )


async def _upload(
    client: httpx.AsyncClient, who: Any, data: bytes = MP3, **extra: str
) -> dict:
    res = await _send(client, who, str(uuid.uuid4()), 0, 1, data, **extra)
    assert res.status_code == 200, res.text
    return res.json()["data"]["song"]


async def _partner(client: httpx.AsyncClient, who: Any, role: str = "follower") -> str:
    res = await client.post(
        "/v1/partners",
        json={"first_name": "Pat", "last_name": "Partner", "partner_role": role},
        headers=who.headers,
    )
    return res.json()["data"]["id"]


async def test_chunks_assemble_out_of_order_and_build(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    fake_drive: FakeDrive,
) -> None:
    alice = await person("alice")
    await db.execute("UPDATE users SET last_name = 'Tester' WHERE id = $1", alice.id)
    partner = await _partner(client, alice)
    upload_id = str(uuid.uuid4())
    third = len(MP3) // 3 + 1
    parts = [MP3[i : i + third] for i in range(0, len(MP3), third)]

    # Any order, as long as the last index comes last: it triggers assembly.
    first = await _send(client, alice, upload_id, 1, 3, parts[1], partner_id=partner)
    assert first.json() == {
        "data": {"received": True, "complete": False},
        "meta": {"version": "v1"},
    }
    await _send(client, alice, upload_id, 0, 3, parts[0], partner_id=partner)
    last = await _send(
        client,
        alice,
        upload_id,
        2,
        3,
        parts[2],
        partner_id=partner,
        routine_name="Blue Monday",
    )

    body = last.json()["data"]
    assert (body["received"], body["complete"]) == (True, True)
    song = body["song"]
    for field in (
        "processed_filename",
        "season_year",
        "drive_file_id",
        "drive_folder_id",
    ):
        assert song[field] is None
    assert song["display_name"] == "Blue Monday"

    await song_builds.wait_for_builds()

    season = song_builds.season_year_now()
    expected = f"AliceTester_PatPartner_Classic_{season}_BlueMonday_v01.mp3"
    [upload] = fake_drive.uploads
    assert upload["filename"] == expected
    assert upload["mime_type"] == "audio/mpeg"
    assert upload["season_year"] == season
    assert upload["app_properties"] == {"deejaytools_song_id": song["id"]}
    assert AUDIO in upload["data"]  # tagged, audio intact
    row = await db.fetchrow("SELECT * FROM songs WHERE id = $1", song["id"])
    assert (
        row["processed_filename"],
        row["drive_file_id"],
        row["drive_folder_id"],
    ) == (
        expected,
        "file-1",
        "division-folder",
    )
    assert row["original_filename"] == "my routine.MP3"
    assert await db.fetchval("SELECT count(*) FROM song_uploads") == 0
    [(shared_file, emails)] = fake_drive.shares
    assert shared_file == "file-1" and emails[0].startswith("alice.")


async def test_validation_order_and_messages(
    client: httpx.AsyncClient, person: Callable, fake_drive: FakeDrive
) -> None:
    alice = await person("alice")
    uid = str(uuid.uuid4())
    cases: list[tuple[dict[str, str], str]] = [
        ({"upload_id": "nope"}, "Invalid upload_id"),
        ({"division": "  "}, "division is required"),
        ({"chunk_index": "-1"}, "Invalid chunk_index"),
        ({"chunk_index": "1.5"}, "Invalid chunk_index"),
        ({"total_chunks": "31"}, "Invalid total_chunks (max 30)"),
        ({"chunk_index": "2", "total_chunks": "2"}, "chunk_index out of range"),
    ]
    for override, message in cases:
        res = await client.post(
            UPLOAD_URL,
            data={**_form(uid, 0, 1), **override},
            files={"chunk": ("chunk", MP3)},
            headers=alice.headers,
        )
        assert res.status_code == 400, override
        assert res.json()["error"] == {"code": "BAD_REQUEST", "message": message}

    # JavaScript Number(): "" is 0, so this is chunk 0 of 1.
    res = await client.post(
        UPLOAD_URL,
        data={**_form(uid, 0, 1), "chunk_index": ""},
        files={"chunk": ("chunk", MP3)},
        headers=alice.headers,
    )
    assert res.json()["data"]["complete"] is True

    missing = await client.post(
        UPLOAD_URL, data=_form(uid, 0, 1), headers=alice.headers
    )
    assert missing.json()["error"]["message"] == "Missing chunk field"
    assert (await client.post(UPLOAD_URL, data=_form(uid, 0, 1))).status_code == 401


async def test_final_chunk_refusals(
    client: httpx.AsyncClient, person: Callable, fake_drive: FakeDrive
) -> None:
    alice = await person("alice")
    bob = await person("bob")
    bobs_partner = await _partner(client, bob)

    async def final(data: bytes = MP3, **extra: str) -> dict:
        res = await _send(client, alice, str(uuid.uuid4()), 0, 1, data, **extra)
        return res.json()

    assert (await final(entity_type="team", partner_id="x"))["error"]["message"] == (
        "Portal uploads cannot specify partner_id or managed_partnership_id"
    )
    assert (await final(entity_type="solo"))["error"][
        "message"
    ] == "Invalid entity_type"
    assert (await final(entity_type="team"))["error"]["message"] == (
        "team_id is required for team uploads"
    )
    assert (await final(entity_type="team", team_id="nope"))["error"][
        "message"
    ] == "Team not found"
    assert (await final(entity_type="other"))["error"]["message"] == (
        "entity_name is required for other uploads"
    )
    assert (await final(partner_id="a", managed_partnership_id="b"))["error"][
        "message"
    ] == ("Specify either partner_id or managed_partnership_id, not both")
    assert (await final(partner_id=bobs_partner))["error"]["message"] == (
        "Partner not found or does not belong to you"
    )
    assert (await final(managed_partnership_id="nope"))["error"]["message"] == (
        "Managed partnership not found or does not belong to you"
    )
    unsupported = await final(b"this is a text file, not a song")
    assert unsupported["error"] == {
        "code": "UNSUPPORTED_FORMAT",
        "message": "That file doesn't look like a supported audio format. "
        "Please upload an MP3, WAV, FLAC, or M4A.",
    }
    assert fake_drive.uploads == []


async def test_missing_chunk_is_409(
    client: httpx.AsyncClient, person: Callable, fake_drive: FakeDrive
) -> None:
    alice = await person("alice")
    upload_id = str(uuid.uuid4())
    await _send(client, alice, upload_id, 0, 3, MP3[:10])
    res = await _send(client, alice, upload_id, 2, 3, MP3[10:])
    assert res.status_code == 409
    assert res.json()["error"] == {
        "code": "CHUNK_MISSING",
        "message": "Expected 3 chunks but only received 2. Please retry the upload.",
    }


async def test_oversized_chunk(client: httpx.AsyncClient, person: Callable) -> None:
    alice = await person("alice")
    res = await _send(
        client, alice, str(uuid.uuid4()), 0, 1, b"x" * (10 * 1024 * 1024 + 1)
    )
    assert res.json()["error"]["message"] == "Chunk exceeds 10 MB limit"


async def test_team_upload_creates_one_placeholder_partner(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    fake_drive: FakeDrive,
) -> None:
    alice = await person("alice")
    team = await client.post(
        "/v1/teams", json={"identifier": "Jt Swing"}, headers=alice.headers
    )
    team_id = team.json()["data"]["id"]

    one = await _upload(client, alice, entity_type="team", team_id=team_id)
    two = await _upload(client, alice, entity_type="team", team_id=team_id)
    await song_builds.wait_for_builds()

    assert one["partner_id"] == two["partner_id"]
    row = await db.fetchrow("SELECT * FROM partners WHERE id = $1", one["partner_id"])
    assert (row["kind"], row["first_name"], row["last_name"]) == (
        "team",
        team.json()["data"]["identifier"],
        "",
    )
    names = sorted(u["filename"] for u in fake_drive.uploads)
    assert names[0].endswith("_v01.mp3") and names[1].endswith("_v02.mp3")


async def test_upload_on_behalf_needs_delegation(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    fake_drive: FakeDrive,
) -> None:
    dancer = await person("dancer")
    admin = await person("admin", admin=True)
    target = await person("target")

    refused = await _send(
        client, dancer, str(uuid.uuid4()), 0, 1, MP3, on_behalf_of_user_id=target.id
    )
    assert refused.status_code == 403
    assert refused.json()["error"] == {
        "code": "FORBIDDEN",
        "message": "Admin access required",
    }

    song = await _upload(client, admin, on_behalf_of_user_id=target.id)
    assert song["user_id"] == target.id
    audit = await db.fetch(
        "SELECT allowed, resource FROM identity_audit_events "
        "WHERE scope = 'deejaytools.delegation.act' ORDER BY occurred_at"
    )
    assert [(a["allowed"], a["resource"]) for a in audit] == [
        (False, target.id),
        (True, target.id),
    ]
    missing = await _send(
        client, admin, str(uuid.uuid4()), 0, 1, MP3, on_behalf_of_user_id="user_nobody"
    )
    assert missing.json()["error"]["message"] == "Target user not found"
    await song_builds.wait_for_builds()


async def test_failed_build_removes_an_unreferenced_song(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    fake_drive: FakeDrive,
) -> None:
    fake_drive.fail_upload = DriveNotConfiguredError()
    alice = await person("alice")
    song = await _upload(client, alice)
    await song_builds.wait_for_builds()

    assert (
        await db.fetchval("SELECT count(*) FROM songs WHERE id = $1", song["id"]) == 0
    )
    assert await db.fetchval("SELECT count(*) FROM song_uploads") == 0
    assert (
        await client.get(f"/v1/songs/{song['id']}", headers=alice.headers)
    ).status_code == 404


async def _submit(
    client: httpx.AsyncClient, admin: Any, owner: Any, song_id: str
) -> str:
    event = await client.post(
        "/v1/events",
        json={"name": "Open", "start_date": "2026-05-01", "end_date": "2026-05-02"},
        headers=admin.headers,
    )
    res = await client.post(
        "/v1/event-song-submissions",
        json={"event_id": event.json()["data"]["id"], "song_id": song_id},
        headers=owner.headers,
    )
    assert res.status_code == 201, res.text
    return res.json()["data"]["id"]


async def test_failed_build_of_a_submitted_song_is_retried_until_it_lands(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    fake_drive: FakeDrive,
) -> None:
    """Fixed defect: deejaytools-api left such a song with no file for good."""
    admin = await person("admin", admin=True)
    alice = await person("alice")
    fake_drive.gate = asyncio.Event()
    fake_drive.fail_upload = RuntimeError("Drive is down")
    song = await _upload(client, alice)
    submission = await _submit(client, admin, alice, song["id"])  # while building
    fake_drive.gate.set()
    await song_builds.wait_for_builds()

    staged = await db.fetchrow("SELECT status, attempts, last_error FROM song_uploads")
    assert (staged["status"], staged["attempts"], staged["last_error"]) == (
        "pending",
        1,
        "Drive is down",
    )
    assert (
        await db.fetchval("SELECT count(*) FROM songs WHERE id = $1", song["id"]) == 1
    )

    fake_drive.fail_upload = None
    await db.execute("UPDATE song_uploads SET next_attempt_at = 0")
    await db.execute("DELETE FROM drive_jobs")
    from api_deejaytools.database import get_sessionmaker

    async with get_sessionmaker()() as session:
        assert await song_builds.process_song_uploads(session) == 1
    await song_builds.wait_for_builds()

    assert (
        await db.fetchval("SELECT drive_file_id FROM songs WHERE id = $1", song["id"])
        == "file-1"
    )
    jobs = await db.fetch("SELECT kind, submission_id FROM drive_jobs")
    assert [(j["kind"], j["submission_id"]) for j in jobs] == [("copy", submission)]


async def test_interrupted_build_resumes_without_uploading_twice(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    fake_drive: FakeDrive,
) -> None:
    """Fixed defects: a restart mid-build, and a failure after the Drive upload."""
    alice = await person("alice")
    fake_drive.gate = asyncio.Event()
    song = await _upload(client, alice)
    # The process "dies": the in-flight build is abandoned, its lease runs out.
    for task in list(song_builds._in_flight):
        task.cancel()
    await asyncio.gather(*list(song_builds._in_flight), return_exceptions=True)
    stale = int(time.time() * 1000) - song_builds.LEASE_MS - 1
    await db.execute(
        "UPDATE song_uploads SET status = 'running', updated_at = $1", stale
    )
    # The file did reach Drive before the crash, tagged with the song id.
    fake_drive.existing[song["id"]] = ("file-from-before", "division-folder")
    fake_drive.gate = None

    from api_deejaytools.database import get_sessionmaker

    async with get_sessionmaker()() as session:
        assert await song_builds.process_song_uploads(session) == 1
    await song_builds.wait_for_builds()

    assert fake_drive.uploads == []
    assert (
        await db.fetchval("SELECT drive_file_id FROM songs WHERE id = $1", song["id"])
        == "file-from-before"
    )


async def test_concurrent_uploads_for_one_slot_get_distinct_versions(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    fake_drive: FakeDrive,
) -> None:
    """Fixed defect: two builds in flight for a slot both took v01."""
    alice = await person("alice")
    fake_drive.gate = asyncio.Event()
    await asyncio.gather(_upload(client, alice), _upload(client, alice))
    fake_drive.gate.set()
    await song_builds.wait_for_builds()

    names = sorted(u["filename"] for u in fake_drive.uploads)
    assert [n.rsplit("_", 1)[1] for n in names] == ["v01.mp3", "v02.mp3"]


async def test_song_deleted_mid_build_has_its_file_deprecated(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    fake_drive: FakeDrive,
) -> None:
    alice = await person("alice")
    fake_drive.gate = asyncio.Event()
    song = await _upload(client, alice)
    res = await client.delete(f"/v1/songs/{song['id']}", headers=alice.headers)
    assert res.status_code == 204
    fake_drive.gate.set()
    await song_builds.wait_for_builds()

    assert fake_drive.soft_deleted == ["file-1"]
    row = await db.fetchrow(
        "SELECT drive_file_id, deleted_at FROM songs WHERE id = $1", song["id"]
    )
    assert row["drive_file_id"] == "file-1" and row["deleted_at"] is not None


def test_entity_names_and_proam_swap() -> None:
    from api_deejaytools.models import ManagedPartnership, Partner, User

    user = User(id="u1", first_name="Ada", last_name="Lovelace")
    follower = Partner(
        first_name="Bo", last_name="B", kind="partner", partner_role="follower"
    )
    leader = Partner(
        first_name="Lee", last_name="L", kind="partner", partner_role="leader"
    )
    team = Partner(
        first_name="Jt Swing", last_name="", kind="team", partner_role="follower"
    )
    managed = ManagedPartnership(
        leader_first_name="Ma",
        leader_last_name="Lead",
        follower_first_name="Mi",
        follower_last_name="Fol",
    )

    def names(**kw: object) -> tuple[str, str | None]:
        kw.setdefault("partner", None)
        kw.setdefault("managed", None)
        kw.setdefault("division", "Classic")
        return song_builds.entity_names(user=user, **kw)  # type: ignore[arg-type]

    assert names() == ("Ada Lovelace", None)
    assert names(partner=follower) == ("Ada Lovelace", "Bo B")
    assert names(partner=leader) == ("Lee L", "Ada Lovelace")
    assert names(partner=team) == ("Jt Swing", None)
    assert names(managed=managed) == ("Ma Lead", "Mi Fol")
    # ProAm FollowerAm names the follower first.
    assert names(partner=follower, division=" ProAm FollowerAm ") == (
        "Bo B",
        "Ada Lovelace",
    )
    assert names(division="ProAm FollowerAm") == ("Ada Lovelace", None)
    nameless = User(id="user_x", first_name=None, last_name=None)
    assert song_builds.entity_names(
        user=nameless, partner=None, managed=None, division=None
    ) == ("user_x", None)


def test_naming_rules() -> None:
    assert song_builds.sanitize_segment("mary-jo o'neil") == "MaryjoOneil"
    assert song_builds.sanitize_segment("José") == "Jos"
    assert song_builds.static_segment("ProAm LeaderAm") == "ProAmLeaderAm"
    assert song_builds.file_extension("song.MP3") == "mp3"
    assert song_builds.file_extension(".hidden") == ""
    assert song_builds.file_extension("trailing.") == ""
    assert song_builds.versioned_filename("A_B", 3, "") == "A_B_v03"
    assert song_builds.versioned_filename("A_B", 100, "wav") == "A_B_v100.wav"


async def test_build_taken_over_stops_without_writing(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    fake_drive: FakeDrive,
) -> None:
    """A build that lost its lease (another took over) leaves the row alone,
    whether it then succeeds or fails."""
    alice = await person("alice")
    fake_drive.gate = asyncio.Event()
    song = await _upload(client, alice)
    await asyncio.sleep(0.05)  # the build is waiting on the Drive upload
    await db.execute("UPDATE song_uploads SET claim_id = 'the-new-owner'")
    fake_drive.fail_upload = RuntimeError("drive down")
    fake_drive.gate.set()
    await song_builds.wait_for_builds()

    # Not deleted as an unreferenced failure, no retry scheduled by the loser.
    row = await db.fetchrow("SELECT status, attempts, claim_id FROM song_uploads")
    assert dict(row) == {
        "status": "running",
        "attempts": 0,
        "claim_id": "the-new-owner",
    }
    assert (
        await db.fetchval("SELECT count(*) FROM songs WHERE id = $1", song["id"]) == 1
    )


async def test_build_taken_over_after_upload_does_not_record(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    fake_drive: FakeDrive,
) -> None:
    alice = await person("alice")
    fake_drive.gate = asyncio.Event()
    song = await _upload(client, alice)
    await asyncio.sleep(0.05)
    await db.execute("UPDATE song_uploads SET claim_id = 'the-new-owner'")
    fake_drive.gate.set()
    await song_builds.wait_for_builds()

    assert (
        await db.fetchval("SELECT drive_file_id FROM songs WHERE id = $1", song["id"])
        is None
    )
    assert await db.fetchval("SELECT drive_file_id FROM song_uploads") is None


async def test_running_build_renews_its_lease(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    fake_drive: FakeDrive,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(song_builds, "HEARTBEAT_S", 0.02)
    alice = await person("alice")
    fake_drive.gate = asyncio.Event()
    await _upload(client, alice)
    await asyncio.sleep(0.05)
    first = await db.fetchval("SELECT updated_at FROM song_uploads")
    await asyncio.sleep(0.1)
    later = await db.fetchval("SELECT updated_at FROM song_uploads")
    fake_drive.gate.set()
    await song_builds.wait_for_builds()
    assert later > first


async def test_tick_starts_builds_without_waiting_and_caps_them(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    fake_drive: FakeDrive,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api_deejaytools.database import get_sessionmaker

    alice = await person("alice")
    fake_drive.gate = asyncio.Event()
    await _upload(client, alice)
    await _upload(client, alice, routine_name="Second")
    for task in list(song_builds._in_flight):
        task.cancel()
    await asyncio.gather(*list(song_builds._in_flight), return_exceptions=True)
    await db.execute("UPDATE song_uploads SET status = 'pending', next_attempt_at = 0")
    monkeypatch.setattr(song_builds, "MAX_IN_FLIGHT", 1)

    async with get_sessionmaker()() as session:
        # Returns while the build is still blocked on Drive.
        assert await asyncio.wait_for(song_builds.process_song_uploads(session), 2) == 1
        assert await song_builds.process_song_uploads(session) == 0  # at the cap
    fake_drive.gate.set()
    await song_builds.wait_for_builds()
    assert len(fake_drive.uploads) == 1
