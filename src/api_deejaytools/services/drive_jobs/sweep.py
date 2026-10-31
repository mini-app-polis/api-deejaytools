"""The event folder sweep: trashing the Drive folders of deleted events once
nothing live is left in them.

It is not a job: the scheduler runs it now and then (``services.scheduler``),
since a deleted event leaves no row to queue it on.
"""

from __future__ import annotations

from dataclasses import dataclass

from mini_app_polis.logger import LOG_SUCCESS, get_logger, with_log_prefix
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...models import Event
from .. import drive
from .enqueue import event_folder_path

logger = get_logger()


@dataclass(frozen=True)
class TrashedEventFolder:
    """An event folder the sweep trashed, with how many folders were below it."""

    folder_id: str
    season: str
    name: str
    subfolders: int


async def sweep_event_folders(
    db: AsyncSession, *, limit: int | None = None
) -> list[TrashedEventFolder]:
    """Trash every event folder, ``<root>/<season>/Events/<name>/``, that no
    event maps to and that holds no untrashed file anywhere below it, with
    the folders inside it. Returns, and logs, what it trashed.

    With ``limit``, it stops once it has trashed that many: each folder takes
    several Drive calls, and the scheduler runs the sweep in its pass, so a
    backlog is cleared a few folders at a time rather than in one long pass.

    An event maps to a folder when its season and name, as folder names
    (``event_folder_path``), are the folder's. Drive is listed before the
    events are read, so a folder made for an event created meanwhile is
    matched to it. Folders go to the Drive trash, never deleted for good;
    a folder with any file in it stays, and so does every upload folder.
    A Drive error ends the sweep; the next one picks up where it stopped.
    """
    folders = await drive.list_event_folders()
    if not folders:
        return []
    events = (
        await db.execute(select(Event.name, Event.season_year, Event.start_date))
    ).all()
    await db.commit()
    mapped: set[tuple[str, str]] = set()
    for event in events:
        try:
            mapped.add(
                event_folder_path(event.name, event.season_year, event.start_date)
            )
        except ValueError:
            # No season, so no folder: copies and shares fail the same way.
            continue

    trashed: list[TrashedEventFolder] = []
    for folder in folders:
        if limit is not None and len(trashed) >= limit:
            break
        if (folder.season, folder.name) in mapped:
            continue
        tree = await drive.empty_folder_tree(folder.folder_id)
        if tree is None:
            continue
        await drive.trash_folders(tree)
        trashed.append(
            TrashedEventFolder(
                folder.folder_id, folder.season, folder.name, len(tree) - 1
            )
        )
        logger.info(
            with_log_prefix(
                LOG_SUCCESS,
                f"drive_event_folder_trashed folder={folder.folder_id} "
                f"path={folder.season}/{drive.EVENTS_FOLDER}/{folder.name} "
                f"subfolders={len(tree) - 1}",
            )
        )
    logger.info(
        with_log_prefix(
            LOG_SUCCESS,
            f"drive_event_folders_swept folders={len(folders)} trashed={len(trashed)}",
        )
    )
    return trashed
