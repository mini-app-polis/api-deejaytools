"""The Drive layer (services/drive.py) over a real DriveFacade and a fake service.

Covers DRIVE.md "Configuration", "Folder layout" (names, sanitizing,
find-or-create, the 1-hour cache and its clearing on a failed event copy) and
``softDeleteOnDrive``. No network: the Google client is the in-memory fake in
drive_fakes.py.
"""

from __future__ import annotations

from typing import Any

import pytest
from mini_app_polis.google import drive as facade_module

from api_deejaytools.config import get_settings
from api_deejaytools.services import drive

from .drive_fakes import FakeDriveService, fake_facade

ROOT = "root"
NOT_CONFIGURED = "Google Drive environment variables are not configured"


class Clock:
    """A settable monotonic clock for the folder-cache TTL."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "GOOGLE_SERVICE_ACCOUNT_EMAIL", "sa@x.iam")
    monkeypatch.setattr(settings, "GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY", "KEY")
    monkeypatch.setattr(settings, "GOOGLE_DRIVE_PARENT_FOLDER_ID", ROOT)


@pytest.fixture
def svc(configured: None, monkeypatch: pytest.MonkeyPatch) -> FakeDriveService:
    facade, service = fake_facade()
    monkeypatch.setattr(drive, "_build_facade", lambda email, key: facade)
    return service


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(drive, "_clock", c)
    return c


@pytest.fixture(autouse=True)
def _empty_folder_cache() -> Any:
    drive.clear_drive_folder_cache()
    yield
    drive.clear_drive_folder_cache()


def folder_lookups(service: FakeDriveService) -> list[str]:
    return [
        kw["q"] for kw in service.ops("files.list") if "appProperties" not in kw["q"]
    ]


# --- sanitize_folder_name -------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Spring Classic", "Spring Classic"),
        ("A/B\\C", "A-B-C"),
        ("  Spring \t\n  Classic  ", "Spring Classic"),
        ("", "unknown"),
        ("   ", "unknown"),
        ("2027", "2027"),
        ("/", "-"),
    ],
)
def test_sanitize_folder_name(raw: str, expected: str) -> None:
    assert drive.sanitize_folder_name(raw) == expected


# --- configuration --------------------------------------------------------------


@pytest.mark.parametrize(
    "missing",
    [
        "GOOGLE_SERVICE_ACCOUNT_EMAIL",
        "GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY",
        "GOOGLE_DRIVE_PARENT_FOLDER_ID",
    ],
)
@pytest.mark.parametrize("value", [None, ""])
async def test_every_operation_fails_fast_when_unconfigured(
    configured: None,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
    value: str | None,
) -> None:
    monkeypatch.setattr(get_settings(), missing, value)

    def no_client(*_: Any) -> Any:
        raise AssertionError("no client may be built")

    monkeypatch.setattr(drive, "_build_facade", no_client)
    assert drive.drive_configured() is False
    calls = [
        drive.upload_song_file(
            b"x", filename="f", mime_type="audio/mpeg", season_year="2027", division="C"
        ),
        drive.share_song_file("f", ["a@b.co"]),
        drive.soft_delete("f"),
        drive.rename_file("f", "n"),
        drive.copy_song_to_event_folder(
            "s",
            submission_id="sub",
            filename="f",
            season_year="2027",
            event_name="E",
            division="C",
        ),
    ]
    for call in calls:
        with pytest.raises(drive.DriveNotConfiguredError) as info:
            await call
        assert str(info.value) == NOT_CONFIGURED


def test_drive_configured_when_all_three_are_set(configured: None) -> None:
    assert drive.drive_configured() is True


def test_credentials_use_drive_file_scope_and_unescape_the_key(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        get_settings(),
        "GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY",
        "-----BEGIN-----\\nabc\\n-----END-----\\n",
    )
    seen: dict[str, Any] = {}

    class FakeFacade:
        def get_file_name(self, file_id: str) -> str:
            return f"name of {file_id}"

    def fake_from_info(info: dict[str, Any], *, scopes: list[str]) -> FakeFacade:
        seen["info"], seen["scopes"] = info, scopes
        return FakeFacade()

    monkeypatch.setattr(drive.DriveFacade, "from_service_account_info", fake_from_info)
    drive.reset_drive_clients()
    facade, root = drive._drive()
    assert root == ROOT
    assert facade.get_file_name("f1") == "name of f1"
    assert seen["scopes"] == ["https://www.googleapis.com/auth/drive.file"]
    assert seen["info"]["client_email"] == "sa@x.iam"
    assert seen["info"]["private_key"] == "-----BEGIN-----\nabc\n-----END-----\n"


# --- upload ---------------------------------------------------------------------


async def test_upload_goes_to_season_and_division_folders(
    svc: FakeDriveService,
) -> None:
    file_id, folder_id = await drive.upload_song_file(
        b"bytes",
        filename="Ada_Classic_2027_v01.mp3",
        mime_type="audio/mpeg",
        season_year="2027",
        division="  Pro/Am  ",
        app_properties={"deejaytools_song_id": "song_1"},
    )
    assert svc.folder_path(folder_id) == ["2027", "Pro-Am"]
    f = svc.files_by_id[file_id]
    assert f["name"] == "Ada_Classic_2027_v01.mp3"
    assert f["parents"] == [folder_id]
    assert f["appProperties"] == {"deejaytools_song_id": "song_1"}
    (create,) = [c for c in svc.ops("files.create") if "media_body" in c]
    assert create["supportsAllDrives"] is True


async def test_upload_with_empty_division_uses_unknown(svc: FakeDriveService) -> None:
    _, folder_id = await drive.upload_song_file(
        b"x", filename="f.mp3", mime_type="audio/mpeg", season_year="2027", division=""
    )
    assert svc.folder_path(folder_id) == ["2027", "unknown"]


async def test_share_delegates_to_the_facade(svc: FakeDriveService) -> None:
    result = await drive.share_song_file(
        "file_1", (e for e in [" A@B.co ", None, "a@b.co", "bad"])
    )
    assert result.shared == ["a@b.co"]
    assert result.failed == []
    (perm,) = svc.ops("permissions.create")
    assert perm["body"] == {"role": "reader", "type": "user", "emailAddress": "a@b.co"}
    assert perm["sendNotificationEmail"] is False


# --- event copy -----------------------------------------------------------------


async def test_event_copy_folder_chain_and_tag(svc: FakeDriveService) -> None:
    source = svc.add_file("orig.mp3", ["x"])
    result = await drive.copy_song_to_event_folder(
        source,
        submission_id="sub_1",
        filename="Ada_Classic_2027_v01.mp3",
        season_year="2027",
        event_name="Spring / Classic ",
        division="Classic",
        subfolder="Finals",
    )
    assert result.reused is False
    assert svc.folder_path(result.folder_id) == [
        "2027",
        "Events",
        "Spring - Classic",
        "Classic",
        "Finals",
    ]
    (copy,) = svc.ops("files.copy")
    assert copy["fileId"] == source
    assert copy["body"] == {
        "parents": [result.folder_id],
        "name": "Ada_Classic_2027_v01.mp3",
        "appProperties": {"deejaytools_submission_id": "sub_1"},
    }


async def test_event_copy_without_subfolder_sits_in_division(
    svc: FakeDriveService,
) -> None:
    result = await drive.copy_song_to_event_folder(
        "src",
        submission_id="sub_1",
        filename="f.mp3",
        season_year="2027",
        event_name="E",
        division="Classic",
    )
    assert svc.folder_path(result.folder_id) == ["2027", "Events", "E", "Classic"]


async def test_event_copy_reuses_a_tagged_copy(svc: FakeDriveService) -> None:
    first = await drive.copy_song_to_event_folder(
        "src",
        submission_id="sub_1",
        filename="f.mp3",
        season_year="2027",
        event_name="E",
        division="Classic",
    )
    second = await drive.copy_song_to_event_folder(
        "src",
        submission_id="sub_1",
        filename="f.mp3",
        season_year="2027",
        event_name="E",
        division="Classic",
    )
    assert second.reused is True
    assert second.file_id == first.file_id
    assert len(svc.ops("files.copy")) == 1


# --- folder cache ---------------------------------------------------------------


async def _upload(season: str = "2027") -> str:
    _, folder = await drive.upload_song_file(
        b"x", filename="f", mime_type="audio/mpeg", season_year=season, division="C"
    )
    return folder


async def test_folders_are_cached_within_the_hour(
    svc: FakeDriveService, clock: Clock
) -> None:
    await _upload()
    assert len(folder_lookups(svc)) == 2
    clock.now += 3599
    await _upload()
    assert len(folder_lookups(svc)) == 2


async def test_folder_cache_expires_after_an_hour(
    svc: FakeDriveService, clock: Clock
) -> None:
    await _upload()
    clock.now += 3600
    await _upload()
    assert len(folder_lookups(svc)) == 4
    # The facade's own cache was emptied, not just ours.
    clock.now += 10
    await _upload()
    assert len(folder_lookups(svc)) == 4


async def test_failed_event_copy_drops_the_whole_cache(
    svc: FakeDriveService, clock: Clock
) -> None:
    await _upload()  # caches 2027 and 2027/C
    assert facade_module.FOLDER_CACHE
    svc.fail_copy = RuntimeError("Drive down")
    with pytest.raises(RuntimeError, match="Drive down"):
        await drive.copy_song_to_event_folder(
            "src",
            submission_id="s",
            filename="f",
            season_year="2027",
            event_name="E",
            division="C",
        )
    assert facade_module.FOLDER_CACHE == {}
    before = len(folder_lookups(svc))
    await _upload()
    assert len(folder_lookups(svc)) == before + 2


async def test_failed_folder_resolution_in_a_copy_also_drops_the_cache(
    svc: FakeDriveService, clock: Clock
) -> None:
    await _upload()
    svc.fail_list = RuntimeError("stale parent")
    with pytest.raises(RuntimeError, match="stale parent"):
        await drive.copy_song_to_event_folder(
            "src",
            submission_id="s",
            filename="f",
            season_year="2027",
            event_name="E",
            division="C",
        )
    assert facade_module.FOLDER_CACHE == {}


async def test_failed_upload_does_not_drop_the_cache(
    svc: FakeDriveService, clock: Clock
) -> None:
    await _upload()
    cached = dict(facade_module.FOLDER_CACHE)
    svc.fail_list = RuntimeError("down")
    with pytest.raises(RuntimeError):
        await _upload("2028")
    assert facade_module.FOLDER_CACHE == cached


async def test_a_folder_deleted_under_the_cache_recovers_after_a_failed_copy(
    svc: FakeDriveService, clock: Clock
) -> None:
    first = await drive.copy_song_to_event_folder(
        "src",
        submission_id="a",
        filename="f",
        season_year="2027",
        event_name="E",
        division="C",
    )
    # Someone deletes the event folder chain in Drive; the cached ids are dead.
    for fid in list(svc.files_by_id):
        if svc.files_by_id[fid]["name"] in {"Events", "E", "C"}:
            del svc.files_by_id[fid]
    svc.fail_copy = RuntimeError("parent not found")
    with pytest.raises(RuntimeError):
        await drive.copy_song_to_event_folder(
            "src",
            submission_id="b",
            filename="f",
            season_year="2027",
            event_name="E",
            division="C",
        )
    svc.fail_copy = None
    retry = await drive.copy_song_to_event_folder(
        "src",
        submission_id="b",
        filename="f",
        season_year="2027",
        event_name="E",
        division="C",
    )
    assert retry.folder_id != first.folder_id
    assert retry.folder_id in svc.files_by_id


# --- soft delete and rename -------------------------------------------------------


async def test_soft_delete_moves_into_deprecated_removing_all_parents(
    svc: FakeDriveService,
) -> None:
    file_id = svc.add_file("song.mp3", ["p1", "p2"])
    await drive.soft_delete(file_id)
    deprecated = svc.folder_id(ROOT, "_deprecated")
    assert deprecated is not None
    assert svc.files_by_id[file_id]["parents"] == [deprecated]
    (update,) = svc.ops("files.update")
    assert update["addParents"] == deprecated
    assert update["removeParents"] == "p1,p2"
    assert update["supportsAllDrives"] is True


async def test_soft_delete_reuses_an_existing_deprecated_folder(
    svc: FakeDriveService,
) -> None:
    a = svc.add_file("a", ["p"])
    b = svc.add_file("b", ["p"])
    await drive.soft_delete(a)
    await drive.soft_delete(b)
    assert svc.files_by_id[a]["parents"] == svc.files_by_id[b]["parents"]
    assert len([c for c in svc.ops("files.create")]) == 1


async def test_rename_is_a_noop_when_the_name_matches(svc: FakeDriveService) -> None:
    file_id = svc.add_file("same.mp3", ["p"])
    assert await drive.rename_file(file_id, "same.mp3") is False
    assert svc.ops("files.update") == []


async def test_rename_updates_a_different_name(svc: FakeDriveService) -> None:
    file_id = svc.add_file("old.mp3", ["p"])
    assert await drive.rename_file(file_id, "new.mp3") is True
    assert svc.files_by_id[file_id]["name"] == "new.mp3"


async def test_each_worker_thread_gets_its_own_client(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # httplib2 is not thread-safe: concurrent to_thread calls must not share one.
    import asyncio
    import threading

    built: list[int] = []

    class FakeFacade:
        def __init__(self) -> None:
            built.append(threading.get_ident())
            self.thread = threading.get_ident()

        def owner(self) -> int:
            return self.thread

    monkeypatch.setattr(drive, "_build_facade", lambda email, key: FakeFacade())
    drive.reset_drive_clients()
    facade, _ = drive._drive()
    barrier = threading.Barrier(3)

    def call() -> tuple[int, int]:
        barrier.wait(timeout=5)
        return threading.get_ident(), facade.owner()  # type: ignore[attr-defined]

    results = await asyncio.gather(*(asyncio.to_thread(call) for _ in range(3)))
    assert all(thread == owner for thread, owner in results)
    assert len(set(built)) == 3
    assert facade.owner() == facade.owner()  # type: ignore[attr-defined]
    assert built.count(threading.get_ident()) == 1  # reused on the same thread
