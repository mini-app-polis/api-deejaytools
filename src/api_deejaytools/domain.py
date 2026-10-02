"""Small domain rules shared across routes, ported from deejaytools-api.

Each is a single definition there (src/schemas/index.ts, src/lib/*) and is a
single definition here; do not reimplement them locally.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from functools import lru_cache
from zoneinfo import ZoneInfo, available_timezones

from .zod_coerce import js_trim

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
    owner = js_trim(owner_name or "")
    partner = js_trim(partner_name or "")
    if partner_kind and partner_kind != "partner":
        return partner or owner
    return f"{owner} & {partner}" if partner else owner


def full_name(*parts: str | None) -> str:
    """Join name parts, skipping empty ones, as ``[a, b].filter(Boolean).join(" ")``."""
    return js_trim(" ".join(p for p in parts if p))


def season_year_from_date_string(date_str: str) -> str:
    """Season year for a YYYY-MM-DD string, read from its fields (no timezone)."""
    year_str, _, rest = date_str.partition("-")
    month_str = rest.partition("-")[0]
    try:
        year, month = int(year_str), int(month_str)
    except ValueError as exc:
        raise ValueError(f"Invalid date string for season year: {date_str}") from exc
    return str(year + 1 if month >= SEASON_ROLLOVER_MONTH else year)


# Names zoneinfo can list that Intl (ICU) refuses: system files, not zones.
_NOT_ZONES = frozenset({"factory", "localtime", "posixrules"})
_NOT_ZONE_PREFIXES = ("posix/", "right/")

# Intl's UTC offset zones: +HH, +HHMM or +HH:MM, hours 00-23.
_OFFSET = re.compile(r"([+-])([01][0-9]|2[0-3])(?::?([0-5][0-9]))?")


@lru_cache(maxsize=1)
def _zone_names() -> dict[str, str]:
    names = {
        name.lower(): name
        for name in available_timezones()
        if name.lower() not in _NOT_ZONES and not name.startswith(_NOT_ZONE_PREFIXES)
    }
    names.setdefault("utc", "UTC")
    return names


def _offset(name: str) -> timezone | None:
    match = _OFFSET.fullmatch(name)
    if match is None:
        return None
    sign, hours, minutes = match.groups()
    delta = timedelta(hours=int(hours), minutes=int(minutes or 0))
    return timezone(-delta if sign == "-" else delta)


def canonical_timezone(name: str) -> str | None:
    """The IANA name for ``name``, matched case-insensitively as Intl matches
    it, the offset itself for an offset zone (``+05:00``, ``-0330``), or None
    if Intl would refuse it."""
    if _offset(name) is not None:
        return name
    return _zone_names().get(name.lower())


def _zone(name: str) -> tzinfo:
    offset = _offset(name)
    if offset is not None:
        return offset
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


# ICU root collation order (what Node's localeCompare uses), taken from Node
# 22: the ASCII punctuation and symbols, and the combining accents.
_ICU_PUNCTUATION = " _-,;:!?.'\"()[]{}@*/\\&#%`^+<=>|~$"
_PUNCTUATION_RANK = {c: i for i, c in enumerate(_ICU_PUNCTUATION)}
_ICU_ACCENTS = (
    "34f,332,313,343,314,301,341,300,340,306,302,30c,30a,342,308,344,30b,303,"
    "307,338,327,328,304,30d,30e,312,315,31a,33d,33e,33f,346,34a,34b,34c,350,"
    "351,352,357,35b,35d,35e,316,317,318,319,31c,31d,31e,31f,320,329,32a,32b,"
    "32c,32f,333,33a,33b,33c,347,348,349,34d,34e,353,354,355,356,359,35a,35c,"
    "35f,362,336,337,335,305,309,30f,310,311,31b,321,322,323,324,325,326,32d,"
    "32e,330,331,334,339,345,358,360,361,363,368,369,364,36a,365,36b,366,36c,"
    "36d,367,36e,36f"
)
_ACCENT_RANK = {chr(int(h, 16)): i for i, h in enumerate(_ICU_ACCENTS.split(","))}


def _primary(char: str) -> tuple[int, int, str]:
    # Whitespace, punctuation and symbols, then digits, then letters (and
    # anything else); punctuation in ICU's order, the rest by code point.
    category = unicodedata.category(char)
    if category[0] in "ZPSC":
        return 0, _PUNCTUATION_RANK.get(char, len(_ICU_PUNCTUATION)), char
    if category == "Nd":
        return 1, 0, char
    return 2, 0, char


def locale_key(
    text: str,
) -> tuple[
    tuple[tuple[int, int, str], ...], tuple[tuple[int, ...], ...], tuple[int, ...]
]:
    """Sort key approximating JavaScript's default ``localeCompare`` (ICU root
    collation) in three levels: base letters (punctuation before digits
    before letters, case and accents ignored), then accents, then case with
    lowercase first."""
    primary: list[tuple[int, int, str]] = []
    secondary: list[tuple[int, ...]] = []
    tertiary: list[int] = []
    for char in text:
        decomposed = unicodedata.normalize("NFKD", char)
        base = "".join(c for c in decomposed if not unicodedata.combining(c))
        marks = "".join(c for c in decomposed if unicodedata.combining(c))
        for b in base.casefold():
            primary.append(_primary(b))
        secondary.append(tuple(_ACCENT_RANK.get(m, len(_ACCENT_RANK)) for m in marks))
        tertiary.append(1 if base != base.lower() else 0)
    return tuple(primary), tuple(secondary), tuple(tertiary)
