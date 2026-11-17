"""Folding one person's rows into lines (services/activity_feed.fold), and
the capture's lifetime (services/change_feed.begin/end)."""

from __future__ import annotations

import asyncio
import contextvars
from types import SimpleNamespace
from typing import Any

from api_deejaytools.services import activity_feed, change_feed
from api_deejaytools.services.change_feed import (
    ADDED,
    BULK,
    CREATED,
    DELETED,
    Entry,
)


def _entry(n: int, table: str, op: str, request: str | None, **facts: Any) -> Entry:
    return Entry(
        id=n, actor_id="u1", request_id=request, table=table, op=op, facts=facts
    )


def test_a_record_drops_what_it_covers_in_its_own_request() -> None:
    song = _entry(
        1,
        "songs",
        DELETED,
        "r1",
        id="s1",
        covers=["songs", "event_song_submissions"],
    )
    cascade = _entry(2, "event_song_submissions", BULK, "r1")
    own_update = _entry(3, "songs", BULK, "r1")
    other_request = _entry(4, "event_song_submissions", CREATED, "r2", song_id="s2")
    assert activity_feed.fold([song, cascade, own_update, other_request]) == [
        song,
        other_request,
    ]


def test_a_described_request_drops_its_bulk_statements() -> None:
    described = _entry(1, "event_registrations", CREATED, "r1")
    side_effect = _entry(2, "event_song_submissions", BULK, "r1")
    alone = _entry(3, "session_divisions", BULK, "r2")
    assert activity_feed.fold([described, side_effect, alone]) == [described, alone]


def test_song_added_takes_in_its_submission() -> None:
    added = _entry(1, "songs", ADDED, None, song_id="s1", routine="R")
    submitted = _entry(
        2, "event_song_submissions", CREATED, "r1", song_id="s1", event_id="e1"
    )
    unrelated = _entry(
        3, "event_song_submissions", CREATED, "r2", song_id="s2", event_id="e1"
    )
    assert activity_feed.fold([added, submitted, unrelated]) == [added, unrelated]
    assert added.facts["events"] == ["e1"]


def test_long_messages_say_how_many_were_left_out() -> None:
    lines = [f"line {i} " + "x" * 100 for i in range(100)]
    text = activity_feed._fit(lines)
    assert len(text) <= 4096
    assert text.endswith("more")
    shown = text.count("\n")
    assert text.endswith(f"…and {100 - shown} more")


def test_changed_fields_are_named_in_words() -> None:
    assert (
        activity_feed._fields({"fields": ["routine_name", "division", "partner_id"]})
        == "routine name, division, partner"
    )


def test_the_capture_closes_for_work_the_request_started() -> None:
    """A task the request starts inherits its context; its writes are not
    the person's, so the capture is closed for it once the request ends."""
    request = SimpleNamespace(state=SimpleNamespace(caller="u1"))
    token = change_feed.begin(request, ignored={"drive_jobs"})
    inherited = contextvars.copy_context()
    assert change_feed._active() is not None
    change_feed.end(token)
    assert change_feed._active() is None
    assert inherited.run(change_feed._active) is None


def test_the_capture_is_per_request() -> None:
    async def one(name: str) -> tuple[str | None, str]:
        token = change_feed.begin(SimpleNamespace(state=SimpleNamespace(caller=name)))
        await asyncio.sleep(0)
        capture = change_feed._active()
        assert capture is not None
        change_feed.end(token)
        return capture.actor(), capture.request_id

    async def both() -> list[tuple[str | None, str]]:
        return list(await asyncio.gather(one("a"), one("b")))

    (a, ra), (b, rb) = asyncio.run(both())
    assert (a, b) == ("a", "b")
    assert ra != rb
