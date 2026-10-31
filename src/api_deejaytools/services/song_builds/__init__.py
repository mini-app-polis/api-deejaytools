"""Building an uploaded song: name it, tag it, upload it to Drive, record it.

docs/DRIVE.md "Background build" is the behaviour: the steps, the filename
rule, the tag contents, the sharing and the re-queued event copies. The
modules: ``naming`` (the filename and title), ``claims`` (a build's claim
and lease on its upload row), ``pipeline`` (the build), ``completion``
(recording a built song), ``failure`` (deleting or retrying a failed one)
and ``scheduling`` (starting builds in the background). The build is
durable:

- **A restart mid-build** does not leave a song with no file and nothing
  to sweep it. The bytes wait in ``song_uploads`` (migration 003), and the
  scheduler resumes any build that is due or whose lease ran out.
- **A failed build of a song already submitted or checked in** is retried
  with backoff instead of leaving the song without a file. Its row cannot
  be deleted (foreign keys), and it does not need to be.
- **A failure after the Drive upload** does not orphan the file. The file
  id is recorded as soon as the upload returns, a retry reuses it, a file
  found by its ``deejaytools_song_id`` appProperty is reused after a crash in
  between, and a song deleted meanwhile has its file deprecated.
- **Two uploads in flight for the same slot** never get the same
  ``vNN``. The filename is reserved under a per-slot advisory lock, counting
  the filenames other builds have reserved as well as finished songs.

A song nothing references is still deleted when its build fails: the
uploader sees it vanish and uploads again. The web app
and the conformance suite rely on that.

Tunables (``claims.HEARTBEAT_S``, ``scheduling.MAX_IN_FLIGHT`` and the like)
are read from the module that uses them, not from this package.
"""

from __future__ import annotations

from .claims import LostClaim
from .naming import (
    entity_names,
    file_extension,
    processed_stem,
    sanitize_segment,
    season_year_now,
    static_segment,
    versioned_filename,
)
from .pipeline import SONG_APP_PROPERTY, build_song
from .scheduling import process_song_uploads, start_build, wait_for_builds

__all__ = [
    "SONG_APP_PROPERTY",
    "LostClaim",
    "build_song",
    "entity_names",
    "file_extension",
    "process_song_uploads",
    "processed_stem",
    "sanitize_segment",
    "season_year_now",
    "start_build",
    "static_segment",
    "versioned_filename",
    "wait_for_builds",
]
