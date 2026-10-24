"""Entity labels on queue, check-in and run reads (deejaytools-api routes/queue.ts,
routes/checkins.ts, routes/runs.ts).

Ordered by :func:`..domain.entity_name_pair`, the one ordering rule: leader
first, the amateur first in ProAm FollowerAm, a placeholder partner by its
own name alone.
"""

from __future__ import annotations

from ..domain import entity_display, entity_name_pair, full_name


def managed_label(
    leader_first: str | None,
    leader_last: str | None,
    follower_first: str | None,
    follower_last: str | None,
    division: str | None = None,
) -> str:
    """ "Leader & Follower" for a managed partnership (follower first in ProAm
    FollowerAm); just the leader when the follower's name is empty."""
    return entity_display(
        *entity_name_pair(
            owner_name="",
            managed_leader=full_name(leader_first, leader_last),
            managed_follower=full_name(follower_first, follower_last),
            division=division,
        )
    )


def pair_label(
    user_first: str | None,
    user_last: str | None,
    partner_first: str | None,
    partner_last: str | None,
    partner_kind: str | None,
    partner_role: str | None = None,
    division: str | None = None,
) -> str:
    """A pair: the user and their partner, leader first by the partner's
    ``partner_role``; or just the placeholder partner's name."""
    return entity_display(
        *entity_name_pair(
            owner_name=full_name(user_first, user_last),
            partner_name=full_name(partner_first, partner_last),
            partner_kind=partner_kind,
            partner_role=partner_role,
            division=division,
        )
    )
