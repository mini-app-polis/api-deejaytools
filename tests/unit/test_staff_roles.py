"""Per-event staff roles (ADR-0012): the pure parts of the grant model and check."""

from __future__ import annotations

import uuid

from identity.policy import authorize
from identity.types import Principal, Role
from starlette.datastructures import QueryParams

from api_deejaytools.auth import STAFF_ROLES, EventRef, event_resource, ref_value
from api_deejaytools.staff import grant_source, role_scopes, scopes_to_remove

ROLES = {
    "deejaytools-event-coordinator": Role(
        name="deejaytools-event-coordinator",
        scopes=frozenset({"deejaytools.entries.read"}),
    ),
    "deejaytools-event-dj": Role(
        name="deejaytools-event-dj",
        scopes=frozenset({"deejaytools.entries.read", "deejaytools.queue.manage"}),
    ),
    "deejaytools-admin": Role(
        name="deejaytools-admin",
        scopes=frozenset({"deejaytools.staff.write", "deejaytools.queue.manage"}),
    ),
}


def _principal(*roles: str) -> Principal:
    return Principal(
        id=uuid.uuid4(),
        kind="human",
        issuer="https://clerk.test.local",
        subject="user_x",
        roles=roles,
        status="active",
    )


def test_every_staff_role_names_an_event_role() -> None:
    assert set(STAFF_ROLES) == {"coordinator", "dj", "event-manager"}
    assert all(name.startswith("deejaytools-event-") for name in STAFF_ROLES.values())


def test_role_scopes_come_from_the_role_table() -> None:
    assert role_scopes("dj", ROLES) == ROLES["deejaytools-event-dj"].scopes
    # A definition missing from the store, or an unknown role, carries nothing.
    assert role_scopes("event-manager", ROLES) == frozenset()
    assert role_scopes("admin", ROLES) == frozenset()


def test_revoke_keeps_scopes_another_held_role_carries() -> None:
    assert scopes_to_remove("coordinator", ["dj"], ROLES) == frozenset()
    assert scopes_to_remove("dj", ["coordinator"], ROLES) == {
        "deejaytools.queue.manage"
    }
    assert scopes_to_remove("dj", [], ROLES) == ROLES["deejaytools-event-dj"].scopes
    # The revoked role still listed as remaining does not keep its own scopes.
    assert scopes_to_remove("dj", ["dj"], ROLES) == ROLES["deejaytools-event-dj"].scopes


def test_grant_source_names_the_role_and_the_granter() -> None:
    assert grant_source("dj", "user_admin") == "deejaytools-event-dj:user_admin"


def test_ref_value_reads_the_named_place() -> None:
    path = EventRef(source="path", name="id")
    body = EventRef(source="body", name="queueEntryId", kind="queue_entry")
    assert ref_value(path, {"id": "e1"}, None) == "e1"
    assert ref_value(path, {}, None) is None
    assert ref_value(body, {}, {"queueEntryId": "q1"}) == "q1"
    for bad in (None, [], {"queueEntryId": 5}, {"queueEntryId": ""}, {}):
        assert ref_value(body, {}, bad) is None


def test_ref_value_reads_a_query_parameter_sent_once() -> None:
    query = EventRef(source="query", name="event_id")
    assert ref_value(query, {}, None, QueryParams("event_id=e1")) == "e1"
    assert ref_value(query, {}, None, QueryParams("")) is None
    assert ref_value(query, {}, None, QueryParams("event_id=")) is None
    # Repeated, it names no event: the query validator refuses it.
    assert ref_value(query, {}, None, QueryParams("event_id=a&event_id=b")) is None
    assert ref_value(query, {"event_id": "e1"}, None) is None


def test_a_grant_on_one_event_does_not_reach_another() -> None:
    staff = _principal("deejaytools-dancer")
    grants = {("deejaytools.queue.manage", event_resource("a"))}
    on_a = authorize(
        staff,
        "deejaytools.queue.manage",
        ROLES,
        resource=event_resource("a"),
        explicit_grants=grants,
    )
    on_b = authorize(
        staff,
        "deejaytools.queue.manage",
        ROLES,
        resource=event_resource("b"),
        explicit_grants=grants,
    )
    assert (on_a.allowed, on_a.reason) == (True, "granted_by_explicit_grant")
    assert (on_b.allowed, on_b.reason) == (False, "no_matching_scope")


def test_a_role_scope_still_allows_on_any_event() -> None:
    admin = _principal("deejaytools-admin")
    decision = authorize(
        admin,
        "deejaytools.staff.write",
        ROLES,
        resource=event_resource("b"),
        explicit_grants=set(),
    )
    assert (decision.allowed, decision.reason) == (True, "granted_by_role")
