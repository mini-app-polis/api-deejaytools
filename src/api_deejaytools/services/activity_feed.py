"""The activity feed's messages: one per person, in words, posted by the scheduler.

``services/change_feed.py`` keeps each change in ``activity_feed``; once a
person has been quiet for a minute, :func:`post_due` claims their rows and
posts them to the activity channel as one message, titled with their name,
one line a change:

    Gregory Rollinscott
    🎵 added 'Good morning' (ProAm LeaderAm, managed partnership Leigh Yu
       & Lemery Rollinscott) to The Open · Drive file
    👥 created team 'Future WOW'
    ✏️ updated their name

Names are read when the message is sent: song titles, events and people
from the database, by the ids the rows kept; what a deleted row was called,
from the row itself. Before a line is written:

- a record's ``covers`` drops the rows of the same request it speaks for (a
  song delete's cascaded submissions), and a request that said what it did
  drops its own ``bulk`` statements;
- "song added" and that song's submission to an event are one line;
- many rows of one kind at one event (a registration list) are one count.

A row with nothing worth saying (an edit that changed no named column) is
left out; a table no line describes is said by its name and a count.
Only production posts, as for the rest of the feed; elsewhere the rows are
claimed and logged, so they do not pile up.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

from mini_app_polis import discord
from mini_app_polis.logger import LOG_FAILURE, get_logger, with_log_prefix
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import get_settings
from ..models import Event, Song, User
from ..models import Session as FloorSession
from . import change_feed, notifications
from .change_feed import ADDED, BULK, CREATED, DELETED, SUMMARY, UPDATED, Entry

logger = get_logger()

_md = notifications._md

#: Discord's limit on an embed's description, less room for "…and N more".
_DESCRIPTION_LIMIT = 3900

#: More than this many rows of one kind at one event are one count.
_COLLAPSE_OVER = 3

#: How a changed column is named in a line, where its name is not enough.
_FIELD_LABELS = {
    "partner_id": "partner",
    "partner_role": "role",
    "routine_name": "routine name",
    "display_name": "display name",
    "personal_descriptor": "descriptor",
    "season_year": "season",
    "original_filename": "file name",
    "first_name": "first name",
    "last_name": "last name",
    "start_date": "start date",
    "end_date": "end date",
    "checkin_opens_at": "check-in time",
    "floor_trial_starts_at": "start time",
    "floor_trial_ends_at": "end time",
    "active_priority_max": "priority limit",
    "active_non_priority_max": "non-priority limit",
    "event_id": "event",
    "linked_label": "link",
}

_ROLE_WORDS = {
    "dj": "a DJ",
    "event-manager": "an event manager",
    "coordinator": "a coordinator",
}

_ROUND_WORDS = {"prelims_only": "prelims only", "finals_only": "finals only"}

#: Tables a fallback line names in words rather than by the table.
_TABLE_WORDS = {
    "session_divisions": "session divisions",
    "drive_jobs": "Drive jobs",
    "event_division_run_limits": "division run limits",
}


def _fields(facts: dict[str, Any]) -> str:
    names = [_FIELD_LABELS.get(f, f.replace("_", " ")) for f in facts.get("fields", [])]
    return ", ".join(dict.fromkeys(names))


def _quoted(title: str | None) -> str:
    return f"'{_md(title)}'" if title else "a song"


def _person(first: str | None, last: str | None) -> str:
    return " ".join(p.strip() for p in (first, last) if p and p.strip())


def _pair(facts: dict[str, Any], prefix: str = "") -> str:
    leader = _person(
        facts.get(f"{prefix}leader_first_name"), facts.get(f"{prefix}leader_last_name")
    )
    follower = _person(
        facts.get(f"{prefix}follower_first_name"),
        facts.get(f"{prefix}follower_last_name"),
    )
    return _md(" & ".join(p for p in (leader, follower) if p) or "a partnership")


def _dancers(facts: dict[str, Any]) -> str:
    names = " & ".join(p for p in (facts.get("dancer_1"), facts.get("dancer_2")) if p)
    division = facts.get("division")
    text = _md(names or "an entry")
    return f"{text} ({_md(division)})" if division else text


class Lookups:
    """Names by id, read once per message from a session of its own."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self._users: dict[str, str | None] = {}
        self._events: dict[str, str | None] = {}
        self._songs: dict[str, tuple[str | None, str | None] | None] = {}
        self._sessions: dict[str, tuple[str, str | None] | None] = {}

    async def user(self, user_id: str | None) -> str:
        """The person's first and last name, else their id; "someone" for none."""
        if not user_id:
            return "someone"
        if user_id not in self._users:
            row = (
                await self.db.execute(
                    select(User.first_name, User.last_name).where(User.id == user_id)
                )
            ).first()
            self._users[user_id] = (
                _person(row.first_name, row.last_name) or None if row else None
            )
        return _md(self._users[user_id] or user_id)

    async def event(self, event_id: str | None) -> str:
        """The event's name; "a deleted event" once its row is gone."""
        if not event_id:
            return "an event"
        if event_id not in self._events:
            self._events[event_id] = (
                await self.db.execute(select(Event.name).where(Event.id == event_id))
            ).scalar_one_or_none()
        name = self._events[event_id]
        return _md(name) if name else "a deleted event"

    async def song(self, song_id: str | None) -> tuple[str | None, str | None]:
        """(title, division), deleted songs included: they keep their row."""
        if not song_id:
            return None, None
        if song_id not in self._songs:
            row = (
                await self.db.execute(
                    select(Song.routine_name, Song.display_name, Song.division).where(
                        Song.id == song_id
                    )
                )
            ).first()
            self._songs[song_id] = (
                (row.routine_name or row.display_name, row.division) if row else None
            )
        return self._songs[song_id] or (None, None)

    async def session(self, session_id: str | None) -> tuple[str, str | None]:
        """(name, event id) of a floor session; "a session" when it is unknown."""
        if not session_id:
            return "a session", None
        if session_id not in self._sessions:
            row = (
                await self.db.execute(
                    select(FloorSession.name, FloorSession.event_id).where(
                        FloorSession.id == session_id
                    )
                )
            ).first()
            self._sessions[session_id] = (row.name, row.event_id) if row else None
        found = self._sessions[session_id]
        return (_md(found[0]), found[1]) if found else ("a session", None)


