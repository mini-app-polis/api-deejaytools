"""The Google Drive layer (docs/DRIVE.md; deejaytools-api src/services/drive.ts).

Every Drive call in this service goes through ``mini_app_polis.google.drive.
DriveFacade`` (ADR-0010 "Reuse from the ecosystem"); nothing here or elsewhere
calls googleapiclient directly. The facade is synchronous, so each call runs
in a worker thread with ``asyncio.to_thread`` and the event loop never blocks
on Google.

Folder layout, as docs/DRIVE.md "Folder layout"::

    <root>/
    ├── <seasonYear>/<division>/<processed_filename>          uploads
    ├── <seasonYear>/Events/<event>/<division>[/Finals|/Prelims]/<filename>
    └── _deprecated/                                          soft deletes

Season year, division, event name and round subfolder go through
``sanitize_folder_name``; ``Events`` and ``_deprecated`` are literal.

Credentials are the service account deejaytools-api uses, from the same three
environment variables, with the ``drive.file`` scope only: the account sees
and changes nothing but files it created. If any of the three is missing or
empty, every operation raises ``DriveNotConfiguredError`` with the message
``Google Drive environment variables are not configured`` before touching
the network (nothing checks this at boot, as in deejaytools-api).

**Folder cache.** Resolved folder ids are reused for at most an hour, keyed by
``(parent, name)``, and the whole cache is dropped whenever an event copy fails
so a folder deleted out from under it recovers on the next retry. The ids
actually live in DriveFacade's process-wide ``FOLDER_CACHE``, which has no
expiry of its own (its key is ``"<parent>/<name>"``, i.e. the same
``(parent, name)``). The TTL is imposed from here as a cache *generation*: the
first lookup after a clear starts the generation, and the first lookup an hour
or more after that start empties ``FOLDER_CACHE`` (``clear_folder_cache()``)
and starts a new one. Every cached id was resolved inside the current
generation, so none is ever reused more than an hour after it was looked up;
some are dropped earlier than an hour, which costs one extra lookup each.
This module is the only user of ``ensure_folder`` in this process, so nothing
else fills or relies on that cache.
A folder this module trashes (``trash_folders``) is dropped from the cache
at once.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, cast

from mini_app_polis.google.drive import (
    FOLDER_CACHE,
    DriveFacade,
    ShareResult,
    clear_folder_cache,
)
from mini_app_polis.logger import LOG_WARNING, get_logger, with_log_prefix

from ..config import get_settings
from ..zod_coerce import js_words

logger = get_logger()

__all__ = [
    "DRIVE_SCOPE",
    "DriveNotConfiguredError",
    "EventCopyResult",
    "EventFolder",
    "NOT_CONFIGURED_MESSAGE",
    "SUBMISSION_APP_PROPERTY",
    "ShareResult",
    "clear_drive_folder_cache",
    "copy_song_to_event_folder",
    "drive_configured",
    "empty_folder_tree",
    "ensure_event_folder",
    "list_event_folders",
    "remove_reader",
    "rename_file",
    "sanitize_folder_name",
    "share_song_file",
    "share_with_reader",
    "soft_delete",
    "trash_folders",
    "upload_song_file",
]

NOT_CONFIGURED_MESSAGE = "Google Drive environment variables are not configured"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
_TOKEN_URI = "https://oauth2.googleapis.com/token"

EVENTS_FOLDER = "Events"
DEPRECATED_FOLDER = "_deprecated"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"

SUBMISSION_APP_PROPERTY = "deejaytools_submission_id"
"""Drive appProperty every event copy carries, valued with its submission id.

