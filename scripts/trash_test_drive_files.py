"""Move every test file left in this environment's Drive tree to the trash.

Outside production every upload and event copy is tagged
``deejaytools_test_file=1`` (docs/DRIVE.md "Test files"), and deleting one
already trashes it. This sweeps up the rest: files a test run created and
never deleted, in the season, event and ``_deprecated`` folders alike. Only
tagged files are touched, so real users' files stay where they are even if
production shares the root. The trash is recoverable; nothing is deleted
permanently.

    doppler run -- uv run python scripts/trash_test_drive_files.py --dry-run
    doppler run -- uv run python scripts/trash_test_drive_files.py

Reads GOOGLE_CREDENTIALS_JSON and GOOGLE_DRIVE_PARENT_FOLDER_ID. Exits
non-zero when Drive is not configured or a Drive call fails.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Settings requires a database URL; this script never connects to one.
os.environ.setdefault(
    "DEEJAYTOOLS_DATABASE_URL", "postgresql://unused@localhost:1/never_connected"
)

from api_deejaytools.services import drive  # noqa: E402


async def _run(dry_run: bool) -> int:
    if not drive.drive_configured():
        print(drive.NOT_CONFIGURED_MESSAGE, file=sys.stderr)
        return 1
    found = await drive.trash_test_files(dry_run=dry_run)
    verb = "would trash" if dry_run else "trashed"
    for file_id in found:
        print(f"{verb} {file_id}")
    print(f"{verb} {len(found)} test file(s)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run", action="store_true", help="list the files, trash nothing"
    )
    return asyncio.run(_run(parser.parse_args().dry_run))


if __name__ == "__main__":
    sys.exit(main())
