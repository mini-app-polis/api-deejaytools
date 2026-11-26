"""Linking a partner row to the account its email belongs to.

A partner row in A's address book is linked (``partners.linked_user_id``)
to B's account when the row's email is B's ``users.email``, compared
trimmed and in any case. The link is what shares A's songs with that
partner with B (:func:`song_records.shared_with`), so B sees them in My
Content, may submit them to events and check them in.

The link is kept in step with the email from both ends:

- adding a partner, or changing their email, links the row to the account
  that email belongs to, or unlinks it when no account has it
  (:func:`account_for_email`);
- an account's sign-in (``POST /v1/auth/sync``) links every unlinked row,
  in anyone's book, whose email is the account's
  (:func:`link_rows_to_account`), so a partner added before they had an
  account is linked once they make one.

Only ``kind = 'partner'`` rows are linked; placeholders stand for no one.
A row is never linked to its own owner. An email that, ignoring case,
belongs to more than one account links to none of them.
"""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Partner, User


def _normal(value: str) -> str:
    return value.strip().lower()


async def account_for_email(
    session: AsyncSession, email: str | None, *, owner_id: str
) -> str | None:
    """The ``users.id`` whose email is ``email`` (trimmed, any case), unless
    that is ``owner_id`` itself, none has it, or more than one does."""
    if email is None or not _normal(email):
        return None
    ids = (
        (
            await session.execute(
                select(User.id)
                .where(func.lower(func.btrim(User.email)) == _normal(email))
                .limit(2)
            )
        )
        .scalars()
        .all()
    )
    if len(ids) != 1 or ids[0] == owner_id:
        return None
    return ids[0]


async def link_rows_to_account(session: AsyncSession, user_id: str, email: str) -> None:
    """Link every unlinked partner row whose email is ``email`` to
    ``user_id``, in every book but the account's own. Does not commit.

    Rows already linked keep their link. Nothing is linked when the email,
    ignoring case, belongs to another account as well.
    """
    if await account_for_email(session, email, owner_id="") != user_id:
        return
    await session.execute(
        update(Partner)
        .where(
            Partner.linked_user_id.is_(None),
            Partner.kind == "partner",
            Partner.user_id != user_id,
            func.lower(func.btrim(Partner.email)) == _normal(email),
        )
        .values(linked_user_id=user_id)
        .execution_options(synchronize_session=False)
    )
