"""The change feed's lines (services/activity_feed.py), against names held in
memory instead of the database."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from api_deejaytools.services import activity_feed
from api_deejaytools.services.change_feed import (
    ADDED,
    BULK,
    CREATED,
    DELETED,
    SUMMARY,
    UPDATED,
    Entry,
)


class FakeLookups(activity_feed.Lookups):
    """Names by id from dicts; an id not held is gone, as in the database."""

    def __init__(self) -> None:
        self.users = {"u1": "Jane Doe", "u2": "Dee Jay"}
        self.events = {"e1": "The Open"}
        self.songs = {"s1": ("Ballad", "Classic")}
        self.floors = {"f1": ("Friday", "e1")}

    async def user(self, user_id: str | None) -> str:
        if not user_id:
            return "someone"
        return self.users.get(user_id, user_id)

    async def event(self, event_id: str | None) -> str:
        if not event_id:
            return "an event"
        return self.events.get(event_id, "a deleted event")

    async def song(self, song_id: str | None) -> tuple[str | None, str | None]:
        return self.songs.get(song_id or "", (None, None))

    async def session(self, session_id: str | None) -> tuple[str, str | None]:
        return self.floors.get(session_id or "", ("a session", None))


def _lines(*entries: Entry) -> list[str]:
    return asyncio.run(activity_feed.lines_for(list(entries), FakeLookups()))


def _e(
    table: str, op: str, n: int = 1, request: str | None = None, **facts: Any
) -> Entry:
    return Entry(
        id=n, actor_id="u1", request_id=request, table=table, op=op, facts=facts
    )


@pytest.mark.parametrize(
    ("entry", "line"),
    [
        (_e("songs", CREATED, id="s1"), "🎵 created song 'Ballad'"),
        (_e("songs", CREATED, id="gone", routine_name="Old"), "🎵 created song 'Old'"),
        (_e("songs", DELETED, id="gone"), "🗑️ deleted a song"),
        (
            _e(
                "event_song_submissions",
                CREATED,
                song_id="s1",
                event_id="e1",
                division="Classic",
                round="finals_only",
            ),
            "📨 submitted 'Ballad' to The Open (Classic, finals only)",
        ),
        (
            _e("event_song_submissions", DELETED, song_id="s1", event_id="gone"),
            "↩️ withdrew 'Ballad' from a deleted event (Classic)",
        ),
        (
            _e(
                "partners", UPDATED, first_name="Jt", kind="team", fields=["first_name"]
            ),
            "✏️ edited team entry Jt (first name)",
        ),
        (_e("teams", CREATED, identifier="Crew"), "👥 created team 'Crew'"),
        (_e("users", UPDATED, id="u2", admin=False), "🛡️ removed Dee Jay's admin role"),
        (
            _e("users", UPDATED, id="u1", fields=["first_name", "display_name"]),
            "✏️ updated their profile (first name, display name)",
        ),
        (
            _e("users", UPDATED, id="u2", fields=["last_name"]),
            "✏️ updated Dee Jay's profile (last name)",
        ),
        (_e("events", CREATED, name="Spring"), "📅 created event Spring"),
        (
            _e("events", UPDATED, name="Spring", fields=["start_date", "end_date"]),
            "✏️ edited event Spring (start date, end date)",
        ),
        (
            _e("sessions", CREATED, name="Friday", event_id="e1"),
            "🕒 created session Friday at The Open",
        ),
        (
            _e(
                "sessions",
                UPDATED,
                id="f1",
                event_id="e1",
                fields=["status"],
                status="in_progress",
            ),
            "🕒 set session Friday at The Open to in progress",
        ),
        (
            _e("sessions", UPDATED, name="Friday", fields=["divisions"]),
            "✏️ edited session Friday (divisions)",
        ),
        (_e("sessions", DELETED, name="Friday"), "🗑️ deleted session Friday"),
        (
            _e(
                "event_registrations",
                CREATED,
                event_id="e1",
                dancer_1="Team A",
                division="Team",
            ),
            "📋 added Team A (Team) to The Open's registration list",
        ),
        (
            _e(
                "event_registrations",
                UPDATED,
                event_id="e1",
                dancer_1="A",
                dancer_2="B",
                scratched=False,
                linked_label=None,
                fields=["scratched", "linked_label", "note", "division"],
            ),
            "📋 marked A & B at The Open: unscratched, unlinked, note, division",
        ),
        (
            _e(
                "event_registrations",
                UPDATED,
                event_id="e1",
                dancer_1="A",
                scratched=True,
                handled=False,
                linked_label="A & B",
                fields=["scratched", "handled", "linked_label"],
            ),
            "📋 marked A at The Open: scratched, not handled, linked to A & B",
        ),
        (
            _e(
                "event_registrations",
                SUMMARY,
                event_id="e1",
                kind="append",
                added=3,
                skipped=2,
            ),
            "📋 added 3 entries to The Open's registration list (2 already on it)",
        ),
        (
            _e(
                "event_staff_grants",
                CREATED,
                event_id="e1",
                user_id="u2",
                role="coordinator",
            ),
            "🎟️ made Dee Jay a coordinator at The Open",
        ),
        (
            _e(
                "event_staff_grants", DELETED, event_id="e1", user_id="u2", role="judge"
            ),
            "🎟️ removed Dee Jay as judge at The Open",
        ),
        (_e("drive_jobs", UPDATED), "⚙️ Drive jobs · 1 updated"),
        (_e("songs", BULK), "⚙️ songs · 1 bulk write"),
    ],
)
def test_each_change_reads_as_a_line(entry: Entry, line: str) -> None:
    assert _lines(entry) == [line]


@pytest.mark.parametrize(
    "entry",
    [
        _e("songs", UPDATED, id="s1", fields=[]),
        _e("songs", "summary"),
        _e("event_song_submissions", UPDATED, song_id="s1"),
        _e("partners", UPDATED, first_name="P"),
        _e("partners", BULK),
        _e("teams", UPDATED, identifier="Same"),
        _e(
            "managed_partnerships",
            UPDATED,
            leader_first_name="L",
            from_leader_first_name="L",
        ),
        _e("managed_partnerships", "summary"),
        _e("users", CREATED, id="u1"),
        _e("users", UPDATED, id="u1"),
        _e("events", UPDATED, name="E"),
        _e("events", "summary"),
        _e("sessions", UPDATED, name="S"),
        _e("sessions", "summary"),
        _e("event_registrations", UPDATED, event_id="e1", fields=[]),
        _e("event_registrations", "other", event_id="e1"),
        _e("event_staff_grants", UPDATED),
        _e("teams", "summary"),
    ],
)
def test_a_change_with_nothing_to_say_is_left_out(entry: Entry) -> None:
    if entry.op == BULK:
        assert _lines(entry) == ["⚙️ partners · 1 bulk write"]
    else:
        assert _lines(entry) == []


def test_many_entries_at_one_event_are_one_count() -> None:
    added = [
        _e("event_registrations", CREATED, n, event_id="e1", dancer_1=f"D{n}")
        for n in range(5)
    ]
    marked = [
        _e("event_registrations", UPDATED, 10 + n, event_id="e1", fields=["handled"])
        for n in range(4)
    ]
    removed = [
        _e("event_registrations", DELETED, 20 + n, event_id="e1") for n in range(4)
    ]
    one = _e("event_registrations", DELETED, 30, event_id="e2", dancer_1="Solo")
    assert _lines(*added, *marked, *removed, one) == [
        "📋 added 5 entries to The Open's registration list",
        "📋 marked 4 entries on The Open's registration list",
        "📋 removed 4 entries from The Open's registration list",
        "📋 removed Solo from a deleted event's registration list",
    ]


def test_song_added_with_no_event_or_file() -> None:
    assert _lines(_e("songs", ADDED, song_id="s9", routine=None)) == ["🎵 added a song"]


def test_a_person_with_nothing_worth_saying_gets_no_message() -> None:
    message = asyncio.run(
        activity_feed.message_for("u1", [_e("songs", UPDATED, id="s1")], FakeLookups())
    )
    assert message is None


def test_lookups_without_an_id_name_nothing() -> None:
    look = activity_feed.Lookups(db=None)  # type: ignore[arg-type]

    async def run() -> tuple[Any, ...]:
        return (
            await look.user(None),
            await look.event(None),
            await look.song(None),
            await look.session(None),
        )

    assert asyncio.run(run()) == (
        "someone",
        "an event",
        (None, None),
        ("a session", None),
    )
