"""/v1/admin/drive-jobs: the operator view of the drive_jobs queue."""

from __future__ import annotations

import time
from collections.abc import Callable

import asyncpg
import httpx
import pytest

from api_deejaytools.routers import admin_drive_jobs

from .content_helpers import drive_jobs, insert_event, insert_song

URL = "/v1/admin/drive-jobs"
INTERNAL = {"error": {"code": "INTERNAL", "message": "Internal server error"}}


async def _job(
    db: asyncpg.Connection,
    job_id: str,
    *,
    kind: str = "copy",
    status: str = "pending",
    submission_id: str | None = None,
    file_id: str | None = None,
    attempts: int = 0,
    last_error: str | None = None,
    updated_at: int = 1,
) -> None:
    await db.execute(
        "INSERT INTO drive_jobs (id, kind, submission_id, file_id, status, attempts,"
        " next_attempt_at, last_error, created_at, updated_at)"
        " VALUES ($1, $2, $3, $4, $5, $6, 7, $7, 3, $8)",
        job_id,
        kind,
        submission_id,
        file_id,
        status,
        attempts,
        last_error,
        updated_at,
    )


async def _submission(
    db: asyncpg.Connection,
    user_id: str,
    sid: str,
    *,
    event_name: str = "Swing Fling",
    copy: str | None = None,
) -> str:
    event = await insert_event(db, name=event_name)
    song = await insert_song(db, user_id)
    await db.execute(
        "INSERT INTO event_song_submissions (id, event_id, song_id,"
        " submitted_by_user_id, drive_copy_file_id, created_at)"
        " VALUES ($1, $2, $3, $4, $5, 1)",
        sid,
        event,
        song,
        user_id,
        copy,
    )
    return sid


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/summary"),
        ("GET", ""),
        ("POST", "/backfill-renames"),
        ("POST", "/job/retry"),
    ],
)
async def test_every_route_requires_a_token_and_the_scope(
    client: httpx.AsyncClient, person: Callable, method: str, path: str
) -> None:
    alice = await person("alice")
    res = await client.request(method, f"{URL}{path}")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHORIZED"
    res = await client.request(method, f"{URL}{path}", headers=alice.headers)
    assert res.status_code == 403
    assert res.json()["error"] == {
        "code": "FORBIDDEN",
        "message": "Admin access required",
    }


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


async def test_summary_empty(client: httpx.AsyncClient, person: Callable) -> None:
    admin = await person("admin", admin=True)
    res = await client.get(f"{URL}/summary", headers=admin.headers)
    assert res.status_code == 200, res.text
    assert res.json() == {
        "data": {"by_status": {}, "submissions_without_copy": 0},
        "meta": {"version": "v1"},
    }


