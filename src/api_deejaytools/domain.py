"""Small domain rules shared across routes, ported from deejaytools-api.

Each is a single definition there (src/schemas/index.ts, src/lib/*) and is a
single definition here; do not reimplement them locally.
"""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime, tzinfo
from functools import lru_cache
from zoneinfo import ZoneInfo, available_timezones

# Source of truth for division order and grouping (schemas/index.ts
# DIVISION_GROUPS); DIVISIONS is its flat order.
DIVISION_GROUPS: tuple[tuple[str, ...], ...] = (
    ("Classic", "Showcase", "Rising Star Classic", "Rising Star Showcase"),
    ("ProAm LeaderAm", "ProAm FollowerAm", "NovInt Routines"),
    ("Sophisticated", "Masters", "Juniors", "Young Adult"),
    ("Exhibition", "Superstar"),
    ("Carolina Shag Divisions",),
    ("Teams", "Cabaret"),
    ("My Division Is Not Listed",),
)
DIVISIONS: tuple[str, ...] = tuple(d for group in DIVISION_GROUPS for d in group)

DEFAULT_TIMEZONE = "America/Chicago"

# Seasons roll over on October 1: October to December belong to the next
# calendar year's season (lib/seasonYear.ts).
SEASON_ROLLOVER_MONTH = 10


def song_entity_key(
    user_id: str, partner_id: str | None, managed_partnership_id: str | None
) -> str:
    """The competing entity a song belongs to: managed partnership, else
    partner, else the user alone."""
    if managed_partnership_id:
        return f"mp:{managed_partnership_id}"
    if partner_id:
        return f"pt:{partner_id}"
    return f"us:{user_id}"


def partnership_display(
    owner_name: str | None, partner_name: str | None, partner_kind: str | None = None
) -> str:
    """The "who" label for a song's partnership or a pair entity.

    Placeholder partners (kind solo/team/other) are a single entity: just
    their name, never "owner & placeholder".
    """
    owner = (owner_name or "").strip()
    partner = (partner_name or "").strip()
    if partner_kind and partner_kind != "partner":
        return partner or owner
    return f"{owner} & {partner}" if partner else owner


def full_name(*parts: str | None) -> str:
    """Join name parts, skipping empty ones, as ``[a, b].filter(Boolean).join(" ")``."""
    return " ".join(p for p in parts if p).strip()


def season_year_from_date_string(date_str: str) -> str:
    """Season year for a YYYY-MM-DD string, read from its fields (no timezone)."""
    year_str, _, rest = date_str.partition("-")
    month_str = rest.partition("-")[0]
    try:
        year, month = int(year_str), int(month_str)
    except ValueError as exc:
        raise ValueError(f"Invalid date string for season year: {date_str}") from exc
    return str(year + 1 if month >= SEASON_ROLLOVER_MONTH else year)


@lru_cache(maxsize=1)
def _zone_names() -> dict[str, str]:
    names = {name.lower(): name for name in available_timezones()}
    names.setdefault("utc", "UTC")
    return names


def canonical_timezone(name: str) -> str | None:
    """The IANA name for ``name``, matched case-insensitively as Intl matches
    it, or None if it is not a timezone."""
    return _zone_names().get(name.lower())


def _zone(name: str) -> ZoneInfo:
    canonical = canonical_timezone(name)
    if canonical is None:
        raise ValueError(f"unknown timezone {name!r}")
    return ZoneInfo(canonical)


def today_in_zone(timezone: str) -> str:
    """Today's date in ``timezone`` as YYYY-MM-DD; UTC if the zone is unusable."""
    try:
        return datetime.now(_zone(timezone)).strftime("%Y-%m-%d")
    except ValueError:
        return datetime.now(UTC).strftime("%Y-%m-%d")


def ms_to_date_in_zone(ms: float, timezone: str) -> str:
    """Epoch milliseconds as YYYY-MM-DD in ``timezone``; UTC if the zone is unusable."""
    zone: tzinfo
    try:
        zone = _zone(timezone)
    except ValueError:
        zone = UTC
    return datetime.fromtimestamp(ms / 1000, zone).strftime("%Y-%m-%d")


def locale_key(text: str) -> tuple[str, str]:
    """Sort key approximating JavaScript's default ``localeCompare``: accents
    and case are ignored first, then used to break ties."""
    base = "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )
    return (base.casefold(), text)
