"""/v1/admin/users: the user directory, role changes, and per-user reads."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import asyncpg
import httpx

from .conftest import TEST_ISSUER, Clerk, bearer
from .content_helpers import insert_event, insert_song

URL = "/v1/admin/users"


async def _set_created(db: asyncpg.Connection, user_id: str, created_at: int) -> None:
    await db.execute(
        "UPDATE users SET created_at = $2 WHERE id = $1", user_id, created_at
    )


async def _partner(
    db: asyncpg.Connection,
    user_id: str,
    pid: str,
    first: str,
    last: str,
    *,
    kind: str = "partner",
    linked_user_id: str | None = None,
) -> None:
    await db.execute(
        "INSERT INTO partners (id, user_id, first_name, last_name, kind,"
        " linked_user_id, created_at, updated_at) VALUES ($1, $2, $3, $4, $5, $6, 5, 6)",
        pid,
        user_id,
        first,
        last,
        kind,
        linked_user_id,
    )


async def _admin_roles(db: asyncpg.Connection, user_id: str) -> list[str]:
    rows = await db.fetch(
        "SELECT r.role_name FROM identity_principal_roles r"
        " JOIN identity_principals p ON p.id = r.principal_id"
        " WHERE p.subject = $1 AND p.issuer = $2 ORDER BY r.role_name",
        user_id,
        TEST_ISSUER,
    )
    return [r["role_name"] for r in rows]


async def _users_role(db: asyncpg.Connection, user_id: str) -> str:
    return str(await db.fetchval("SELECT role FROM users WHERE id = $1", user_id))


# ---------------------------------------------------------------------------
# GET /v1/admin/users
# ---------------------------------------------------------------------------


async def test_list_requires_a_token(client: httpx.AsyncClient) -> None:
    res = await client.get(URL)
    assert res.status_code == 401
    assert res.json() == {
        "error": {"code": "UNAUTHORIZED", "message": "Authentication required"}
    }


async def test_list_refuses_a_non_admin(
    client: httpx.AsyncClient, person: Callable
) -> None:
    alice = await person("alice")
    res = await client.get(URL, headers=alice.headers)
    assert res.status_code == 403
    assert res.json() == {
        "error": {"code": "FORBIDDEN", "message": "Admin access required"}
    }


async def test_list_shape_counts_order_and_derived_role(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    alice = await person("alice")
    await _set_created(db, admin.id, 200)
    await _set_created(db, alice.id, 100)
    # users.role says admin, but there is no grant: the wire says "user".
    await db.execute("UPDATE users SET role = 'admin' WHERE id = $1", alice.id)
    await insert_song(db, alice.id)
    deleted = await insert_song(db, alice.id)
    await db.execute("UPDATE songs SET deleted_at = 9 WHERE id = $1", deleted)
    await _partner(db, alice.id, "p1", "Pat", "One")
    await _partner(db, alice.id, "p2", "Solo", "Placeholder", kind="solo")

    res = await client.get(URL, headers=admin.headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["meta"] == {"version": "v1", "count": 2}
    alice_row, admin_row = body["data"]
    assert alice_row == {
        "id": alice.id,
        "email": f"alice.{alice.id}@example.test",
        "first_name": "Alice",
        "last_name": None,
        "role": "user",
        "created_at": 100,
        "song_count": 1,
        "partner_count": 1,
    }
    assert admin_row["id"] == admin.id
    assert admin_row["role"] == "admin"
    assert admin_row["song_count"] == 0
    assert admin_row["partner_count"] == 0


async def test_list_search_and_role_filter(
    client: httpx.AsyncClient, person: Callable
) -> None:
    admin = await person("admin", admin=True)
    alice = await person("alice")
    bob = await person("bob")

    async def ids(params: dict[str, Any]) -> list[str]:
        res = await client.get(URL, params=params, headers=admin.headers)
        assert res.status_code == 200, res.text
        return sorted(u["id"] for u in res.json()["data"])

    assert await ids({"q": "  ALI  "}) == [alice.id]
    assert await ids({"q": bob.id[5:12]}) == [bob.id]  # matches the email
    assert await ids({"q": "   "}) == sorted([admin.id, alice.id, bob.id])
    assert await ids({"role": "admin"}) == [admin.id]
    assert await ids({"role": "user"}) == sorted([alice.id, bob.id])
    assert await ids({"role": "user", "q": "bob"}) == [bob.id]


async def test_list_validates_the_query(
    client: httpx.AsyncClient, person: Callable
) -> None:
    admin = await person("admin", admin=True)
    for qs in ("role=owner", "q=a&q=b"):
        res = await client.get(f"{URL}?{qs}", headers=admin.headers)
        assert res.status_code == 400, qs
        assert res.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_list_checks_auth_before_the_query(
    client: httpx.AsyncClient, person: Callable
) -> None:
    alice = await person("alice")
    res = await client.get(f"{URL}?role=owner", headers=alice.headers)
    assert res.status_code == 403


# ---------------------------------------------------------------------------
# PATCH /v1/admin/users/{id}/role
# ---------------------------------------------------------------------------


async def test_grant_writes_the_store_and_the_mirror_together(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    alice = await person("alice")
    await insert_song(db, alice.id)
    await _partner(db, alice.id, "p1", "Pat", "One")

    res = await client.patch(
        f"{URL}/{alice.id}/role", json={"role": "admin"}, headers=admin.headers
    )
    assert res.status_code == 200, res.text
    created = await db.fetchval("SELECT created_at FROM users WHERE id = $1", alice.id)
    assert res.json() == {
        "data": {
            "id": alice.id,
            "email": f"alice.{alice.id}@example.test",
            "first_name": "Alice",
            "last_name": None,
            "role": "admin",
            "created_at": created,
            "song_count": 1,
            "partner_count": 1,
        },
        "meta": {"version": "v1"},
    }
    assert await _admin_roles(db, alice.id) == [
        "deejaytools-admin",
        "deejaytools-dancer",
    ]
    assert await _users_role(db, alice.id) == "admin"
    granted_by = await db.fetchval(
        "SELECT r.granted_by FROM identity_principal_roles r"
        " JOIN identity_principals p ON p.id = r.principal_id"
        " WHERE p.subject = $1 AND r.role_name = 'deejaytools-admin'",
        alice.id,
    )
    assert granted_by == admin.id

    me = await client.get("/v1/auth/me", headers=alice.headers)
    assert me.json()["data"]["role"] == "admin"
    assert (await client.get(URL, headers=alice.headers)).status_code == 200

    # Idempotent: granting again changes nothing and still answers 200.
    again = await client.patch(
        f"{URL}/{alice.id}/role", json={"role": "admin"}, headers=admin.headers
    )
    assert again.status_code == 200
    assert again.json()["data"]["role"] == "admin"
    assert await _admin_roles(db, alice.id) == [
        "deejaytools-admin",
        "deejaytools-dancer",
    ]


async def test_revoke_writes_the_store_and_the_mirror_together(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    other = await person("other", admin=True)
    await db.execute("UPDATE users SET role = 'admin' WHERE id = $1", other.id)

    res = await client.patch(
        f"{URL}/{other.id}/role", json={"role": "user"}, headers=admin.headers
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["role"] == "user"
    assert await _admin_roles(db, other.id) == ["deejaytools-dancer"]
    assert await _users_role(db, other.id) == "user"

    me = await client.get("/v1/auth/me", headers=other.headers)
    assert me.json()["data"]["role"] == "user"
    assert (await client.get(URL, headers=other.headers)).status_code == 403


async def test_self_demotion_is_refused(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    res = await client.patch(
        f"{URL}/{admin.id}/role", json={"role": "user"}, headers=admin.headers
    )
    assert res.status_code == 403
    assert res.json() == {
        "error": {
            "code": "forbidden",
            "message": "You cannot change your own admin role.",
        }
    }
    assert await _admin_roles(db, admin.id) == [
        "deejaytools-admin",
        "deejaytools-dancer",
    ]

    # Setting one's own role to admin is allowed (a no-op grant).
    ok = await client.patch(
        f"{URL}/{admin.id}/role", json={"role": "admin"}, headers=admin.headers
    )
    assert ok.status_code == 200
    assert await _users_role(db, admin.id) == "admin"


async def test_target_without_a_principal_is_provisioned(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    clerk: Clerk,
    new_user_id: Callable[[], str],
) -> None:
    """Someone who signed up through deejaytools-api during a rollback."""
    admin = await person("admin", admin=True)
    uid = new_user_id()
    await db.execute(
        "INSERT INTO users (id, email, first_name, created_at, updated_at)"
        " VALUES ($1, 'late@example.test', 'Late', 1, 1)",
        uid,
    )

    res = await client.patch(
        f"{URL}/{uid}/role", json={"role": "admin"}, headers=admin.headers
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["role"] == "admin"
    principal = await db.fetchrow(
        "SELECT kind, email, display_name FROM identity_principals"
        " WHERE issuer = $1 AND subject = $2",
        TEST_ISSUER,
        uid,
    )
    assert dict(principal) == {
        "kind": "human",
        "email": "late@example.test",
        "display_name": "late@example.test",
    }
    assert await _admin_roles(db, uid) == ["deejaytools-admin", "deejaytools-dancer"]
    assert await _users_role(db, uid) == "admin"

    me = await client.get("/v1/auth/me", headers=bearer(clerk.token(uid)))
    assert me.status_code == 200, me.text
    assert me.json()["data"]["role"] == "admin"


async def test_revoke_for_a_target_without_a_principal(
    client: httpx.AsyncClient,
    person: Callable,
    db: asyncpg.Connection,
    new_user_id: Callable[[], str],
) -> None:
    """A users.role admin with no principal is provisioned, then revoked."""
    admin = await person("admin", admin=True)
    uid = new_user_id()
    await db.execute(
        "INSERT INTO users (id, email, role, created_at, updated_at)"
        " VALUES ($1, 'late@example.test', 'admin', 1, 1)",
        uid,
    )
    res = await client.patch(
        f"{URL}/{uid}/role", json={"role": "user"}, headers=admin.headers
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["role"] == "user"
    assert await _admin_roles(db, uid) == ["deejaytools-dancer"]
    assert await _users_role(db, uid) == "user"


async def test_role_change_errors(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    alice = await person("alice")

    missing = await client.patch(
        f"{URL}/user_nobody/role", json={"role": "admin"}, headers=admin.headers
    )
    assert missing.status_code == 404
    assert missing.json() == {"error": {"code": "NOT_FOUND", "message": "Not found"}}

    for body in ({"role": "owner"}, {}, {"role": None}):
        bad = await client.patch(
            f"{URL}/{alice.id}/role", json=body, headers=admin.headers
        )
        assert bad.status_code == 400, body
        assert bad.json()["error"]["code"] == "VALIDATION_ERROR"

    # Body validation runs before the self-demotion guard, as in Node.
    bad_self = await client.patch(
        f"{URL}/{admin.id}/role", json={"role": "owner"}, headers=admin.headers
    )
    assert bad_self.status_code == 400

    assert (
        await client.patch(f"{URL}/{alice.id}/role", json={"role": "admin"})
    ).status_code == 401
    # Auth before body validation: a non-admin with a bad body is a 403.
    forbidden = await client.patch(
        f"{URL}/{admin.id}/role", json={"role": "owner"}, headers=alice.headers
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "FORBIDDEN"
    assert await _admin_roles(db, alice.id) == ["deejaytools-dancer"]


# ---------------------------------------------------------------------------
# GET /v1/admin/users/{id}/partners
# ---------------------------------------------------------------------------


async def test_partners_shape_and_order(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    alice = await person("alice")
    await _partner(db, alice.id, "p_b", "Zed", "Brown")
    await _partner(db, alice.id, "p_a2", "Bea", "Adams", linked_user_id=admin.id)
    await _partner(db, alice.id, "p_a1", "Al", "Adams")
    await _partner(db, alice.id, "p_team", "Team", "Name", kind="team")
    await _partner(db, admin.id, "p_other", "Not", "Alices")

    res = await client.get(f"{URL}/{alice.id}/partners", headers=admin.headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert [p["id"] for p in body["data"]] == ["p_a1", "p_a2", "p_b"]
    assert body["meta"] == {"version": "v1", "count": 3}
    assert body["data"][1] == {
        "id": "p_a2",
        "user_id": alice.id,
        "first_name": "Bea",
        "last_name": "Adams",
        "partner_role": "follower",
        "email": None,
        "linked_user_id": admin.id,
        "created_at": 5,
        "updated_at": 6,
        "display_name": "Bea Adams",
    }


async def test_partners_errors(client: httpx.AsyncClient, person: Callable) -> None:
    admin = await person("admin", admin=True)
    alice = await person("alice")
    res = await client.get(f"{URL}/user_nobody/partners", headers=admin.headers)
    assert res.status_code == 404
    assert res.json() == {"error": {"code": "NOT_FOUND", "message": "User not found"}}
    assert (await client.get(f"{URL}/{alice.id}/partners")).status_code == 401
    assert (
        await client.get(f"{URL}/{alice.id}/partners", headers=alice.headers)
    ).status_code == 403


# ---------------------------------------------------------------------------
# GET /v1/admin/users/{id}/event-song-submissions
# ---------------------------------------------------------------------------


async def test_submissions_for_a_user_and_event(
    client: httpx.AsyncClient, person: Callable, db: asyncpg.Connection
) -> None:
    admin = await person("admin", admin=True)
    alice = await person("alice")
    event = await insert_event(db, name="Swing Fling")
    other_event = await insert_event(db, name="Other")
    song = await insert_song(
        db, alice.id, division="Classic", season_year="2026", routine_name="Blue"
    )
    for sid, eid, created in (("s1", event, 10), ("s2", other_event, 20)):
        await db.execute(
            "INSERT INTO event_song_submissions (id, event_id, song_id,"
            " submitted_by_user_id, created_at) VALUES ($1, $2, $3, $4, $5)",
            sid,
            eid,
            song,
            alice.id,
            created,
        )

    res = await client.get(
        f"{URL}/{alice.id}/event-song-submissions",
        params={"event_id": event},
        headers=admin.headers,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["meta"] == {"version": "v1", "count": 1}
    row = body["data"][0]
    assert row["id"] == "s1"
    assert row["event_id"] == event
    assert row["event_name"] == "Swing Fling"
    assert row["song_label"] == "Alice Classic 2026 Blue"
    assert row["division"] == "Classic"
    assert row["round"] == "prelims_and_finals"
    assert row["created_at"] == 10
    assert set(row) == {
        "id",
        "event_id",
        "event_name",
        "event_start_date",
        "event_status",
        "song_id",
        "song_label",
        "division",
        "round",
        "created_at",
    }


async def test_submissions_errors(client: httpx.AsyncClient, person: Callable) -> None:
    admin = await person("admin", admin=True)
    alice = await person("alice")
    path = f"{URL}/{alice.id}/event-song-submissions"

    for qs in ("", "?event_id="):
        res = await client.get(f"{path}{qs}", headers=admin.headers)
        assert res.status_code == 400, qs
        assert res.json()["error"]["code"] == "VALIDATION_ERROR"

    # Query validation comes before the user lookup.
    assert (
        await client.get(
            f"{URL}/user_nobody/event-song-submissions", headers=admin.headers
        )
    ).status_code == 400
    missing = await client.get(
        f"{URL}/user_nobody/event-song-submissions?event_id=e", headers=admin.headers
    )
    assert missing.status_code == 404
    assert missing.json() == {
        "error": {"code": "NOT_FOUND", "message": "User not found"}
    }
    assert (await client.get(f"{path}?event_id=e")).status_code == 401
    assert (await client.get(f"{path}", headers=alice.headers)).status_code == 403
