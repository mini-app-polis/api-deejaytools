"""``process_drive_jobs`` against real Postgres (deejaytools-api DRIVE.md "Processing").

The behaviour cases are deejaytools-api's src/services/driveJobs.test.ts,
ported with the same inputs and expected outcomes (ADR-009 "golden cases are
ported"); there the database was mocked, here claims, leases and SKIP LOCKED
run for real. Drive is mocked: most cases replace the Drive layer's functions,
and the fixed-defect cases run the real Drive layer over the in-memory fake
service so copy tagging and reuse are exercised end to end.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api_deejaytools.config import get_settings
from api_deejaytools.database import _get_sessionmaker
from api_deejaytools.services import drive, drive_jobs
from api_deejaytools.services.drive import EventCopyResult

from .conftest import TEST_DATABASE_URL
from .drive_fakes import FakeDriveService, fake_facade

MINUTE = 60_000


def now_ms() -> int:
    return int(time.time() * 1000)


# --- fakes ----------------------------------------------------------------------


class Logs:
    """Stands in for the module logger; keeps (level, message) pairs."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def info(self, msg: str, *a: Any, **k: Any) -> None:
        self.lines.append(("info", msg))

    def warning(self, msg: str, *a: Any, **k: Any) -> None:
        self.lines.append(("warning", msg))

    def error(self, msg: str, *a: Any, **k: Any) -> None:
        self.lines.append(("error", msg))

    def at(self, level: str, event: str) -> list[str]:
        return [m for lvl, m in self.lines if lvl == level and f" {event}" in f" {m}"]

    def any(self, event: str) -> list[str]:
        return [m for _, m in self.lines if event in m]


class Scope:
    def __init__(self) -> None:
        self.level: str | None = None
        self.tags: dict[str, str] = {}
        self.contexts: dict[str, Any] = {}

    def set_level(self, level: str) -> None:
        self.level = level

    def set_tag(self, k: str, v: str) -> None:
        self.tags[k] = v

    def set_context(self, k: str, v: Any) -> None:
        self.contexts[k] = v


class Sentry:
    """Stands in for sentry_sdk: each capture records the scope it ran in."""

    def __init__(self) -> None:
        self.captured: list[tuple[BaseException, Scope]] = []
        self._scope: Scope | None = None

    @contextlib.contextmanager
    def new_scope(self) -> Iterator[Scope]:
        self._scope = Scope()
        try:
            yield self._scope
        finally:
            self._scope = None

    def capture_exception(self, exc: BaseException) -> None:
        self.captured.append((exc, self._scope or Scope()))


class FakeDrive:
    """Replaces the Drive layer's copy / rename / soft-delete for a test."""

    def __init__(self) -> None:
        self.copies: list[tuple[str, dict[str, Any]]] = []
        self.renames: list[tuple[str, str]] = []
        self.soft_deletes: list[str] = []
        self.copy_error: Exception | None = None
        self.copy_result = EventCopyResult("copy_file_1", "copy_folder_1", reused=False)

    async def copy_song_to_event_folder(
        self, source: str, **kw: Any
    ) -> EventCopyResult:
        self.copies.append((source, kw))
        if self.copy_error is not None:
            raise self.copy_error
        return self.copy_result

    async def rename_file(self, file_id: str, name: str) -> bool:
        self.renames.append((file_id, name))
        return True

    async def soft_delete(self, file_id: str) -> None:
        self.soft_deletes.append(file_id)


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> Logs:
    rec = Logs()
    monkeypatch.setattr(drive_jobs, "logger", rec)
    return rec


@pytest.fixture
def sentry(monkeypatch: pytest.MonkeyPatch) -> Sentry:
    rec = Sentry()
    monkeypatch.setattr(drive_jobs, "sentry_sdk", rec)
    return rec


@pytest.fixture
def fake_drive(monkeypatch: pytest.MonkeyPatch) -> FakeDrive:
    fd = FakeDrive()
    monkeypatch.setattr(
        drive, "copy_song_to_event_folder", fd.copy_song_to_event_folder
    )
    monkeypatch.setattr(drive, "rename_file", fd.rename_file)
    monkeypatch.setattr(drive, "soft_delete", fd.soft_delete)
    return fd


