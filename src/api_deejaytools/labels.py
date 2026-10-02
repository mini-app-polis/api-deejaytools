"""Song and entity labels (deejaytools-api src/lib/songLabel.ts and the
``partnershipLabel`` in src/routes/event-song-submissions.ts)."""

from __future__ import annotations

import re

from .domain import full_name, partnership_display
from .zod_coerce import js_trim

# JavaScript's /_v(\d+)(?:\.[^.]*)?$/: ASCII digits, and $ only at the very end.
_VERSION = re.compile(r"_v([0-9]+)(?:\.[^.]*)?\Z")


def song_version(processed_filename: str | None) -> str | None:
    """The ``vNN`` a processed filename carries ("..._v03.mp3" -> "v03")."""
    match = _VERSION.search(processed_filename or "")
    return f"v{match.group(1)}" if match else None


def build_structured_song_label(
    *,
    partnership: str,
    division: str | None,
    season_year: str | None,
    routine_name: str | None,
    processed_filename: str | None,
    display_name: str | None,
    song_id: str,
) -> str:
    """Song label outside the live queue: "Partnership Division Year Routine vNN".

    Missing pieces are dropped. With no structure at all (no division,
    season year or routine name), or no partnership, it falls back to the
    display name, then the processed filename, then the partnership, then
    the song id.
    """
    version = song_version(processed_filename)
    division = js_trim(division or "") or None
    season_year = js_trim(season_year or "") or None
    routine_name = js_trim(routine_name or "") or None
    partnership = js_trim(partnership)

    if (division or season_year or routine_name) and partnership:
        return " ".join(
            p for p in (partnership, division, season_year, routine_name, version) if p
        )
    return (
        js_trim(display_name or "")
        or js_trim(processed_filename or "")
        or partnership
        or song_id
    )


def partnership_label(
    *,
    managed_leader_first: str | None,
    managed_leader_last: str | None,
    managed_follower_first: str | None,
    managed_follower_last: str | None,
    owner_first: str | None,
    owner_last: str | None,
    partner_first: str | None,
    partner_last: str | None,
    partner_kind: str | None = None,
) -> str:
    """The "who" of a song: its managed partnership's names when it has one
    (joined row present), else owner and partner via ``partnership_display``."""
    if managed_leader_first is not None:
        leader = full_name(managed_leader_first, managed_leader_last)
        follower = full_name(managed_follower_first, managed_follower_last)
        return f"{leader} & {follower}" if follower else leader
    return partnership_display(
        full_name(owner_first, owner_last),
        full_name(partner_first, partner_last),
        partner_kind,
    )
