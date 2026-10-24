"""Entity labels on queue, check-in and run reads lead with the leader.

They are read live at floor trials, so the order is the one
``domain.entity_name_pair`` gives everywhere else.
"""

from __future__ import annotations

from api_deejaytools.queue.labels import managed_label, pair_label


def test_a_partner_who_leads_is_named_first() -> None:
    # Amelie uploaded the song; Fabio, her partner, leads.
    assert (
        pair_label("Amelie", "Gruber", "Fabio", "Zanardelli", "partner", "leader")
        == "Fabio Zanardelli & Amelie Gruber"
    )


def test_a_partner_who_follows_is_named_second() -> None:
    assert (
        pair_label("Fabio", "Zanardelli", "Amelie", "Gruber", "partner", "follower")
        == "Fabio Zanardelli & Amelie Gruber"
    )


def test_proam_follower_am_puts_the_amateur_first() -> None:
    # The follower is the amateur, so she reads first.
    assert (
        pair_label(
            "Pro",
            "Lead",
            "Am",
            "Follow",
            "partner",
            "follower",
            "ProAm FollowerAm",
        )
        == "Am Follow & Pro Lead"
    )
    assert (
        pair_label(
            "Am", "Follow", "Pro", "Lead", "partner", "leader", "ProAm FollowerAm"
        )
        == "Am Follow & Pro Lead"
    )


def test_proam_leader_am_keeps_the_leader_first() -> None:
    assert (
        pair_label(
            "Am", "Lead", "Pro", "Follow", "partner", "follower", "ProAm LeaderAm"
        )
        == "Am Lead & Pro Follow"
    )


def test_a_team_or_placeholder_is_its_own_name() -> None:
    assert pair_label("Ada", "L", "Jt", "Swing", "team", "follower", "Teams") == (
        "Jt Swing"
    )
    assert pair_label("Ada", "L", "Someone", None, "other", "leader") == "Someone"


def test_a_pair_with_no_partner_name_is_the_user() -> None:
    assert pair_label("Ada", "L", None, None, "partner", "leader") == "Ada L"


def test_a_managed_partnership_reads_leader_then_follower() -> None:
    assert managed_label("Ma", "Lead", "Mi", "Fol") == "Ma Lead & Mi Fol"
    assert managed_label("Ma", "Lead", "Mi", "Fol", "Classic") == "Ma Lead & Mi Fol"


def test_a_managed_proam_follower_am_puts_the_follower_first() -> None:
    assert managed_label("Ma", "Lead", "Mi", "Fol", "ProAm FollowerAm") == (
        "Mi Fol & Ma Lead"
    )


def test_a_managed_partnership_without_a_follower_is_its_leader() -> None:
    assert managed_label("Ma", "Lead", None, None) == "Ma Lead"
    assert managed_label("Ma", "Lead", None, None, "ProAm FollowerAm") == "Ma Lead"
