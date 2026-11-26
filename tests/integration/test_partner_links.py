"""A partner row is linked to the account its email belongs to: on create,
on an email change, at that account's sign-in, and by migration 019 for
rows that already exist."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import asyncpg
import httpx

from tests.integration.conftest import ROOT, bearer

from .content_helpers import insert_song
from .queue_helpers import insert_partner

PAT = {"first_name": "Pat", "last_name": "Partner", "partner_role": "follower"}


async def _email(db: asyncpg.Connection, user_id: str) -> str:
    return await db.fetchval("SELECT email FROM users WHERE id = $1", user_id)


async def _create(client: httpx.AsyncClient, who: Any, email: str | None) -> dict:
    res = await client.post(
        "/v1/partners", json={**PAT, "email": email}, headers=who.headers
    )
    assert res.status_code == 201, res.text
    return res.json()["data"]


async def _patch(client: httpx.AsyncClient, who: Any, pid: str, **body: Any) -> dict:
    res = await client.patch(f"/v1/partners/{pid}", json=body, headers=who.headers)
    assert res.status_code == 200, res.text
    return res.json()["data"]


async def test_create_links_the_account_the_email_is(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    alice, bob = await person("alice"), await person("bob")
    bob_email = await _email(db, bob.id)
    row = await _create(client, alice, bob_email.upper())
    assert row["linked_user_id"] == bob.id

    song = await insert_song(db, alice.id, partner_id=row["id"])
    mine = (await client.get("/v1/songs", headers=bob.headers)).json()["data"]
    assert [(s["id"], s["shared_with_me"]) for s in mine] == [(song, True)]


async def test_create_links_nothing_for_an_unknown_or_own_email(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    alice = await person("alice")
    assert (await _create(client, alice, "nobody@example.test"))[
        "linked_user_id"
    ] is None
    own = await _create(client, alice, await _email(db, alice.id))
    assert own["linked_user_id"] is None


async def test_an_email_change_relinks_and_clearing_it_unlinks(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    alice, bob, carol = (
        await person("alice"),
        await person("bob"),
        await person("carol"),
    )
    row = await _create(client, alice, await _email(db, bob.id))
    assert row["linked_user_id"] == bob.id

    # Other fields, or the same email sent again, keep the link.
    assert (await _patch(client, alice, row["id"], first_name="Bo"))[
        "linked_user_id"
    ] == bob.id
    again = await _patch(client, alice, row["id"], email=row["email"])
    assert again["linked_user_id"] == bob.id

    moved = await _patch(client, alice, row["id"], email=await _email(db, carol.id))
    assert moved["linked_user_id"] == carol.id
    cleared = await _patch(client, alice, row["id"], email=None)
    assert cleared["linked_user_id"] is None


async def test_sign_in_links_partners_added_before_the_account(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    clerk: Any,
    new_user_id: Callable[[], str],
) -> None:
    alice, erin = await person("alice"), await person("erin")
    row = await _create(client, alice, "New.Dancer@Example.test")
    placeholder = await insert_partner(db, alice.id, kind="placeholder")
    await db.execute(
        "UPDATE partners SET email = 'new.dancer@example.test' WHERE id = $1",
        placeholder,
    )
    # A row already linked elsewhere keeps its link.
    taken = await _create(client, erin, await _email(db, alice.id))
    assert taken["linked_user_id"] == alice.id
    await db.execute(
        "UPDATE partners SET email = 'new.dancer@example.test' WHERE id = $1",
        taken["id"],
    )

    uid = new_user_id()
    res = await client.post(
        "/v1/auth/sync",
        json={"email": "new.dancer@example.test", "firstName": "New"},
        headers=bearer(clerk.token(uid)),
    )
    assert res.status_code == 200, res.text

    links = dict(
        await db.fetch(
            "SELECT id, linked_user_id FROM partners WHERE id = ANY($1::text[])",
            [row["id"], placeholder, taken["id"]],
        )
    )
    assert links == {row["id"]: uid, placeholder: None, taken["id"]: alice.id}


async def test_migration_019_links_existing_rows(
    person: Callable, db: asyncpg.Connection
) -> None:
    alice, bob = await person("alice"), await person("bob")
    bob_email = await _email(db, bob.id)
    linked = await insert_partner(db, alice.id)
    own = await insert_partner(db, alice.id)
    unknown = await insert_partner(db, alice.id)
    placeholder = await insert_partner(db, alice.id, kind="placeholder")
    for pid, email in (
        (linked, f" {bob_email.upper()}"),
        (own, await _email(db, alice.id)),
        (unknown, "nobody@example.test"),
        (placeholder, bob_email),
    ):
        await db.execute("UPDATE partners SET email = $1 WHERE id = $2", email, pid)

    sql = (ROOT / "migrations" / "019_link_partners_by_email.sql").read_text()
    await db.execute(sql)
    await db.execute(sql)  # idempotent

    links = dict(
        await db.fetch(
            "SELECT id, linked_user_id FROM partners WHERE id = ANY($1::text[])",
            [linked, own, unknown, placeholder],
        )
    )
    assert links == {linked: bob.id, own: None, unknown: None, placeholder: None}