# ---------------------------------------------------------------------------
# Lines
# ---------------------------------------------------------------------------

Line = Callable[[Entry, Lookups], Awaitable[str | None]]


def song_added_phrase(facts: dict[str, Any]) -> str:
    """ "added 'Routine' (Division, with Partner)", without who added it.

    ``for`` is the dancer the song belongs to when someone else uploaded it
    ("Upload For"): then "uploaded 'Routine' for Owner (…)".
    """
    what = _quoted(facts.get("routine"))
    owner = facts.get("for")
    sentence = f"uploaded {what} for {_md(owner)}" if owner else f"added {what}"
    details: list[str] = []
    division = facts.get("division")
    if division:
        details.append(_md(division))
    partner, kind = facts.get("partner"), facts.get("partner_kind")
    if facts.get("managed"):
        details.append(f"managed partnership {_md(facts['managed'])}")
    elif partner and kind == "team":
        details.append(f"team {_md(partner)}")
    elif partner and kind and kind != "partner":
        details.append(f"as {_md(partner)}")
    elif partner:
        details.append(f"with {_md(partner)}")
    if details:
        sentence += f" ({', '.join(details)})"
    return sentence


async def _song_added(e: Entry, look: Lookups) -> str:
    line = f"🎵 {song_added_phrase(e.facts)}"
    events = [await look.event(event_id) for event_id in e.facts.get("events", [])]
    if events:
        line += f" to {', '.join(events)}"
    file_id = e.facts.get("drive_file_id")
    if file_id:
        line += f" · [Drive file]({notifications.drive_file_url(file_id)})"
    return line


async def _song(e: Entry, look: Lookups) -> str | None:
    f = e.facts
    title, division = await look.song(f.get("id"))
    title = title or f.get("routine_name") or f.get("display_name")
    if e.op == CREATED:
        return f"🎵 created song {_quoted(title)}"
    if e.op == UPDATED:
        fields = _fields(f)
        return f"✏️ edited {_quoted(title)} ({fields})" if fields else None
    if e.op == DELETED:
        division = division or f.get("division")
        line = f"🗑️ deleted {_quoted(title)}"
        if division:
            line += f" ({_md(division)})"
        count = f.get("submissions", 0)
        if count:
            line += f", withdrawn from {count} event{'s' if count != 1 else ''}"
        return line
    return None


def _division_and_round(division: str | None, round_: str | None) -> str:
    parts = [_md(division)] if division else []
    if round_ in _ROUND_WORDS:
        parts.append(_ROUND_WORDS[round_])
    return f" ({', '.join(parts)})" if parts else ""