It is how a copy job finds a copy an earlier run already made (docs/DRIVE.md
"Known defects": overlapping runs and copy-then-DB-failure duplicated it).
"""

FOLDER_CACHE_TTL_SECONDS = 60 * 60


class DriveNotConfiguredError(RuntimeError):
    """A Drive call was made with a service-account variable missing."""

    def __init__(self) -> None:
        super().__init__(NOT_CONFIGURED_MESSAGE)


def sanitize_folder_name(name: str) -> str:
    """A folder name as docs/DRIVE.md specifies: ``/`` and ``\\`` become ``-``, runs
    of whitespace collapse to one space, the ends are trimmed, and an empty
    result becomes ``unknown``.

    Removing backslashes also means no folder name needs the backslash escape
    the Drive query would otherwise want (``ensure_folder`` escapes only
    quotes).
    """
    cleaned = " ".join(js_words(name.replace("/", "-").replace("\\", "-")))
    return cleaned or "unknown"


# --- configuration and client ------------------------------------------------


@dataclass(frozen=True)
class _DriveConfig:
    email: str
    private_key: str
    root_folder_id: str


def _config() -> _DriveConfig:
    settings = get_settings()
    account = settings.google_service_account
    root = settings.GOOGLE_DRIVE_PARENT_FOLDER_ID
    if account is None or not root:
        raise DriveNotConfiguredError()
    email, key = account
    return _DriveConfig(email, key, root)


def drive_configured() -> bool:
    """Whether the service account and the parent folder are both configured."""
    try:
        _config()
    except DriveNotConfiguredError:
        return False
    return True


def _build_facade(email: str, private_key: str) -> DriveFacade:
    return DriveFacade.from_service_account_info(
        {
            "type": "service_account",
            "client_email": email,
            "private_key": private_key,
            "token_uri": _TOKEN_URI,
        },
        scopes=[DRIVE_SCOPE],
    )


_thread_local = threading.local()
_client_generation = 0


def reset_drive_clients() -> None:
    """Make every thread build a fresh facade on its next call (tests)."""
    global _client_generation
    _client_generation += 1


def _thread_facade(email: str, private_key: str) -> DriveFacade:
    """This thread's facade, built on first use.

    One per thread, not one per process: googleapiclient's HTTP transport
    (httplib2) is not thread-safe, and every Drive call runs in a worker
    thread through ``asyncio.to_thread``. A shared client lets concurrent
    calls interleave on one connection and read each other's responses.
    """
    cache: dict[tuple[int, str, str], DriveFacade] = getattr(
        _thread_local, "facades", {}
    )
    _thread_local.facades = cache
    key = (_client_generation, email, private_key)
    for stale in [k for k in cache if k[0] != _client_generation]:
        del cache[stale]
    if key not in cache:
        cache[key] = _build_facade(email, private_key)
    return cache[key]


class _PerThreadFacade:
    """Looks like a DriveFacade; each method call runs on the calling
    thread's own facade. Resolve methods on the event loop, call them in the
    worker thread: ``await asyncio.to_thread(facade.copy_file, ...)``."""

    def __init__(self, email: str, private_key: str) -> None:
        self._email = email
        self._private_key = private_key

    def __getattr__(self, name: str) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            facade = _thread_facade(self._email, self._private_key)
            return getattr(facade, name)(*args, **kwargs)

        return call


def _drive() -> tuple[DriveFacade, str]:
    """The facade and the root folder id, or ``DriveNotConfiguredError``."""
    config = _config()
    facade = cast(DriveFacade, _PerThreadFacade(config.email, config.private_key))
    return facade, config.root_folder_id


# --- folder cache -------------------------------------------------------------

_clock: Callable[[], float] = time.monotonic
_generation_started_at: float | None = None


def clear_drive_folder_cache() -> None:
    """Drop every cached folder id (DriveFacade's ``FOLDER_CACHE``) and end
    the current cache generation."""
    global _generation_started_at
    clear_folder_cache()
    _generation_started_at = None


def _expire_folder_cache_if_due() -> None:
    global _generation_started_at
    now = _clock()
    if _generation_started_at is None:
        _generation_started_at = now
    elif now - _generation_started_at >= FOLDER_CACHE_TTL_SECONDS:
        clear_folder_cache()
        _generation_started_at = now


async def _ensure_folder(facade: DriveFacade, parent_id: str, name: str) -> str:
    """Find-or-create ``name`` under ``parent_id`` through the 1-hour cache."""
    _expire_folder_cache_if_due()
    return await asyncio.to_thread(facade.ensure_folder, parent_id, name)


# --- operations ---------------------------------------------------------------


async def upload_song_file(
    data: bytes,
    *,
    filename: str,
    mime_type: str,
    season_year: str,
    division: str,
    app_properties: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Upload a song's bytes to ``<root>/<season_year>/<division>/<filename>``.

    Year and division folders are created on demand; an empty division gives
    the ``unknown`` folder. ``app_properties`` are stored on the file as Drive
    appProperties, so the upload pipeline can tag a file with its song and
    find it again after a partial failure.

    Returns ``(file_id, division_folder_id)``.
    """
    facade, root = _drive()
    year_id = await _ensure_folder(facade, root, sanitize_folder_name(season_year))
    division_id = await _ensure_folder(facade, year_id, sanitize_folder_name(division))
    file_id = await asyncio.to_thread(
        facade.upload_bytes,
        parent_id=division_id,
        filename=filename,
        content=data,
        mime_type=mime_type,
        app_properties=app_properties,
        # Songs run to ~110 MiB; the single-request upload is for <= 5 MB.
        resumable=True,
    )
    if not file_id:
        raise RuntimeError("Drive upload did not return a file id")
    return file_id, division_id


async def find_song_file(
    *, key: str, value: str, season_year: str, division: str
) -> tuple[str, str] | None:
    """A file already uploaded to ``<root>/<season_year>/<division>/`` with the
    appProperty ``key=value``, as ``(file_id, division_folder_id)``, or None.

    Lets a build interrupted between its Drive upload and recording the file
    id find that file again instead of uploading a second copy.
    """
    facade, root = _drive()
    year_id = await _ensure_folder(facade, root, sanitize_folder_name(season_year))
    division_id = await _ensure_folder(facade, year_id, sanitize_folder_name(division))
    ids = await asyncio.to_thread(
        facade.find_files_by_app_property, division_id, key=key, value=value
    )
    return (ids[0], division_id) if ids else None


async def share_song_file(file_id: str, emails: Iterable[str | None]) -> ShareResult:
    """Grant ``reader`` on a file to each address, with no notification email.

    Addresses are trimmed, lowercased, de-duplicated and checked against
    ``^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$`` by the facade. Per-address failures
    come back in ``ShareResult.failed`` and are never raised; the caller logs
    them (``song_drive_share_partial_failure``).
    """
    facade, _root = _drive()
    # Materialized here: a generator consumed in the worker thread would run
    # caller code off the event loop.
    addresses = list(emails)
    return await asyncio.to_thread(facade.share_with_readers, file_id, addresses)


async def soft_delete(file_id: str) -> None:
    """Move a file into ``<root>/_deprecated``, removing all its current parents.

    Nothing is permanently deleted or put in the Drive trash. ``_deprecated``
    is created if missing. (docs/DRIVE.md "softDeleteOnDrive".)
    """
    facade, root = _drive()
    deprecated_id = await _ensure_folder(facade, root, DEPRECATED_FOLDER)
    await asyncio.to_thread(
        facade.move_file,
        file_id,
        new_parent_id=deprecated_id,
        remove_from_parents=True,
    )


async def rename_file(file_id: str, name: str) -> bool:
    """Rename a file if its current name differs; True when a change was made.

    Reading first makes a repeated rename sweep cost one metadata read per
    already-correct file and no writes.
    """
    facade, _root = _drive()
    current = await asyncio.to_thread(facade.get_file_name, file_id)
    if current == name:
        return False
    await asyncio.to_thread(facade.rename_file, file_id, name)
    return True


@dataclass(frozen=True)
class EventCopyResult:
    """An event copy: its id, the folder it is in, and whether it already existed."""

    file_id: str
    folder_id: str
    reused: bool


async def copy_song_to_event_folder(
    source_file_id: str,
    *,
    submission_id: str,
    filename: str,
    season_year: str,
    event_name: str,
    division: str,
    subfolder: str | None = None,
) -> EventCopyResult:
    """Copy a song file into
    ``<root>/<season_year>/Events/<event_name>/<division>[/<subfolder>]/<filename>``.

    The copy is tagged ``deejaytools_submission_id=<submission_id>``. Before
    copying, the destination folder is searched for a file with that tag and,
    if one exists, it is returned (``reused=True``) instead of a second copy.

    On any error in folder resolution, the lookup or the copy, the whole
    folder cache is dropped before re-raising: a cached id for a folder since
    deleted would otherwise fail every retry for the rest of the hour.
    """
    facade, root = _drive()
    try:
        year_id = await _ensure_folder(facade, root, sanitize_folder_name(season_year))
        events_id = await _ensure_folder(facade, year_id, EVENTS_FOLDER)
        event_id = await _ensure_folder(
            facade, events_id, sanitize_folder_name(event_name)
        )
        destination_id = await _ensure_folder(
            facade, event_id, sanitize_folder_name(division)
        )
        if subfolder:
            destination_id = await _ensure_folder(
                facade, destination_id, sanitize_folder_name(subfolder)
            )

        # Fix for docs/DRIVE.md "Known defects" (overlapping copy runs after a
        # lease reclaim, and a copy followed by a failed DB write, both made a
        # second copy): a copy this submission already has is found by its
        # tag and reused.
        existing = await asyncio.to_thread(
            facade.find_files_by_app_property,
            destination_id,
            key=SUBMISSION_APP_PROPERTY,
            value=submission_id,
        )
        if existing:
            if len(existing) > 1:
                logger.warning(
                    with_log_prefix(
                        LOG_WARNING,
                        f"drive_copy_duplicates_found submission={submission_id} "
                        f"folder={destination_id} file_ids={existing}",
                    )
                )
            return EventCopyResult(existing[0], destination_id, reused=True)

        file_id = await asyncio.to_thread(
            facade.copy_file,
            source_file_id,
            parent_folder_id=destination_id,
            name=filename,
            app_properties={SUBMISSION_APP_PROPERTY: submission_id},
        )
    except Exception:
        clear_drive_folder_cache()
        raise
    if not file_id:
        raise RuntimeError("Drive copy did not return a file id")
    return EventCopyResult(file_id, destination_id, reused=False)


async def ensure_event_folder(*, season_year: str, event_name: str) -> str:
    """The event's folder, ``<root>/<season_year>/Events/<event_name>/``.

    Created if it does not exist yet, as an event copy creates it. On any
    error the whole folder cache is dropped before re-raising, as for a copy.
    """
    facade, root = _drive()
    try:
        year_id = await _ensure_folder(facade, root, sanitize_folder_name(season_year))
        events_id = await _ensure_folder(facade, year_id, EVENTS_FOLDER)
        return await _ensure_folder(facade, events_id, sanitize_folder_name(event_name))
    except Exception:
        clear_drive_folder_cache()
        raise


async def share_with_reader(file_id: str, email: str) -> ShareResult:
    """Grant ``reader`` on a file or folder to one address, with no
    notification email, as ``share_song_file`` does. A failure comes back in
    ``ShareResult.failed``; an address the facade drops as invalid comes
    back in neither list."""
    return await share_song_file(file_id, [email])


async def remove_reader(file_id: str, email: str) -> int:
    """Remove every ``reader`` permission ``email`` holds directly on a file
    or folder; returns how many were removed. Safe to repeat.

    The facade's ``remove_permission``, on the worker thread's own facade.
    The address is compared trimmed and lowercased, as shares are made.
    Other roles and other people's permissions are left alone. Drive errors
    propagate (a Drive job retries them).
    """
    facade, _root = _drive()
    return await asyncio.to_thread(
        facade.remove_permission, file_id, email, role="reader"
    )


@dataclass(frozen=True)
class EventFolder:
    """A folder ``<root>/<season>/Events/<name>/``, with the names Drive has."""

    folder_id: str
    season: str
    name: str


async def list_event_folders() -> list[EventFolder]:
    """Every untrashed event folder, ``<root>/<season>/Events/<name>/``.

    Only lists: unlike the find-or-create lookups, nothing is created. The
    upload folders ``<root>/<season>/<division>/`` and ``_deprecated`` are
    not event folders and never returned.
    """
    facade, root = _drive()
    found: list[EventFolder] = []
    for season in await asyncio.to_thread(facade.get_all_subfolders, root):
        for child in await asyncio.to_thread(facade.get_all_subfolders, season.id):
            if child.name != EVENTS_FOLDER:
                continue
            for event in await asyncio.to_thread(facade.get_all_subfolders, child.id):
                found.append(EventFolder(event.id, season.name, event.name))
    return found


async def empty_folder_tree(folder_id: str) -> list[str] | None:
    """The folder and every folder below it, deepest first, when none of them
    holds an untrashed file; None as soon as one does.

    Anything that is not a folder counts as a file, whoever put it there.
    The service account sees only files it created (``drive.file``), so a
    file another account added is not seen.
    """
    facade, _root = _drive()
    folders: list[str] = []

    async def walk(current: str) -> bool:
        children = await asyncio.to_thread(facade.get_files_in_folder, current)
        for child in children:
            if child.mime_type != FOLDER_MIME_TYPE or not await walk(child.id):
                return False
        folders.append(current)
        return True

    return folders if await walk(folder_id) else None


async def trash_folders(folder_ids: list[str]) -> None:
    """Move folders to the Drive trash, in order (recoverable; nothing is
    permanently deleted), and drop every one trashed from the folder cache,
    so a later find-or-create looks the name up again instead of reusing a
    trashed id."""
    facade, _root = _drive()
    trashed: set[str] = set()
    try:
        for folder_id in folder_ids:
            await asyncio.to_thread(facade.trash_file, folder_id)
            trashed.add(folder_id)
    finally:
        for key, cached in list(FOLDER_CACHE.items()):
            if cached in trashed:
                FOLDER_CACHE.pop(key, None)
