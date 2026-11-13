"""Which entries an add-only registration import appends, and which it skips."""

from __future__ import annotations

from api_deejaytools.routers.admin_event_registrations import (
    RegistrationItem,
    entries_to_add,
    entry_key,
)


def _item(
    division: str, dancer_1: str, dancer_2: str | None = None
) -> RegistrationItem:
    body = {"division": division, "dancer_1": dancer_1}
    if dancer_2 is not None:
        body["dancer_2"] = dancer_2
    return RegistrationItem.model_validate(body)


def test_appends_new_entries_in_the_order_sent() -> None:
    items = [_item("Masters", "Cara"), _item("Classic", "Ada", "Bob")]
    added, skipped = entries_to_add([], items)
    assert [(i.dancer_1, d2, key) for i, d2, key in added] == [
        ("Cara", None, entry_key("Masters", "Cara", None)),
        ("Ada", "Bob", entry_key("Classic", "Ada", "Bob")),
    ]
    assert skipped == 0


def test_skips_entries_already_on_the_list_as_the_replace_import_matches_them() -> None:
    existing = [entry_key("Classic", "Ada Lovelace", "Bob Jones")]
    items = [
        _item("CLASSIC", "  Ada   Lovelace ", "bob jónes"),
        _item("Classic", "Cara"),
    ]
    added, skipped = entries_to_add(existing, items)
    assert [i.dancer_1 for i, _, _ in added] == ["Cara"]
    assert skipped == 1


def test_a_repeat_within_the_paste_counts_once() -> None:
    items = [
        _item("Classic", "Ada", "Bob"),
        _item("Classic", "Cara"),
        _item("classic", "ada", "bob"),
    ]
    added, skipped = entries_to_add([], items)
    assert [i.dancer_1 for i, _, _ in added] == ["Ada", "Cara"]
    assert skipped == 1


def test_an_empty_dancer_2_is_none() -> None:
    existing = [entry_key("Teams", "Future Wow", None)]
    added, skipped = entries_to_add(
        existing, [_item("Teams", "Future Wow", ""), _item("Teams", "Wow 2", " ")]
    )
    assert [(i.dancer_1, d2) for i, d2, _ in added] == [("Wow 2", None)]
    assert skipped == 1


def test_nothing_sent_adds_nothing() -> None:
    assert entries_to_add(["x"], []) == ([], 0)