async def _submission(e: Entry, look: Lookups) -> str | None:
    f = e.facts
    title, song_division = await look.song(f.get("song_id"))
    event = await look.event(f.get("event_id"))
    if e.op == CREATED:
        tail = _division_and_round(f.get("division") or song_division, f.get("round"))
        return f"📨 submitted {_quoted(title)} to {event}{tail}"
    if e.op == DELETED:
        tail = _division_and_round(f.get("division") or song_division, f.get("round"))
        return f"↩️ withdrew {_quoted(title)} from {event}{tail}"
    if e.op == UPDATED and "to" in f:
        return (
            f"🔀 moved {_quoted(title)} at {event} from "
            f"{_md(f.get('from') or 'no division')} to {_md(f['to'])}"
        )
    return None


async def _partner(e: Entry, look: Lookups) -> str | None:
    f = e.facts
    name = _md(_person(f.get("first_name"), f.get("last_name")) or "a partner")
    noun = "team entry" if f.get("kind") == "team" else "partner"
    if e.op == CREATED:
        return f"👤 added {noun} {name}"
    if e.op == UPDATED:
        fields = _fields(f)
        return f"✏️ edited {noun} {name} ({fields})" if fields else None
    if e.op == DELETED:
        return f"🗑️ removed {noun} {name}"
    return None


async def _team(e: Entry, look: Lookups) -> str | None:
    f = e.facts
    name = f"'{_md(f.get('identifier') or 'a team')}'"
    if e.op == CREATED:
        return f"👥 created team {name}"
    if e.op == UPDATED and "from" in f:
        return f"✏️ renamed team '{_md(f['from'])}' to {name}"
    if e.op == DELETED:
        return f"🗑️ deleted team {name}"
    return None


async def _managed(e: Entry, look: Lookups) -> str | None:
    f = e.facts
    pair = _pair(f)
    if e.op == CREATED:
        return f"🤝 created managed partnership {pair}"
    if e.op == UPDATED:
        before = _pair(f, "from_")
        if before != pair:
            return f"✏️ renamed managed partnership {before} to {pair}"
        return None
    if e.op == DELETED:
        count = f.get("songs", 0)
        tail = f" and its {count} song{'s' if count != 1 else ''}" if count else ""
        return f"🗑️ deleted managed partnership {pair}{tail}"
    return None


async def _user(e: Entry, look: Lookups) -> str | None:
    f = e.facts
    if "admin" in f:
        name = await look.user(f.get("id"))
        if f["admin"]:
            return f"🛡️ made {name} an admin"
        return f"🛡️ removed {name}'s admin role"
    if e.op != UPDATED:
        return None
    fields = _fields(f)
    if not fields:
        return None
    if f.get("id") == e.actor_id:
        if set(f.get("fields", [])) <= {"first_name", "last_name"}:
            return "✏️ updated their name"
        return f"✏️ updated their profile ({fields})"
    return f"✏️ updated {await look.user(f.get('id'))}'s profile ({fields})"


async def _event(e: Entry, look: Lookups) -> str | None:
    f = e.facts
    name = _md(f.get("name") or "an event")
    if e.op == CREATED:
        dates = f"{f['start_date']} – {f['end_date']}" if f.get("start_date") else ""
        return f"📅 created event {name}" + (f" ({dates})" if dates else "")
    if e.op == UPDATED:
        fields = _fields(f)
        return f"✏️ edited event {name} ({fields})" if fields else None
    if e.op == DELETED:
        return f"🗑️ deleted event {name}"
    return None


async def _floor_session(e: Entry, look: Lookups) -> str | None:
    f = e.facts
    name = _md(f.get("name") or "a session")
    if not f.get("name") and f.get("id"):
        name, _ = await look.session(f["id"])
    where = f" at {await look.event(f['event_id'])}" if f.get("event_id") else ""
    if e.op == CREATED:
        return f"🕒 created session {name}{where}"
    if e.op == UPDATED:
        fields = f.get("fields", [])
        if fields == ["status"] and f.get("status"):
            status = _md(str(f["status"]).replace("_", " "))
            return f"🕒 set session {name}{where} to {status}"
        named = _fields(f)
        return f"✏️ edited session {name}{where} ({named})" if named else None
    if e.op == DELETED:
        return f"🗑️ deleted session {name}{where}"
    return None


