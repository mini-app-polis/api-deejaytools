"""The season year rolls over on October 1, by the UTC date (ADR-0011)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from api_deejaytools.domain import season_year_for, season_year_from_date_string
from api_deejaytools.services.song_builds import season_year_now

CENTRAL = timezone(timedelta(hours=-5))


@pytest.mark.parametrize(
    ("moment", "season"),
    [
        (datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC), "2026"),
        (datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC), "2027"),
        (datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC), "2027"),
        (datetime(2027, 1, 1, 0, 0, 0, tzinfo=UTC), "2027"),
        # 7:30 pm Central on Sep 30 is already Oct 1 in UTC: next season.
        (datetime(2026, 9, 30, 19, 30, tzinfo=CENTRAL), "2027"),
        # 6:30 pm Central is still Sep 30 in UTC.
        (datetime(2026, 9, 30, 18, 30, tzinfo=CENTRAL), "2026"),
        # A naive value is read as UTC.
        (datetime(2026, 10, 1, 0, 0, 0), "2027"),
    ],
)
def test_upload_season_rolls_over_on_october_1_utc(
    moment: datetime, season: str
) -> None:
    assert season_year_now(moment) == season


def test_uploads_and_events_share_one_rule() -> None:
    for month in range(1, 13):
        assert season_year_for(2026, month) == season_year_from_date_string(
            f"2026-{month:02d}-15"
        )
        assert season_year_now(datetime(2026, month, 15, tzinfo=UTC)) == (
            season_year_for(2026, month)
        )
