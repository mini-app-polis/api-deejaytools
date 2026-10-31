"""The ``drive_jobs`` queue (docs/DRIVE.md, "drive_jobs queue").

Enqueueing (``enqueue``: best-effort, after the request's own commit, or in
the transaction of the change that calls for the job) and processing
(``processor``): the batch the scheduler runs once per tick
(``process_drive_jobs``), with the per-kind work in ``copy_jobs`` (``copy``,
``rename``, ``trash``) and ``share_jobs`` (``share_event_folder``,
``unshare_event_folder``, ``share_song``).

``share_song`` gives a song's partner reader access to the song's Drive
file when the partner is set after the file was built (an upload's build
shares it with the partner it has then). Like the folder jobs it is added
in the transaction of the change that calls for it, and reads the song's
partner when it runs.

``share_event_folder`` and ``unshare_event_folder`` give an event's DJs and
event managers read access to the event's folder, and take it away again
(ADR-0012, "The DJ's Drive folder"). They are added in the transaction of
the staff change or event change that calls for them (``add_drive_job``),
so they are as durable as that change. Each checks the staff list when it
runs, so jobs that run out of order still leave access matching it.

``sweep_event_folders`` (``sweep``) trashes the folders of deleted events
once nothing live is left in them. It is not a job: the scheduler runs it
now and then (``services.scheduler``), since a deleted event leaves no row
to queue it on.

Processing guards against three failure modes (ADR-0007 point 4):

- overlapping copy runs, and a copy followed by a failed DB write, do not
  leave a duplicate copy: copies are tagged with their submission id and an
  existing one is reused (``services.drive.copy_song_to_event_folder``);
- a copy that finishes after its submission was deleted, or after another run
  recorded a different copy, is recorded only into a still-empty
  ``drive_copy_file_id`` and otherwise soft-deleted (``copy_jobs._record_copy``);
- a DB error while recording a job's outcome does not strand the rest of
  the batch in ``running``: each status update is guarded on its own.
"""

from __future__ import annotations

from .enqueue import (
    DRIVE_FOLDER_ROLES,
    SHARE_EVENT_FOLDER,
    SHARE_SONG,
    UNSHARE_EVENT_FOLDER,
    DriveJobKind,
    add_drive_job,
    enqueue_copy_job,
    enqueue_drive_job,
    enqueue_trash_jobs,
    event_folder_path,
)
from .job import DriveJobRefused
from .processor import (
    DEFAULT_BATCH_SIZE,
    LEASE_TIMEOUT_MS,
    MAX_ATTEMPTS,
    backoff_ms,
    process_drive_jobs,
)
from .sweep import TrashedEventFolder, sweep_event_folders

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DRIVE_FOLDER_ROLES",
    "LEASE_TIMEOUT_MS",
    "MAX_ATTEMPTS",
    "SHARE_EVENT_FOLDER",
    "SHARE_SONG",
    "UNSHARE_EVENT_FOLDER",
    "DriveJobKind",
    "DriveJobRefused",
    "TrashedEventFolder",
    "add_drive_job",
    "backoff_ms",
    "enqueue_copy_job",
    "enqueue_drive_job",
    "enqueue_trash_jobs",
    "event_folder_path",
    "process_drive_jobs",
    "sweep_event_folders",
]
