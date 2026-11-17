"""The scheduler's song-length step: songs with a Drive file and no length
are measured a few at a time, those never tried first; a file that cannot be
read waits a day (services/song_lengths.py)."""

from __future__ import annotations

import io
import wave
from collections.abc import AsyncIterator, Callable

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api_deejaytools.clock import now_ms
from api_deejaytools.database import _get_sessionmaker
from api_deejaytools.services import drive, song_lengths

from .conftest import TEST_DATABASE_URL
from .content_helpers import insert_song

DAY_MS = song_lengths.RETRY_AFTER_MS


def _wav(seconds: float) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(seconds * 8000))
    return buf.getvalue()


@pytest.fixture
async def session(db: asyncpg.Connection) -> AsyncIterator[AsyncSession]:
    async with _get_sessionmaker(TEST_DATABASE_URL)() as s:
        yield s


@pytest.fixture
def files(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    """Drive's files by id; a missing id fails the download like Drive."""
    stored: dict[str, bytes] = {}

    async def download(file_id: str) -> bytes:
        if file_id not in stored:
            raise RuntimeError(f"File not found: {file_id}")
        return stored[file_id]

    monkeypatch.setattr(drive, "download_file", download)
    return stored


async def _lengths(db: asyncpg.Connection) -> dict[str, tuple[int | None, bool]]:
    rows = await db.fetch("SELECT id, duration_ms, duration_checked_at FROM songs")
    return {
        r["id"]: (r["duration_ms"], r["duration_checked_at"] is not None) for r in rows
    }


async def test_songs_are_measured_a_batch_at_a_time_oldest_first(
    session: AsyncSession,
    db: asyncpg.Connection,
    person: Callable,
    files: dict[str, bytes],
) -> None:
    alice = await person("alice")
    for n in range(3):
        files[f"f{n}"] = _wav(n + 1)
        await insert_song(
            db, alice.id, song_id=f"s{n}", drive_file_id=f"f{n}", created_at=n + 1
        )
    # Never measured: no file yet (still building), or soft-deleted.
    await insert_song(db, alice.id, song_id="building")
    await insert_song(db, alice.id, song_id="deleted", drive_file_id="f0")
    await db.execute("UPDATE songs SET deleted_at = 1 WHERE id = 'deleted'")

    assert await song_lengths.measure_song_lengths(session, limit=2) == 2
    lengths = await _lengths(db)
    assert lengths["s0"] == (1000, True)
    assert lengths["s1"] == (2000, True)
    assert lengths["s2"] == (None, False)

    assert await song_lengths.measure_song_lengths(session, limit=2) == 1
    lengths = await _lengths(db)
    assert lengths["s2"] == (3000, True)
    assert lengths["building"] == (None, False)
    assert lengths["deleted"] == (None, False)

    # Nothing left: a measured song is never read again.
    assert await song_lengths.measure_song_lengths(session, limit=2) == 0


async def test_an_unreadable_file_waits_a_day_behind_untried_songs(
    session: AsyncSession,
    db: asyncpg.Connection,
    person: Callable,
    files: dict[str, bytes],
) -> None:
    alice = await person("alice")
    files["junk"] = b"not audio at all, just some text"
    files["good"] = _wav(2)
    await insert_song(db, alice.id, song_id="junk", drive_file_id="junk", created_at=1)
    await insert_song(
        db, alice.id, song_id="missing", drive_file_id="gone", created_at=2
    )
    await insert_song(db, alice.id, song_id="good", drive_file_id="good", created_at=3)

    # Neither the unparseable file nor the one Drive refuses raises.
    assert await song_lengths.measure_song_lengths(session, limit=2) == 0
    lengths = await _lengths(db)
    assert lengths["junk"] == (None, True)
    assert lengths["missing"] == (None, True)

    # The untried song goes next; the failed ones are not retried yet.
    assert await song_lengths.measure_song_lengths(session, limit=2) == 1
    assert (await _lengths(db))["good"] == (2000, True)
    assert await song_lengths.measure_song_lengths(session, limit=2) == 0

    # A day later they are tried again: the fixed file now reads.
    await db.execute(
        "UPDATE songs SET duration_checked_at = $1 WHERE id IN ('junk', 'missing')",
        now_ms() - DAY_MS - 1,
    )
    files["junk"] = _wav(4)
    assert await song_lengths.measure_song_lengths(session, limit=2) == 1
    lengths = await _lengths(db)
    assert lengths["junk"] == (4000, True)
    assert lengths["missing"] == (None, True)