@pytest.fixture
def real_drive(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDriveService]:
    """The real Drive layer over the in-memory fake Google service."""
    settings = get_settings()
    monkeypatch.setattr(settings, "GOOGLE_SERVICE_ACCOUNT_EMAIL", "sa@x.iam")
    monkeypatch.setattr(settings, "GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY", "KEY")
    monkeypatch.setattr(settings, "GOOGLE_DRIVE_PARENT_FOLDER_ID", "root")
    facade, svc = fake_facade()
    monkeypatch.setattr(drive, "_build_facade", lambda email, key: facade)
    drive.clear_drive_folder_cache()
    yield svc
    drive.clear_drive_folder_cache()


@pytest.fixture
async def session(db: asyncpg.Connection) -> AsyncIterator[AsyncSession]:
    async with _get_sessionmaker(TEST_DATABASE_URL)() as s:
        yield s


# --- seed helpers -----------------------------------------------------------------


async def seed_submission(
    conn: asyncpg.Connection,
    *,
    submission_id: str = "sub_1",
    already_copied: str | None = None,
    song_id: str = "song_1",
    drive_file_id: str | None = "source_file_1",
    original_filename: str | None = "my track.mp3",
    processed_filename: str | None = "2026_Classic.mp3",
    division: str | None = "Classic",
    submission_division: str | None = None,
    submission_round: str | None = None,
    event_name: str = "Spring Classic",
    event_season_year: str | None = "2027",
    event_start_date: str = "2026-11-25",
) -> None:
    """makeCopySubmissionRow's defaults, as real rows."""
    t = now_ms()
    await conn.execute(
        "INSERT INTO users (id, email, created_at, updated_at) VALUES ($1, $2, $3, $3) "
        "ON CONFLICT DO NOTHING",
        "user_1",
        "u@example.test",
        t,
    )
    await conn.execute(
        "INSERT INTO events (id, name, start_date, end_date, season_year, "
        "created_at, updated_at) VALUES ($1, $2, $3, $3, $4, $5, $5) "
        "ON CONFLICT DO NOTHING",
        f"event_{submission_id}",
        event_name,
        event_start_date,
        event_season_year,
        t,
    )
    await conn.execute(
        "INSERT INTO songs (id, user_id, drive_file_id, original_filename, "
        "processed_filename, division, created_at, updated_at) "
        "VALUES ($1, 'user_1', $2, $3, $4, $5, $6, $6) ON CONFLICT DO NOTHING",
        song_id,
        drive_file_id,
        original_filename,
        processed_filename,
        division,
        t,
    )
    await conn.execute(
        "INSERT INTO event_song_submissions (id, event_id, song_id, "
        "submitted_by_user_id, drive_copy_file_id, division, round, created_at) "
        "VALUES ($1, $2, $3, 'user_1', $4, $5, $6, $7)",
        submission_id,
        f"event_{submission_id}",
        song_id,
        already_copied,
        submission_division,
        submission_round,
        t,
    )


async def add_job(
    conn: asyncpg.Connection,
    *,
    job_id: str = "job_1",
    kind: str = "copy",
    submission_id: str | None = "sub_1",
    file_id: str | None = None,
    status: str = "pending",
    attempts: int = 0,
    next_attempt_at: int | None = None,
    updated_at: int | None = None,
) -> None:
    t = now_ms()
    await conn.execute(
        "INSERT INTO drive_jobs (id, kind, submission_id, file_id, status, attempts, "
        "next_attempt_at, created_at, updated_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
        job_id,
        kind,
        submission_id,
        file_id,
        status,
        attempts,
        t if next_attempt_at is None else next_attempt_at,
        t,
        t if updated_at is None else updated_at,
    )


async def job(conn: asyncpg.Connection, job_id: str = "job_1") -> asyncpg.Record:
    row = await conn.fetchrow("SELECT * FROM drive_jobs WHERE id = $1", job_id)
    assert row is not None
    return row


async def copy_id(conn: asyncpg.Connection, submission_id: str = "sub_1") -> Any:
    return await conn.fetchval(
        "SELECT drive_copy_file_id FROM event_song_submissions WHERE id = $1",
        submission_id,
    )


# --- backoff and constants ----------------------------------------------------------


def test_backoff_grows_and_caps_at_30_minutes() -> None:
    assert drive_jobs.backoff_ms(0) == 60_000
    assert drive_jobs.backoff_ms(1) == 120_000
    assert drive_jobs.backoff_ms(2) == 240_000
    assert drive_jobs.backoff_ms(10) == 30 * MINUTE
    assert drive_jobs.backoff_ms(4) == 960_000
    assert drive_jobs.backoff_ms(9) == 1_800_000


