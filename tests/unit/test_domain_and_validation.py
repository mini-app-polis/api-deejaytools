"""Ported domain rules and the zod-compatible validation types."""

from __future__ import annotations

from typing import ClassVar

import pytest
from pydantic import ValidationError

from api_deejaytools.domain import (
    DIVISIONS,
    canonical_timezone,
    ms_to_date_in_zone,
    partnership_display,
    season_year_from_date_string,
    song_entity_key,
)
from api_deejaytools.validation import Email, JsInt, JsNumber, ZodModel


def test_divisions_flat_order() -> None:
    assert DIVISIONS[:3] == ("Classic", "Showcase", "Rising Star Classic")
    assert DIVISIONS[-1] == "My Division Is Not Listed"
    assert len(DIVISIONS) == 17


def test_song_entity_key_precedence() -> None:
    assert song_entity_key("u", "p", "m") == "mp:m"
    assert song_entity_key("u", "p", None) == "pt:p"
    assert song_entity_key("u", None, None) == "us:u"


def test_partnership_display() -> None:
    assert partnership_display("Ada L", "Bo P") == "Ada L & Bo P"
    assert partnership_display("Ada L", "") == "Ada L"
    assert partnership_display("Ada L", "Team X", "team") == "Team X"
    assert partnership_display("Ada L", "", "solo") == "Ada L"


@pytest.mark.parametrize(
    ("date", "season"),
    [("2026-09-30", "2026"), ("2026-10-01", "2027"), ("2026-12-31", "2027")],
)
def test_season_year(date: str, season: str) -> None:
    assert season_year_from_date_string(date) == season


def test_timezones() -> None:
    assert canonical_timezone("america/chicago") == "America/Chicago"
    assert canonical_timezone("UTC") == "UTC"
    assert canonical_timezone("Mars/Olympus") is None
    # 2026-01-01T03:00Z is still Dec 31 in Chicago.
    assert ms_to_date_in_zone(1767236400000, "America/Chicago") == "2025-12-31"
    assert ms_to_date_in_zone(1767236400000, "bogus") == "2026-01-01"


class Body(ZodModel):
    _NULLABLE: ClassVar[frozenset[str]] = frozenset({"note"})

    email: Email
    count: JsInt | None = None
    at: JsNumber | None = None
    flag: bool | None = None
    note: str | None = None


def test_zod_model_behaviour() -> None:
    ok = Body.model_validate(
        {"email": "a@b.test", "count": 5.0, "at": 1.0, "note": None, "extra": 1}
    )
    assert (ok.count, ok.at, ok.note) == (5, 1, None)
    assert isinstance(ok.at, int)
    assert ok.model_fields_set == {"email", "count", "at", "note"}

    for bad in (
        {"email": "a@b.test", "count": "5"},  # no coercion
        {"email": "a@b.test", "count": 1.5},  # not an integer
        {"email": "a@b.test", "flag": "true"},  # no coercion
        {"email": "a@b.test", "count": None},  # optional is not nullable
        {"email": "not-an-email"},
        {"email": "a@localhost"},
    ):
        with pytest.raises(ValidationError):
            Body.model_validate(bad)