async def test_summary_counts(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    await _submission(db, admin.id, "s1")
    await _submission(db, admin.id, "s2")
    await _submission(db, admin.id, "s3", copy="file_3")
    for i, status in enumerate(["pending", "pending", "done", "failed"]):
        await _job(db, f"j{i}", status=status)
    res = await client.get(f"{URL}/summary", headers=admin.headers)
    assert res.json()["data"] == {
        "by_status": {"pending": 2, "done": 1, "failed": 1},
        "submissions_without_copy": 2,
    }


async def test_summary_failure_is_internal(
    client: httpx.AsyncClient, person: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await person("admin", admin=True)

    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(admin_drive_jobs, "success", boom)
    res = await client.get(f"{URL}/summary", headers=admin.headers)
    assert res.status_code == 500
    assert res.json() == INTERNAL


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


async def test_list_shape_order_and_filter(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    sid = await _submission(db, admin.id, "s1", event_name="Swingtacular 2027")
    await _job(
        db,
        "j_copy",
        submission_id=sid,
        status="failed",
        attempts=10,
        last_error="Drive not configured",
        updated_at=50,
    )
    await _job(db, "j_trash", kind="trash", file_id="f1", status="done", updated_at=40)
    await _job(db, "j_orphan", kind="rename", submission_id="s_gone", updated_at=60)

    res = await client.get(URL, headers=admin.headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["meta"] == {"version": "v1", "count": 3}
    assert [j["id"] for j in body["data"]] == ["j_orphan", "j_copy", "j_trash"]
    assert body["data"][1] == {
        "id": "j_copy",
        "kind": "copy",
        "status": "failed",
        "attempts": 10,
        "last_error": "Drive not configured",
        "next_attempt_at": 7,
        "created_at": 3,
        "updated_at": 50,
        "submission_id": sid,
        "file_id": None,
        "event_name": "Swingtacular 2027",
    }
    assert body["data"][0]["event_name"] is None
    assert body["data"][2]["event_name"] is None
    assert body["data"][2]["file_id"] == "f1"

    failed = await client.get(URL, params={"status": "failed"}, headers=admin.headers)
    assert [j["id"] for j in failed.json()["data"]] == ["j_copy"]
    running = await client.get(URL, params={"status": "running"}, headers=admin.headers)
    assert running.json() == {"data": [], "meta": {"version": "v1", "count": 0}}


async def test_list_limit(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    for i in range(55):
        await _job(db, f"j{i:02d}", updated_at=i)

    async def count(qs: str) -> int:
        res = await client.get(f"{URL}{qs}", headers=admin.headers)
        assert res.status_code == 200, (qs, res.text)
        return len(res.json()["data"])

    assert await count("") == 50
    assert await count("?limit=2") == 2
    # z.coerce.number() is Number(): whitespace, exponents, hex and 2.0 pass.
    assert await count("?limit=%202%20") == 2
    assert await count("?limit=1e1") == 10
    assert await count("?limit=0x3") == 3
    assert await count("?limit=4.0") == 4
    assert await count("?limit=200") == 55
    res = await client.get(f"{URL}?limit=1", headers=admin.headers)
    assert res.json()["data"][0]["id"] == "j54"


async def test_list_validates_the_query(
    client: httpx.AsyncClient, person: Callable
) -> None:
    admin = await person("admin", admin=True)
    for qs in (
        "status=queued",
        "limit=0",
        "limit=201",
        "limit=1.5",
        "limit=abc",
        "limit=",
        "limit=1_0",
        "limit=Infinity",
        "limit=1&limit=2",
    ):
        res = await client.get(f"{URL}?{qs}", headers=admin.headers)
        assert res.status_code == 400, qs
        assert res.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_list_failure_is_internal(
    client: httpx.AsyncClient, person: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await person("admin", admin=True)

    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(admin_drive_jobs, "success_list", boom)
    res = await client.get(URL, headers=admin.headers)
    assert res.status_code == 500
    assert res.json() == INTERNAL


# ---------------------------------------------------------------------------
# backfill-renames
# ---------------------------------------------------------------------------


async def test_backfill_enqueues_a_rename_per_copied_submission(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    await _submission(db, admin.id, "s1", copy="file_1")
    await _submission(db, admin.id, "s2", copy="file_2")
    await _submission(db, admin.id, "s3")

    res = await client.post(f"{URL}/backfill-renames", headers=admin.headers)
    assert res.status_code == 200, res.text
    assert res.json() == {"data": {"enqueued": 2}, "meta": {"version": "v1"}}
    assert await drive_jobs(db) == [
        ("rename", "s1", None, "pending"),
        ("rename", "s2", None, "pending"),
    ]
    row = await db.fetchrow(
        "SELECT attempts, next_attempt_at, created_at, updated_at FROM drive_jobs LIMIT 1"
    )
    assert row["attempts"] == 0
    assert row["next_attempt_at"] == row["created_at"] == row["updated_at"]
    assert abs(row["created_at"] - time.time() * 1000) < 60_000

    # Safe to repeat: it queues again.
    again = await client.post(f"{URL}/backfill-renames", headers=admin.headers)
    assert again.json()["data"] == {"enqueued": 2}
    assert len(await drive_jobs(db)) == 4


async def test_backfill_with_nothing_to_rename(
    client: httpx.AsyncClient, person: Callable
) -> None:
    admin = await person("admin", admin=True)
    res = await client.post(f"{URL}/backfill-renames", headers=admin.headers)
    assert res.json()["data"] == {"enqueued": 0}


async def test_backfill_failure_is_internal_and_keeps_earlier_jobs(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = await person("admin", admin=True)
    await _submission(db, admin.id, "s1", copy="file_1")
    await _submission(db, admin.id, "s2", copy="file_2")
    real = admin_drive_jobs.enqueue_drive_job
    calls = 0

    async def flaky(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("insert failed")
        await real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(admin_drive_jobs, "enqueue_drive_job", flaky)
    res = await client.post(f"{URL}/backfill-renames", headers=admin.headers)
    assert res.status_code == 500
    assert res.json() == INTERNAL
    assert len(await drive_jobs(db)) == 1


# ---------------------------------------------------------------------------
# retry
# ---------------------------------------------------------------------------


async def test_retry_a_failed_job(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    await _job(db, "j1", status="failed", attempts=10, last_error="boom")
    res = await client.post(f"{URL}/j1/retry", headers=admin.headers)
    assert res.status_code == 200, res.text
    assert res.json() == {
        "data": {"id": "j1", "status": "pending"},
        "meta": {"version": "v1"},
    }
    row = await db.fetchrow("SELECT * FROM drive_jobs WHERE id = 'j1'")
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert row["last_error"] == "boom"
    assert row["next_attempt_at"] == row["updated_at"]
    assert abs(row["updated_at"] - time.time() * 1000) < 60_000
    assert row["created_at"] == 3


async def test_retry_errors(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    res = await client.post(f"{URL}/nope/retry", headers=admin.headers)
    assert res.status_code == 404
    assert res.json() == {
        "error": {"code": "NOT_FOUND", "message": "Drive job not found"}
    }
    for status in ("pending", "running", "done"):
        await _job(db, f"j_{status}", status=status)
        res = await client.post(f"{URL}/j_{status}/retry", headers=admin.headers)
        assert res.status_code == 409
        assert res.json() == {
            "error": {
                "code": "conflict",
                "message": f"Job is {status}, not failed — only exhausted jobs "
                "can be retried.",
            }
        }
        assert (
            await db.fetchval(
                "SELECT status FROM drive_jobs WHERE id = $1", f"j_{status}"
            )
            == status
        )


async def test_retry_loses_a_race_safely(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The status changes between the read and the guarded update."""
    admin = await person("admin", admin=True)
    await _job(db, "j1", status="failed")

    async def claimed_by_tick() -> None:
        await db.execute("UPDATE drive_jobs SET status = 'running' WHERE id = 'j1'")

    original_execute = admin_drive_jobs.AsyncSession.execute
    seen = 0

    async def execute(self, statement, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal seen
        if getattr(statement, "is_dml", False) and "drive_jobs" in str(statement):
            seen += 1
            await claimed_by_tick()
        return await original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(admin_drive_jobs.AsyncSession, "execute", execute)
    res = await client.post(f"{URL}/j1/retry", headers=admin.headers)
    assert seen == 1
    assert res.status_code == 409
    assert res.json() == {
        "error": {
            "code": "conflict",
            "message": "Job changed state before it could be retried — re-check "
            "and try again.",
        }
    }
    assert (
        await db.fetchval("SELECT status FROM drive_jobs WHERE id = 'j1'") == "running"
    )