def test_lease_outlasts_a_slow_drive_call() -> None:
    assert drive_jobs.LEASE_TIMEOUT_MS >= 10 * MINUTE
    assert drive_jobs.MAX_ATTEMPTS == 10
    assert drive_jobs.DEFAULT_BATCH_SIZE == 10


# --- ported behaviour cases -------------------------------------------------------


async def test_claim_failure_returns_zero_and_reports(
    db: asyncpg.Connection,
    session: AsyncSession,
    fake_drive: FakeDrive,
    sentry: Sentry,
    logs: Logs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import text

    await add_job(db)
    monkeypatch.setattr(drive_jobs, "_CLAIM_SQL", text("SELECT * FROM no_such_table"))
    result = await drive_jobs.process_drive_jobs(session)
    assert result == {"claimed": 0, "succeeded": 0, "failed": 0}
    assert fake_drive.copies == []
    assert len(sentry.captured) == 1
    assert sentry.captured[0][1].tags == {"subsystem": "drive_jobs"}
    assert logs.at("error", "drive_jobs_claim_failed")


async def test_already_copied_makes_no_drive_call(
    db: asyncpg.Connection, session: AsyncSession, fake_drive: FakeDrive, logs: Logs
) -> None:
    await seed_submission(db, already_copied="existing_copy")
    await add_job(db)
    result = await drive_jobs.process_drive_jobs(session)
    assert result["succeeded"] == 1
    assert fake_drive.copies == []
    assert await copy_id(db) == "existing_copy"


async def test_no_source_file_is_done_without_drive(
    db: asyncpg.Connection, session: AsyncSession, fake_drive: FakeDrive, logs: Logs
) -> None:
    await seed_submission(db, drive_file_id=None)
    await add_job(db)
    assert (await drive_jobs.process_drive_jobs(session))["succeeded"] == 1
    assert fake_drive.copies == []
    assert (await job(db))["status"] == "done"
    assert logs.at("warning", "drive_copy_skipped_no_source")


async def test_submission_gone_is_done(
    db: asyncpg.Connection, session: AsyncSession, fake_drive: FakeDrive, logs: Logs
) -> None:
    await add_job(db, submission_id="missing")
    assert (await drive_jobs.process_drive_jobs(session))["succeeded"] == 1
    assert fake_drive.copies == []
    assert (await job(db))["status"] == "done"


async def test_successful_copy_records_the_copy_id(
    db: asyncpg.Connection, session: AsyncSession, fake_drive: FakeDrive, logs: Logs
) -> None:
    await seed_submission(db)
    await add_job(db)
    assert (await drive_jobs.process_drive_jobs(session))["succeeded"] == 1
    assert fake_drive.copies == [
        (
            "source_file_1",
            {
                "submission_id": "sub_1",
                "filename": "2026_Classic.mp3",
                "season_year": "2027",
                "event_name": "Spring Classic",
                "division": "Classic",
                "subfolder": None,
            },
        )
    ]
    assert await copy_id(db) == "copy_file_1"
    row = await job(db)
    assert row["status"] == "done"
    assert row["last_error"] is None


async def test_superseded_lease_is_not_marked_done(
    db: asyncpg.Connection,
    session: AsyncSession,
    fake_drive: FakeDrive,
    logs: Logs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_submission(db)
    await add_job(db)
    original = fake_drive.copy_song_to_event_folder

    async def copy_then_lose_lease(source: str, **kw: Any) -> EventCopyResult:
        # The lease was reclaimed while this run was in flight.
        await db.execute("UPDATE drive_jobs SET status = 'pending' WHERE id = 'job_1'")
        return await original(source, **kw)

    monkeypatch.setattr(drive, "copy_song_to_event_folder", copy_then_lose_lease)
    result = await drive_jobs.process_drive_jobs(session)
    assert result == {"claimed": 1, "succeeded": 0, "failed": 1}
    assert (await job(db))["status"] == "pending"
    assert logs.at("warning", "drive_job_completion_superseded")


@pytest.mark.parametrize(
    ("round_", "subfolder"),
    [
        ("finals_only", "Finals"),
        ("prelims_only", "Prelims"),
        ("prelims_and_finals", None),
        (None, None),
    ],
)
async def test_round_picks_the_subfolder(
    db: asyncpg.Connection,
    session: AsyncSession,
    fake_drive: FakeDrive,
    logs: Logs,
    round_: str | None,
    subfolder: str | None,
) -> None:
    await seed_submission(db, submission_round=round_)
    await add_job(db)
    assert (await drive_jobs.process_drive_jobs(session))["succeeded"] == 1
    assert fake_drive.copies[0][1]["subfolder"] == subfolder


async def test_submission_division_wins_over_the_song(
    db: asyncpg.Connection, session: AsyncSession, fake_drive: FakeDrive, logs: Logs
) -> None:
    await seed_submission(db, division="Classic", submission_division="Showcase")
    await add_job(db)
    await drive_jobs.process_drive_jobs(session)
    assert fake_drive.copies[0][1]["division"] == "Showcase"


async def test_blank_division_is_unknown(
    db: asyncpg.Connection, session: AsyncSession, fake_drive: FakeDrive, logs: Logs
) -> None:
    await seed_submission(db, division="  ", submission_division=None)
    await add_job(db)
    await drive_jobs.process_drive_jobs(session)
    assert fake_drive.copies[0][1]["division"] == "unknown"


async def test_event_season_year_is_used_not_the_songs(
    db: asyncpg.Connection, session: AsyncSession, fake_drive: FakeDrive, logs: Logs
) -> None:
    await seed_submission(db, event_season_year="2028", event_start_date="2027-09-01")
    await add_job(db)
    await drive_jobs.process_drive_jobs(session)
    assert fake_drive.copies[0][1]["season_year"] == "2028"


async def test_season_year_from_start_date_when_null(
    db: asyncpg.Connection, session: AsyncSession, fake_drive: FakeDrive, logs: Logs
) -> None:
    await seed_submission(db, event_season_year=None, event_start_date="2026-10-15")
    await add_job(db)
    await drive_jobs.process_drive_jobs(session)
    assert fake_drive.copies[0][1]["season_year"] == "2027"


async def test_failing_copy_is_rescheduled_without_sentry(
    db: asyncpg.Connection,
    session: AsyncSession,
    fake_drive: FakeDrive,
    sentry: Sentry,
    logs: Logs,
) -> None:
    fake_drive.copy_error = RuntimeError("Drive down")
    await seed_submission(db)
    await add_job(db, attempts=1)
    before = now_ms()
    result = await drive_jobs.process_drive_jobs(session)
    assert result["succeeded"] == 0
    row = await job(db)
    assert (row["status"], row["attempts"], row["last_error"]) == (
        "pending",
        2,
        "Drive down",
    )
    # backoff(2) = 4 minutes after the failure.
    assert before + 4 * MINUTE <= row["next_attempt_at"] <= now_ms() + 4 * MINUTE
    assert sentry.captured == []


async def test_exhausted_copy_is_failed_and_reported_once(
    db: asyncpg.Connection,
    session: AsyncSession,
    fake_drive: FakeDrive,
    sentry: Sentry,
    logs: Logs,
) -> None:
    fake_drive.copy_error = RuntimeError("Drive down")
    await seed_submission(db)
    await add_job(db, attempts=drive_jobs.MAX_ATTEMPTS - 1)
    assert (await drive_jobs.process_drive_jobs(session))["succeeded"] == 0
    row = await job(db)
    assert (row["status"], row["attempts"], row["last_error"]) == (
        "failed",
        drive_jobs.MAX_ATTEMPTS,
        "Drive down",
    )
    assert len(sentry.captured) == 1
    exc, scope = sentry.captured[0]
    assert str(exc) == "Drive down"
    assert scope.tags == {"subsystem": "drive_jobs", "drive_job_kind": "copy"}
    assert scope.contexts["drive_job"] == {
        "job_id": "job_1",
        "kind": "copy",
        "submission_id": "sub_1",
        "file_id": None,
        "attempts": 10,
    }
    assert logs.at("error", "drive_job_exhausted")


async def test_trash_job_soft_deletes_the_file(
    db: asyncpg.Connection, session: AsyncSession, fake_drive: FakeDrive, logs: Logs
) -> None:
    await add_job(db, kind="trash", submission_id=None, file_id="copy_to_trash")
    assert (await drive_jobs.process_drive_jobs(session))["succeeded"] == 1
    assert fake_drive.soft_deletes == ["copy_to_trash"]
    assert fake_drive.copies == []
    assert (await job(db))["status"] == "done"


async def test_rename_job_renames_the_copy(
    db: asyncpg.Connection, session: AsyncSession, fake_drive: FakeDrive, logs: Logs
) -> None:
    await seed_submission(db, already_copied="copy_file_1")
    await add_job(db, kind="rename")
    assert (await drive_jobs.process_drive_jobs(session))["succeeded"] == 1
    assert fake_drive.renames == [("copy_file_1", "2026_Classic.mp3")]
    assert fake_drive.copies == []
    assert (await job(db))["status"] == "done"
    assert logs.at("info", "drive_rename_succeeded")


async def test_rename_without_a_copy_is_done_without_drive(
    db: asyncpg.Connection, session: AsyncSession, fake_drive: FakeDrive, logs: Logs
) -> None:
    await seed_submission(db, already_copied=None)
    await add_job(db, kind="rename")
    assert (await drive_jobs.process_drive_jobs(session))["succeeded"] == 1
    assert fake_drive.renames == []
    assert (await job(db))["status"] == "done"


async def test_rename_for_a_gone_submission_is_done(
    db: asyncpg.Connection, session: AsyncSession, fake_drive: FakeDrive, logs: Logs
) -> None:
    await add_job(db, kind="rename", submission_id="missing")
    assert (await drive_jobs.process_drive_jobs(session))["succeeded"] == 1
    assert fake_drive.renames == []
    assert (await job(db))["status"] == "done"


async def test_rename_without_submission_id_is_rescheduled_with_row_keys(
    db: asyncpg.Connection, session: AsyncSession, fake_drive: FakeDrive, logs: Logs
) -> None:
    await add_job(db, kind="rename", submission_id=None)
    assert (await drive_jobs.process_drive_jobs(session))["succeeded"] == 0
    assert fake_drive.renames == []
    last_error = (await job(db))["last_error"]
    assert "rename job job_1 has no submission_id" in last_error
    assert "row keys:" in last_error


async def test_copy_and_trash_without_their_field_are_rescheduled(
    db: asyncpg.Connection, session: AsyncSession, fake_drive: FakeDrive, logs: Logs
) -> None:
    await add_job(db, job_id="c", kind="copy", submission_id=None)
    await add_job(db, job_id="t", kind="trash", submission_id=None, file_id=None)
    await drive_jobs.process_drive_jobs(session)
    assert "copy job c has no submission_id" in (await job(db, "c"))["last_error"]
    assert "trash job t has no file_id" in (await job(db, "t"))["last_error"]


async def test_unknown_kind_is_rescheduled(
    db: asyncpg.Connection, session: AsyncSession, fake_drive: FakeDrive, logs: Logs
) -> None:
    await add_job(db, kind="explode")
    await drive_jobs.process_drive_jobs(session)
    assert (await job(db))["last_error"] == "unknown drive job kind: explode"


async def test_stuck_running_jobs_are_reclaimed_without_an_attempt(
    db: asyncpg.Connection, session: AsyncSession, fake_drive: FakeDrive, logs: Logs
) -> None:
    t = now_ms()
    # Not due again yet, so the same pass does not re-claim it.
    await add_job(
        db,
        job_id="stuck_job",
        status="running",
        attempts=3,
        next_attempt_at=t + 60 * MINUTE,
        updated_at=t - 11 * MINUTE,
    )
    await drive_jobs.process_drive_jobs(session)
    row = await job(db, "stuck_job")
    assert (row["status"], row["attempts"]) == ("pending", 3)
    assert row["updated_at"] >= t
    assert logs.at("warning", "drive_jobs_reclaimed")


async def test_reclaimed_job_that_is_due_runs_in_the_same_pass(
    db: asyncpg.Connection, session: AsyncSession, fake_drive: FakeDrive, logs: Logs
) -> None:
    await seed_submission(db)
    await add_job(db, status="running", updated_at=now_ms() - 11 * MINUTE)
    assert (await drive_jobs.process_drive_jobs(session))["succeeded"] == 1
    assert (await job(db))["status"] == "done"


async def test_fresh_running_leases_are_left_alone(
    db: asyncpg.Connection, session: AsyncSession, fake_drive: FakeDrive, logs: Logs
) -> None:
    await add_job(db, job_id="live", status="running", updated_at=now_ms() - 5 * MINUTE)
    await drive_jobs.process_drive_jobs(session)
    assert (await job(db, "live"))["status"] == "running"
    assert not logs.any("drive_jobs_reclaimed")


async def test_claims_even_when_reclaim_fails(
    db: asyncpg.Connection,
    session: AsyncSession,
    fake_drive: FakeDrive,
    sentry: Sentry,
    logs: Logs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(*_: Any) -> int:
        raise RuntimeError("reclaim failed")

    monkeypatch.setattr(drive_jobs, "_reclaim_stuck_jobs", broken)
    await seed_submission(db)
    await add_job(db)
    assert (await drive_jobs.process_drive_jobs(session))["succeeded"] == 1
    assert sentry.captured == []
    assert logs.at("error", "drive_jobs_reclaim_failed")


async def test_retry_log_carries_error_message_and_attempts(
    db: asyncpg.Connection, session: AsyncSession, fake_drive: FakeDrive, logs: Logs
) -> None:
    fake_drive.copy_error = RuntimeError("Drive down")
    await seed_submission(db)
    await add_job(db, attempts=1)
    await drive_jobs.process_drive_jobs(session)
    (line,) = logs.at("warning", "drive_job_retrying")
    assert "error_message='Drive down'" in line
    assert "attempts=2 " in line
    assert "next_attempt_at=20" in line


async def test_first_failure_logs_at_error_later_ones_at_warn(
    db: asyncpg.Connection, session: AsyncSession, fake_drive: FakeDrive, logs: Logs
) -> None:
    fake_drive.copy_error = RuntimeError("Drive down")
    await seed_submission(db)
    await add_job(db, attempts=0)
    await drive_jobs.process_drive_jobs(session)
    assert len(logs.at("error", "drive_job_retrying")) == 1
    assert logs.at("warning", "drive_job_retrying") == []

    logs.lines.clear()
    await db.execute("UPDATE drive_jobs SET next_attempt_at = 0 WHERE id = 'job_1'")
    await drive_jobs.process_drive_jobs(session)
    (line,) = logs.at("warning", "drive_job_retrying")
    assert "attempts=2 " in line
    assert logs.at("error", "drive_job_retrying") == []


async def test_processed_is_logged_with_counts(
    db: asyncpg.Connection, session: AsyncSession, fake_drive: FakeDrive, logs: Logs
) -> None:
    await seed_submission(db)
    await add_job(db)
    await drive_jobs.process_drive_jobs(session)
    (line,) = logs.at("info", "drive_jobs_processed")
    assert "claimed=1 succeeded=1 failed=0" in line


async def test_nothing_claimed_logs_nothing_processed(
    db: asyncpg.Connection, session: AsyncSession, fake_drive: FakeDrive, logs: Logs
) -> None:
    result = await drive_jobs.process_drive_jobs(session)
    assert result == {"claimed": 0, "succeeded": 0, "failed": 0}
    assert logs.any("drive_jobs_processed") == []


# --- claiming, for real -----------------------------------------------------------


async def test_claims_at_most_ten_due_jobs_oldest_first(
    db: asyncpg.Connection, session: AsyncSession, fake_drive: FakeDrive, logs: Logs
) -> None:
    t = now_ms()
    for i in range(12):
        await add_job(
            db,
            job_id=f"j{i:02d}",
            kind="trash",
            submission_id=None,
            file_id=f"f{i:02d}",
            next_attempt_at=t - 1000 * (20 - i),
        )
    await add_job(
        db,
        job_id="later",
        kind="trash",
        submission_id=None,
        file_id="x",
        next_attempt_at=t + MINUTE,
    )
    result = await drive_jobs.process_drive_jobs(session)
    assert result["claimed"] == 10
    assert fake_drive.soft_deletes == [f"f{i:02d}" for i in range(10)]
    statuses = {
        r["id"]: r["status"]
        for r in await db.fetch("SELECT id, status FROM drive_jobs")
    }
    assert statuses["j10"] == statuses["j11"] == statuses["later"] == "pending"


async def test_skip_locked_leaves_rows_another_claimer_holds(
    db: asyncpg.Connection, session: AsyncSession, fake_drive: FakeDrive, logs: Logs
) -> None:
    for name in ("held", "free"):
        await add_job(db, job_id=name, kind="trash", submission_id=None, file_id=name)
    other = await asyncpg.connect(TEST_DATABASE_URL)
    try:
        tx = other.transaction()
        await tx.start()
        await other.execute("SELECT id FROM drive_jobs WHERE id = 'held' FOR UPDATE")
        result = await drive_jobs.process_drive_jobs(session)
        await tx.rollback()
    finally:
        await other.close()
    assert result["claimed"] == 1
    assert fake_drive.soft_deletes == ["free"]
    assert (await job(db, "held"))["status"] == "pending"


async def test_concurrent_passes_take_disjoint_batches(
    db: asyncpg.Connection, fake_drive: FakeDrive, logs: Logs
) -> None:
    import asyncio

    for i in range(6):
        await add_job(
            db, job_id=f"j{i}", kind="trash", submission_id=None, file_id=f"f{i}"
        )
    maker = _get_sessionmaker(TEST_DATABASE_URL)
    async with maker() as a, maker() as b:
        ra, rb = await asyncio.gather(
            drive_jobs.process_drive_jobs(a, limit=4),
            drive_jobs.process_drive_jobs(b, limit=4),
        )
    assert ra["claimed"] + rb["claimed"] == 6
    assert sorted(fake_drive.soft_deletes) == [f"f{i}" for i in range(6)]


# --- fixed defects (DRIVE.md "Known defects") ---------------------------------------


def tagged_copies(svc: FakeDriveService, submission_id: str) -> list[str]:
    return [
        fid
        for fid, f in svc.files_by_id.items()
        if f["appProperties"].get("deejaytools_submission_id") == submission_id
    ]


async def test_fixed_copy_then_db_failure_reuses_the_copy_on_retry(
    db: asyncpg.Connection,
    session: AsyncSession,
    real_drive: FakeDriveService,
    logs: Logs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = real_drive.add_file("2026_Classic.mp3", ["x"])
    await seed_submission(db, drive_file_id=source)
    await add_job(db)
    real_record = drive_jobs._record_copy

    async def db_down(*_: Any, **__: Any) -> None:
        raise RuntimeError("connection reset")

    monkeypatch.setattr(drive_jobs, "_record_copy", db_down)
    await drive_jobs.process_drive_jobs(session)
    assert (await job(db))["status"] == "pending"
    assert len(tagged_copies(real_drive, "sub_1")) == 1

    monkeypatch.setattr(drive_jobs, "_record_copy", real_record)
    await db.execute("UPDATE drive_jobs SET next_attempt_at = 0 WHERE id = 'job_1'")
    assert (await drive_jobs.process_drive_jobs(session))["succeeded"] == 1

    copies = tagged_copies(real_drive, "sub_1")
    assert len(copies) == 1
    assert len(real_drive.ops("files.copy")) == 1
    assert await copy_id(db) == copies[0]


async def test_fixed_overlapping_runs_make_one_copy(
    db: asyncpg.Connection,
    session: AsyncSession,
    real_drive: FakeDriveService,
    logs: Logs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = real_drive.add_file("2026_Classic.mp3", ["x"])
    await seed_submission(db, drive_file_id=source)
    await add_job(db)
    real_record = drive_jobs._record_copy
    overlapped: list[dict[str, int]] = []
    started_b: list[bool] = []

    async def slow_record(
        db_: AsyncSession, sub: str, fid: str, *, reused: bool
    ) -> None:
        if not started_b:
            started_b.append(True)
            # Run A hung after copying; its lease is reclaimed and run B does
            # the whole job before A records anything.
            await db.execute(
                "UPDATE drive_jobs SET status = 'pending', next_attempt_at = 0 "
                "WHERE id = 'job_1'"
            )
            async with _get_sessionmaker(TEST_DATABASE_URL)() as other:
                overlapped.append(await drive_jobs.process_drive_jobs(other))
        await real_record(db_, sub, fid, reused=reused)

    monkeypatch.setattr(drive_jobs, "_record_copy", slow_record)
    result = await drive_jobs.process_drive_jobs(session)

    assert overlapped == [{"claimed": 1, "succeeded": 1, "failed": 0}]
    assert result["succeeded"] == 0  # A's completion was superseded
    copies = tagged_copies(real_drive, "sub_1")
    assert len(copies) == 1
    assert len(real_drive.ops("files.copy")) == 1
    assert await copy_id(db) == copies[0]
    # The surviving copy is the recorded one: nothing was discarded.
    assert real_drive.folder_id("root", "_deprecated") is None


async def test_fixed_copy_after_submission_deleted_is_soft_deleted(
    db: asyncpg.Connection,
    session: AsyncSession,
    real_drive: FakeDriveService,
    logs: Logs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = real_drive.add_file("2026_Classic.mp3", ["x"])
    await seed_submission(db, drive_file_id=source)
    await add_job(db)
    real_copy = drive.copy_song_to_event_folder

    async def copy_then_delete(*a: Any, **kw: Any) -> EventCopyResult:
        result = await real_copy(*a, **kw)
        await db.execute("DELETE FROM event_song_submissions WHERE id = 'sub_1'")
        return result

    monkeypatch.setattr(drive, "copy_song_to_event_folder", copy_then_delete)
    assert (await drive_jobs.process_drive_jobs(session))["succeeded"] == 1

    (orphan,) = tagged_copies(real_drive, "sub_1")
    deprecated = real_drive.folder_id("root", "_deprecated")
    assert real_drive.files_by_id[orphan]["parents"] == [deprecated]
    assert logs.at("warning", "drive_copy_discarded")
    assert "reason=submission_deleted" in logs.at("warning", "drive_copy_discarded")[0]


async def test_fixed_copy_losing_to_another_recorded_copy_is_soft_deleted(
    db: asyncpg.Connection,
    session: AsyncSession,
    real_drive: FakeDriveService,
    logs: Logs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = real_drive.add_file("2026_Classic.mp3", ["x"])
    winner = real_drive.add_file("winner.mp3", ["elsewhere"])
    await seed_submission(db, drive_file_id=source)
    await add_job(db)
    real_copy = drive.copy_song_to_event_folder

    async def copy_then_other_records(*a: Any, **kw: Any) -> EventCopyResult:
        result = await real_copy(*a, **kw)
        await db.execute(
            "UPDATE event_song_submissions SET drive_copy_file_id = $1 "
            "WHERE id = 'sub_1'",
            winner,
        )
        return result

    monkeypatch.setattr(drive, "copy_song_to_event_folder", copy_then_other_records)
    await drive_jobs.process_drive_jobs(session)

    assert await copy_id(db) == winner
    (loser,) = tagged_copies(real_drive, "sub_1")
    deprecated = real_drive.folder_id("root", "_deprecated")
    assert real_drive.files_by_id[loser]["parents"] == [deprecated]
    assert real_drive.files_by_id[winner]["parents"] == ["elsewhere"]
    assert "reason=other_copy_recorded" in logs.at("warning", "drive_copy_discarded")[0]


async def test_fixed_discard_falls_back_to_a_trash_job(
    db: asyncpg.Connection,
    session: AsyncSession,
    fake_drive: FakeDrive,
    logs: Logs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_submission(db)
    await add_job(db)
    original = fake_drive.copy_song_to_event_folder

    async def copy_then_delete(source: str, **kw: Any) -> EventCopyResult:
        result = await original(source, **kw)
        await db.execute("DELETE FROM event_song_submissions WHERE id = 'sub_1'")
        return result

    async def drive_down(file_id: str) -> None:
        raise RuntimeError("Drive down")

    monkeypatch.setattr(drive, "copy_song_to_event_folder", copy_then_delete)
    monkeypatch.setattr(drive, "soft_delete", drive_down)
    assert (await drive_jobs.process_drive_jobs(session))["succeeded"] == 1
    trash = await db.fetch(
        "SELECT * FROM drive_jobs WHERE kind = 'trash' AND file_id = 'copy_file_1'"
    )
    assert len(trash) == 1
    assert trash[0]["status"] == "pending"
    assert logs.at("error", "drive_copy_discard_failed")


async def test_fixed_failure_update_error_does_not_strand_the_batch(
    db: asyncpg.Connection,
    session: AsyncSession,
    fake_drive: FakeDrive,
    sentry: Sentry,
    logs: Logs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t = now_ms()
    await add_job(
        db, job_id="a", kind="copy", submission_id=None, next_attempt_at=t - 2
    )
    await add_job(
        db, job_id="b", kind="copy", submission_id=None, next_attempt_at=t - 1
    )
    await add_job(
        db, job_id="c", kind="trash", submission_id=None, file_id="f", next_attempt_at=t
    )
    real_execute = session.execute

    async def flaky_execute(stmt: Any, params: Any = None, *a: Any, **kw: Any) -> Any:
        if (
            isinstance(params, dict)
            and params.get("id") == "a"
            and "attempts" in params
        ):
            raise RuntimeError("db went away")
        return await real_execute(stmt, params, *a, **kw)

    monkeypatch.setattr(session, "execute", flaky_execute)
    result = await drive_jobs.process_drive_jobs(session)

    assert result == {"claimed": 3, "succeeded": 1, "failed": 2}
    # a: its failure could not be recorded; the lease will reclaim it.
    assert (await job(db, "a"))["status"] == "running"
    # b and c ran and were recorded after a's failed update.
    row_b = await job(db, "b")
    assert (row_b["status"], row_b["attempts"]) == ("pending", 1)
    assert (await job(db, "c"))["status"] == "done"
    assert logs.at("error", "drive_job_failure_update_failed")
    assert logs.at("info", "drive_jobs_processed")
