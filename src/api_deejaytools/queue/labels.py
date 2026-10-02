"""Entity labels on queue, check-in and run reads (deejaytools-api routes/queue.ts,
routes/checkins.ts, routes/runs.ts)."""

from __future__ import annotations

from ..domain import full_name, partnership_display


def managed_label(
    leader_first: str | None,
    leader_last: str | None,
    follower_first: str | None,
    follower_last: str | None,
) -> str:
    """ "Leader & Follower" for a managed partnership; just the leader when the
    follower's name is empty."""
    leader = full_name(leader_first, leader_last)
    follower = full_name(follower_first, follower_last)
    return f"{leader} & {follower}" if follower else leader


def pair_label(
    user_first: str | None,
    user_last: str | None,
    partner_first: str | None,
    partner_last: str | None,
    partner_kind: str | None,
) -> str:
    """A pair: leader user and partner, or just the placeholder partner's name."""
    return partnership_display(
        full_name(user_first, user_last),
        full_name(partner_first, partner_last),
        partner_kind,
    )
