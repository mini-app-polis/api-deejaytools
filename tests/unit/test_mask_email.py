"""The masked email the event people search returns (routers/event_people.py)."""

from __future__ import annotations

import pytest

from api_deejaytools.routers.event_people import mask_email


@pytest.mark.parametrize(
    ("email", "masked"),
    [
        ("kaiano@gmail.com", "k•••@gmail.com"),
        # A one-character local part: still three bullets, so the length of
        # the local part is never told.
        ("k@gmail.com", "k•••@gmail.com"),
        # Plus addressing and a subdomain: the whole domain is kept.
        ("Ada+wcs@mail.example.co.uk", "A•••@mail.example.co.uk"),
        # A quoted local part holding an @: the domain is after the last @.
        ('"a@b"@example.com', '"•••@example.com'),
        ("  ada@example.com ", "a•••@example.com"),
    ],
)
def test_masks_the_local_part(email: str, masked: str) -> None:
    assert mask_email(email) == masked


@pytest.mark.parametrize(
    "email", [None, "", "   ", "no-at-sign", "@example.com", "ada@"]
)
def test_nothing_to_mask_is_none(email: str | None) -> None:
    # Never the address as it is.
    assert mask_email(email) is None
