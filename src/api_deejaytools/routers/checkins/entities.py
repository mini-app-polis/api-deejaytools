"""The entity a check-in is for, and the couple rule.

A managed partnership must be the caller's; a song's pair is created if
missing. A couple holds one entry per session, whichever partner's pair
holds it: a pair's partner row stands for the other's account by link or,
unlinked, by email.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ColumnElement, and_, func, insert, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ...errors import bad_request
from ...models import ManagedPartnership, Pair, Partner, QueueEntry, User
from ...queue import EntityRef


async def _managed_entity(
    db: AsyncSession, managed_partnership_id: str, user_id: str
) -> EntityRef:
    managed = (
        await db.execute(
            select(ManagedPartnership.id, ManagedPartnership.user_id)
            .where(
                ManagedPartnership.id == managed_partnership_id,
                ManagedPartnership.deleted_at.is_(None),
            )
            .limit(1)
        )
    ).first()
    if managed is None or managed.user_id != user_id:
        raise bad_request("Managed partnership not found")
    return EntityRef(managed_partnership_id=managed_partnership_id)


async def _song_pair(db: AsyncSession, owner_id: str, partner_id: str, now: int) -> str:
    """The pair of a song's owner and its partner, created if missing.

    The pair is written outside the check-in transaction: it stays even when
    the check-in is refused afterwards.
    """
    pair_id = (
        await db.execute(
            select(Pair.id)
            .where(Pair.user_a_id == owner_id, Pair.partner_b_id == partner_id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if pair_id is None:
        pair_id = str(uuid.uuid4())
        await db.execute(
            insert(Pair).values(
                id=pair_id,
                user_a_id=owner_id,
                partner_b_id=partner_id,
                created_at=now,
            )
        )
        await db.commit()
    return pair_id


def _stands_for(row: Any, user: Any) -> ColumnElement[bool]:
    """Partner row ``row`` stands for account ``user``: it is linked to that
    account, or, unlinked, its email is the account's (trimmed, any case).

    For the couple rule only; access to songs and check-ins goes by the link
    alone.
    """
    row_email = func.lower(func.trim(row.email))
    return or_(
        row.linked_user_id == user.id,
        and_(
            row.linked_user_id.is_(None),
            row_email != "",
            row_email == func.lower(func.trim(user.email)),
        ),
    )


async def _couple_has_live_entry(
    db: AsyncSession, pair_id: str, session_id: str
) -> bool:
    """Whether the other half of pair ``pair_id`` holds a live entry in this
    session with a pair of their own that stands for the same couple.

    Pair ``pair_id`` is led by A with a partner row standing for account B;
    the couple is checked in when a pair led by B, whose partner row stands
    for A, has a queue entry here. The pair itself is the single-entry
    rule's (:func:`entity_has_live_entry`), so it is not counted again.
    """
    mine = aliased(Pair, name="mine")
    my_row = aliased(Partner, name="my_row")
    leader = aliased(User, name="leader")
    theirs = aliased(Pair, name="theirs")
    their_row = aliased(Partner, name="their_row")
    other = aliased(User, name="other")
    row = (
        await db.execute(
            select(QueueEntry.id)
            .select_from(mine)
            .join(my_row, my_row.id == mine.partner_b_id)
            .join(leader, leader.id == mine.user_a_id)
            .join(theirs, theirs.user_a_id != mine.user_a_id)
            .join(other, other.id == theirs.user_a_id)
            .join(their_row, their_row.id == theirs.partner_b_id)
            .join(QueueEntry, QueueEntry.entity_pair_id == theirs.id)
            .where(
                mine.id == pair_id,
                QueueEntry.session_id == session_id,
                _stands_for(my_row, other),
                _stands_for(their_row, leader),
            )
            .limit(1)
        )
    ).first()
    return row is not None
