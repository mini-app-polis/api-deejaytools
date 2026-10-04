"""Unit fixtures: nothing provisioned — no database, no network, no app client.

Settings is validated when first read and requires a database URL, so an
inert placeholder is set for the tests that read configuration. Nothing in
this layer imports a database driver, so it is never connected to.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault(
    "DEEJAYTOOLS_DATABASE_URL", "postgresql://unit@localhost:1/never_connected_test"
)


@pytest.fixture(autouse=True)
def _fresh_drive_clients() -> None:
    """Drop the Drive clients a previous test cached."""
    from api_deejaytools.services import drive

    drive.reset_drive_clients()