def _registration_marks(f: dict[str, Any]) -> str:
    marks: list[str] = []
    for column in f.get("fields", []):
        if column == "handled":
            marks.append("handled" if f.get("handled") else "not handled")
        elif column == "scratched":
            marks.append("scratched" if f.get("scratched") else "unscratched")
        elif column == "linked_label":
            label = f.get("linked_label")
            marks.append(f"linked to {_md(label)}" if label else "unlinked")
        elif column == "note":
            marks.append("note")
        else:
            marks.append(_FIELD_LABELS.get(column, column.replace("_", " ")))
    return ", ".join(dict.fromkeys(marks))


async def _registration(e: Entry, look: Lookups) -> str | None:
    f = e.facts
    event = await look.event(f.get("event_id"))
    if e.op == CREATED:
        return f"📋 added {_dancers(f)} to {event}'s registration list"
    if e.op == DELETED:
        return f"📋 removed {_dancers(f)} from {event}'s registration list"
    if e.op == UPDATED:
        marks = _registration_marks(f)
        return f"📋 marked {_dancers(f)} at {event}: {marks}" if marks else None
    if e.op == SUMMARY:
        added, removed = f.get("added", 0), f.get("removed", 0)
        if f.get("kind") == "append":
            skipped = f.get("skipped", 0)
            tail = f" ({skipped} already on it)" if skipped else ""
            return f"📋 added {added} entries to {event}'s registration list{tail}"
        return (
            f"📋 imported {event}'s registration list: "
            f"{added} added, {removed} removed, {f.get('kept', 0)} unchanged"
        )
    return None


async def _staff(e: Entry, look: Lookups) -> str | None:
    f = e.facts
    who = await look.user(f.get("user_id"))
    role = _ROLE_WORDS.get(f.get("role") or "", _md(f.get("role") or "staff"))
    event = await look.event(f.get("event_id"))
    if e.op == CREATED:
        return f"🎟️ made {who} {role} at {event}"
    if e.op == DELETED:
        return f"🎟️ removed {who} as {role} at {event}"
    return None


_LINES: dict[str, Line] = {
    "songs": _song,
    "event_song_submissions": _submission,
    "partners": _partner,
    "teams": _team,
    "managed_partnerships": _managed,
    "users": _user,
    "events": _event,
    "sessions": _floor_session,
    "event_registrations": _registration,
    "event_staff_grants": _staff,
}

_FALLBACK_WORDS = {
    CREATED: ("created", "created"),
    UPDATED: ("updated", "updated"),
    DELETED: ("deleted", "deleted"),
    BULK: ("bulk write", "bulk writes"),
}


def _fallback(table: str, counts: Counter[str]) -> str:
    parts = [
        f"{counts[op]} {one if counts[op] == 1 else many}"
        for op, (one, many) in _FALLBACK_WORDS.items()
        if counts.get(op)
    ]
    return f"⚙️ {_TABLE_WORDS.get(table, table)} · {', '.join(parts)}"


# ---------------------------------------------------------------------------
# Folding one person's rows into lines
# ---------------------------------------------------------------------------


def fold(entries: list[Entry]) -> list[Entry]:
    """Drop what another row already says; join "song added" to its submission."""
    covered: dict[str | None, set[str]] = {}
    described: set[str | None] = set()
    for e in entries:
        if e.request_id is None:
            continue
        if e.facts.get("covers"):
            covered.setdefault(e.request_id, set()).update(e.facts["covers"])
        if e.op != BULK:
            described.add(e.request_id)

    kept: list[Entry] = []
    for e in entries:
        if e.request_id is not None:
            if e.table in covered.get(e.request_id, set()) and not e.facts.get(
                "covers"
            ):
                continue
            if e.op == BULK and e.request_id in described:
                continue
        kept.append(e)

    added = {
        e.facts.get("song_id"): e for e in kept if e.op == ADDED and e.table == "songs"
    }
    joined: list[Entry] = []
    for e in kept:
        song = added.get(e.facts.get("song_id"))
        if (
            song is not None
            and e.table == "event_song_submissions"
            and e.op == CREATED
            and e.facts.get("song_id")
        ):
            song.facts.setdefault("events", []).append(e.facts.get("event_id"))
            continue
        joined.append(e)
    return joined


