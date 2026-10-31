"""Naming an uploaded song (docs/DRIVE.md "Background build", steps 2-5):
the upload-time season, the entity names after the ProAm swap, and the
``<stem>_vNN[.<ext>]`` filename's parts.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from ...domain import entity_name_pair, season_year_for
from ...models import ManagedPartnership, Partner, User
from ...zod_coerce import js_trim, js_words

_NON_ALNUM = re.compile(r"[^a-zA-Z0-9]")


def season_year_now(now: datetime | None = None) -> str:
    """The upload-time season, by the UTC date (ADR-0011).

    UTC whatever the server's own timezone, so the rollover happens at the
    same instant wherever the API runs. ``now`` is for tests; a naive value
    is read as UTC.
    """
    moment = now or datetime.now(UTC)
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC)
    return season_year_for(moment.year, moment.month)


def sanitize_segment(value: str | None) -> str:
    """Each whitespace-separated word stripped to ASCII alphanumerics,
    lowercased, first letter capitalised, joined: ``mary-jo o'neil`` →
    ``MaryjoOneil``."""
    if not value:
        return ""
    words = []
    for word in js_words(value):
        clean = _NON_ALNUM.sub("", word)
        if clean:
            lower = clean.lower()
            words.append(lower[0].upper() + lower[1:])
    return "".join(words)


def static_segment(value: str | None) -> str:
    """ASCII alphanumerics only, case kept: ``ProAm LeaderAm`` → ``ProAmLeaderAm``."""
    return _NON_ALNUM.sub("", value) if value else ""


def file_extension(filename: str) -> str:
    """The original file's extension: after the last dot unless that dot is
    first or last, alphanumerics only, lowercased."""
    trimmed = js_trim(filename)
    dot = trimmed.rfind(".")
    if dot <= 0 or dot == len(trimmed) - 1:
        return ""
    return _NON_ALNUM.sub("", trimmed[dot + 1 :]).lower()


def _name(*parts: str | None) -> str:
    return js_trim(" ".join(p for p in parts if p))


def entity_names(
    *,
    user: User,
    partner: Partner | None,
    managed: ManagedPartnership | None,
    division: str | None,
) -> tuple[str, str | None]:
    """(first, second) for the filename and title, after the ProAm swap."""
    return entity_name_pair(
        owner_name=_name(user.first_name, user.last_name) or user.id,
        partner_name=(
            _name(partner.first_name, partner.last_name)
            if partner is not None
            else None
        ),
        partner_kind=partner.kind if partner is not None else None,
        partner_role=partner.partner_role if partner is not None else None,
        managed_leader=(
            _name(managed.leader_first_name, managed.leader_last_name)
            if managed is not None
            else None
        ),
        managed_follower=(
            _name(managed.follower_first_name, managed.follower_last_name)
            if managed is not None
            else None
        ),
        division=division,
    )


def processed_stem(
    *,
    first: str,
    second: str | None,
    user_id: str,
    division: str | None,
    season_year: str,
    routine_name: str | None,
    personal_descriptor: str | None,
) -> str:
    """The filename without its version and extension."""
    partnership = (
        f"{sanitize_segment(first)}_{sanitize_segment(second)}"
        if second
        else sanitize_segment(first)
    )
    segments = [
        partnership or sanitize_segment(user_id) or "user",
        static_segment(division),
        sanitize_segment(season_year),
        sanitize_segment(routine_name),
        sanitize_segment(personal_descriptor),
    ]
    return "_".join(s for s in segments if s)


def versioned_filename(stem: str, version: int, extension: str) -> str:
    """``<stem>_vNN[.<ext>]``, at least two digits."""
    name = f"{stem}_v{version:02d}"
    return f"{name}.{extension}" if extension else name
