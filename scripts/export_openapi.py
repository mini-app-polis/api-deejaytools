"""Write the API's OpenAPI document to docs/openapi.json, or check it is current.

The committed document is the API's contract snapshot: deejaytools.com keeps
a copy and checks the endpoints and schemas it relies on against it, so a
change that would break the web app shows up in a diff and in a failing
check before it merges, without a deploy to test against.

    uv run python scripts/export_openapi.py          # rewrite the snapshot
    uv run python scripts/export_openapi.py --check  # exit 1 if it is stale

The document is generated from the app itself (``app.openapi()``); nothing
connects to a database. Settings is validated at import and requires a
database URL, so an inert placeholder is set when none is.

``info.version`` is left out: it is the package version, which moves on
every release without the contract changing.

The unit suite runs the same comparison (tests/unit/test_openapi_snapshot.py),
so CI fails when the snapshot is stale.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "docs" / "openapi.json"


def document() -> dict[str, Any]:
    """The OpenAPI document as the app builds it, without ``info.version``."""
    os.environ.setdefault(
        "DEEJAYTOOLS_DATABASE_URL",
        "postgresql://openapi@localhost:1/never_connected",
    )
    from api_deejaytools.main import app

    app.openapi_schema = None
    doc = app.openapi()
    doc["info"].pop("version", None)
    return doc


def render() -> str:
    """The snapshot's exact text."""
    return json.dumps(document(), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        current = SNAPSHOT.read_text() if SNAPSHOT.exists() else ""
        if current != text:
            print(
                f"{os.path.relpath(SNAPSHOT, ROOT)} is stale: run "
                "`uv run python scripts/export_openapi.py` and commit the result.",
                file=sys.stderr,
            )
            return 1
        return 0
    SNAPSHOT.write_text(text)
    print(f"wrote {os.path.relpath(SNAPSHOT, ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