async def lines_for(entries: list[Entry], look: Lookups) -> list[str]:
    """One person's claimed rows, as the message's lines, oldest first."""
    folded = fold(entries)

    # Many rows of one kind at one event are one count.
    groups = Counter(
        (e.table, e.op, e.facts.get("event_id"))
        for e in folded
        if e.table == "event_registrations" and e.op in (CREATED, DELETED, UPDATED)
    )
    lines: list[str] = []
    counted: set[tuple[str, str, Any]] = set()
    fallback: dict[str, Counter[str]] = {}
    for e in folded:
        key = (e.table, e.op, e.facts.get("event_id"))
        if key in groups and groups[key] > _COLLAPSE_OVER:
            if key not in counted:
                counted.add(key)
                event = await look.event(key[2])
                verb = {
                    CREATED: f"added {groups[key]} entries to",
                    DELETED: f"removed {groups[key]} entries from",
                    UPDATED: f"marked {groups[key]} entries on",
                }[e.op]
                lines.append(f"📋 {verb} {event}'s registration list")
            continue
        if e.op == ADDED and e.table == "songs":
            lines.append(await _song_added(e, look))
            continue
        render = _LINES.get(e.table)
        if render is None or e.op == BULK:
            fallback.setdefault(e.table, Counter())[e.op] += 1
            continue
        line = await render(e, look)
        if line:
            lines.append(line)
    lines.extend(_fallback(table, counts) for table, counts in sorted(fallback.items()))
    return lines


def _fit(lines: list[str]) -> str:
    """The lines, cut to fit an embed, with how many were left out."""
    out: list[str] = []
    used = 0
    for i, line in enumerate(lines):
        if used + len(line) + 1 > _DESCRIPTION_LIMIT:
            out.append(f"…and {len(lines) - i} more")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


async def message_for(
    actor_id: str, entries: list[Entry], look: Lookups
) -> dict[str, Any] | None:
    """The embed for one person's rows, or None when nothing is worth saying."""
    lines = await lines_for(entries, look)
    if not lines:
        return None
    return {
        "embeds": [
            {
                # Not escaped: a title shows its text as typed, and an
                # embed never pings.
                "title": await _actor_name(actor_id),
                "color": notifications._ANNOUNCE_COLOR,
                "description": _fit(lines),
                "footer": {"text": notifications.footer()},
            }
        ],
        "allowed_mentions": dict(discord.NO_MENTIONS),
    }


async def _actor_name(actor_id: str) -> str:
    """Who made the changes: their name, or their id when they have none or
    the read fails."""
    try:
        return await notifications.user_display_name(actor_id) or actor_id
    except Exception:  # noqa: BLE001 - the id names them well enough
        logger.exception("activity_feed: could not name the actor")
        return actor_id


def feed_enabled() -> bool:
    """Whether the change feed is posted: production, switched on.

    ``NOTIFY_DATA_CHANGES`` and ``DISCORD_NOTIFY_EVENTS`` mute changes only;
    "song added" still posts from production (:func:`_postable`).
    """
    settings = get_settings()
    return (
        _in_production()
        and settings.NOTIFY_DATA_CHANGES
        and notifications.event_enabled(notifications.DATA_CHANGED, settings)
    )


def _in_production() -> bool:
    return get_settings().ENVIRONMENT.lower() == "production"


def _postable(entries: list[Entry]) -> list[Entry]:
    """The rows that are posted: all of them with the feed on, else "song
    added" only, which the switches never muted."""
    if feed_enabled():
        return entries
    return [e for e in entries if e.op == ADDED]


async def post_due(
    maker: async_sessionmaker[AsyncSession], *, now: int | None = None
) -> int:
    """Post one message for each person quiet for a minute; returns how many.

    Called by the scheduler each tick. The rows are claimed (marked posted)
    first, so a failed delivery is not retried and nothing is posted twice.
    Never raises past a person: one bad row does not hold up the rest.
    """
    due = await change_feed.claim_due(maker, now=now)
    if not due:
        return 0
    if not _in_production():
        count = sum(map(len, due.values()))
        logger.info(f"activity_feed: {count} change(s) not posted outside production")
        return 0
    sent = 0
    async with maker() as db:
        look = Lookups(db)
        for actor_id, claimed in due.items():
            entries = _postable(claimed)
            if not entries:
                continue
            try:
                payload = await message_for(actor_id, entries, look)
            except Exception as exc:  # noqa: BLE001 - one person's rows, not the feed
                logger.error(
                    with_log_prefix(
                        LOG_FAILURE,
                        f"activity_feed_render_failed actor={actor_id}: {exc!r}",
                    )
                )
                continue
            if payload is None:
                continue
            if await notifications.send(
                discord.CHANNEL_ACTIVITY, payload, "activity/change"
            ):
                sent += 1
        await db.rollback()
    return sent
