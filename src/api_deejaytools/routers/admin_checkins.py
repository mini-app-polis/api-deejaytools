"""``/v1/admin/checkins`` (deejaytools-api docs/API.md, src/routes/admin-checkins.ts).

Synthetic test check-ins for exercising the floor without real dancers:
inject one (stub leader user, follower partner, pair and placeholder song,
all owned by the stub user), list them, and delete them all. Injection and
deletion are behind ``deejaytools.testdata.write``, the list behind
``deejaytools.testdata.read``.

Injection bypasses the check-in window, and, as in Node, neither takes the
session lock nor auto-fills: injected entries wait until the next fill.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, ClassVar, Literal

from fastapi import APIRouter, Depends
from mini_app_polis.logger import LOG_FAILURE, get_logger, with_log_prefix
from pydantic import BaseModel, Field
from sqlalchemy import delete, desc, insert, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import Caller, require_scope
from ..database import get_db_session
from ..db_errors import driver_message
from ..errors import (
    ErrorCode,
    ErrorResponse,
    ListMeta,
    Meta,
    api_error,
    success,
    success_list,
)
from ..models import (
    Checkin,
    Pair,
    Partner,
    QueueEntry,
    QueueEvent,
    Run,
    Session,
    Song,
    User,
)
from ..queue import (
    AdmissionError,
    EntityRef,
    InitialQueue,
    determine_initial_queue,
    entity_has_live_entry,
    load_admission_context,
    next_bottom_position,
)
from ..queue.audit import record_queue_event
from ..validation import NonEmptyStr, ZodModel
from ..zod_coerce import js_trim
from ..zod_types import zod_body

logger = get_logger()

router = APIRouter(prefix="/v1/admin/checkins", tags=["admin"])

READ_SCOPE = "deejaytools.testdata.read"
WRITE_SCOPE = "deejaytools.testdata.write"

# Every synthetic user's email matches this.
STUB_EMAIL_PATTERN = "admin-injected-%@test.local"


class InjectBody(ZodModel):
    """Body of ``POST /v1/admin/checkins``. Names are trimmed when stored."""

    NULLABLE: ClassVar[frozenset[str]] = frozenset({"notes"})

    sessionId: NonEmptyStr = Field(..., description="Session to inject into.")
    divisionName: NonEmptyStr = Field(..., description="Division.")
    leaderFirstName: NonEmptyStr = Field(..., description="Stub leader first name.")
    leaderLastName: NonEmptyStr = Field(..., description="Stub leader last name.")
    followerFirstName: NonEmptyStr = Field(..., description="Stub follower first name.")
    followerLastName: NonEmptyStr = Field(..., description="Stub follower last name.")
    notes: str | None = Field(None, description="Notes; blank is stored as null.")


class InjectedPair(BaseModel):
    """The synthetic pair."""

    id: str = Field(..., description="Pair id.")
    partner_b_id: str = Field(..., description="Stub follower partner id.")
    display_name: str = Field(..., description="'Leader Name & Follower Name'.")


class InjectedData(BaseModel):
    """The injected check-in."""

    id: str = Field(..., description="Check-in id.")
    sessionId: str = Field(..., description="Session id.")
    divisionName: str = Field(..., description="Division.")
    initialQueue: Literal["priority", "non_priority"] = Field(
        ..., description="Queue it was admitted to."
    )
    pair: InjectedPair = Field(..., description="The synthetic pair.")


class InjectedResponse(BaseModel):
    """Injection created."""

    data: InjectedData = Field(..., description="The injection.")
    meta: Meta = Field(..., description="Response metadata.")


class TestInjectionData(BaseModel):
    """One synthetic pair, with its check-in and queue state when it has one."""

    pair_id: str = Field(..., description="Pair id.")
    created_at: int = Field(..., description="Pair created, epoch ms.")
    leader_name: str = Field(..., description="Stub leader's name.")
    follower_name: str | None = Field(None, description="Stub follower's name.")
    session_id: str | None = Field(None, description="Session of the check-in.")
    session_name: str | None = Field(None, description="Session name.")
    division_name: str | None = Field(None, description="Division.")
    queue_status: Literal["active", "priority", "non_priority", "off_queue"] = Field(
        ..., description="Queue it is in, or off_queue."
    )
    position: int | None = Field(None, description="Queue position when queued.")


class TestInjectionListResponse(BaseModel):
    """Synthetic injections, newest pair first."""

    data: list[TestInjectionData] = Field(..., description="The injections.")
    meta: ListMeta = Field(..., description="Response metadata.")


class DeletedCountResponse(BaseModel):
    """How many synthetic users were removed."""

    data: dict[str, int] = Field(..., description="{'deleted': n}.")
    meta: Meta = Field(..., description="Response metadata.")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _conflict(message: str) -> Exception:
    return api_error(409, "conflict", message)


AUTH: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid token."},
    403: {"model": ErrorResponse, "description": "Lacks the scope."},
}


@router.post(
    "",
    status_code=201,
    response_model=InjectedResponse,
    summary="Inject a test check-in",
    description=(
        "Requires deejaytools.testdata.write. Creates a stub leader user, a "
        "follower partner, their pair and a placeholder song, then checks the "
        "pair in by the normal admission rules, ignoring the check-in window. "
        "No dedup: every call creates fresh rows."
    ),
    responses={
        **AUTH,
        400: {"model": ErrorResponse, "description": "Admission refused."},
        404: {"model": ErrorResponse, "description": "No such session."},
        409: {"model": ErrorResponse, "description": "Conflict."},
    },
)
async def inject_checkin(
    caller: Caller = Depends(require_scope(WRITE_SCOPE)),
    body: InjectBody = Depends(zod_body(InjectBody)),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Inject a synthetic check-in."""
    admin_id = caller.user_id
    now = _now_ms()
    leader_first = js_trim(body.leaderFirstName)
    leader_last = js_trim(body.leaderLastName)
    follower_first = js_trim(body.followerFirstName)
    follower_last = js_trim(body.followerLastName)

    session = (
        await db.execute(select(Session.id).where(Session.id == body.sessionId))
    ).first()
    if session is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Session not found")

    stub_user_id = str(uuid.uuid4())
    await db.execute(
        insert(User).values(
            id=stub_user_id,
            email=f"admin-injected-{stub_user_id}@test.local",
            display_name=f"{leader_first} {leader_last}",
            first_name=leader_first,
            last_name=leader_last,
            role="user",
            created_at=now,
            updated_at=now,
        )
    )
    song_id = str(uuid.uuid4())
    await db.execute(
        insert(Song).values(
            id=song_id,
            user_id=stub_user_id,
            partner_id=None,
            display_name="[Test Placeholder]",
            original_filename=None,
            processed_filename=None,
            drive_file_id=None,
            drive_folder_id=None,
            division=None,
            routine_name=None,
            personal_descriptor=None,
            season_year=None,
            created_at=now,
            updated_at=now,
        )
    )
    stub_partner_id = str(uuid.uuid4())
    await db.execute(
        insert(Partner).values(
            id=stub_partner_id,
            user_id=stub_user_id,
            first_name=follower_first,
            last_name=follower_last,
            partner_role="follower",
            email=None,
            linked_user_id=None,
            created_at=now,
            updated_at=now,
        )
    )
    pair_id = str(uuid.uuid4())
    await db.execute(
        insert(Pair).values(
            id=pair_id,
            user_a_id=stub_user_id,
            partner_b_id=stub_partner_id,
            created_at=now,
        )
    )
    # Node writes the stub rows outside any transaction: they stay even when
    # admission refuses the check-in below.
    await db.commit()

    entity = EntityRef(pair_id=pair_id)
    try:
        ctx = await load_admission_context(db, body.sessionId, body.divisionName)
        initial_queue: InitialQueue = await determine_initial_queue(db, entity, ctx)
    except AdmissionError as exc:
        raise api_error(400, ErrorCode.BAD_REQUEST, str(exc)) from exc
    except DBAPIError as exc:
        # As deejaytools-api, which answers any error from the admission
        # lookup (a NUL byte in divisionName, say) with 400 and its message.
        await db.rollback()
        raise api_error(400, ErrorCode.BAD_REQUEST, driver_message(exc)) from exc

    if await entity_has_live_entry(db, entity, body.sessionId):
        raise _conflict("This entity already has a live queue entry in this session")

    checkin_id = str(uuid.uuid4())
    await db.commit()
    try:
        await db.execute(
            insert(Checkin).values(
                id=checkin_id,
                session_id=body.sessionId,
                division_name=body.divisionName,
                entity_pair_id=pair_id,
                entity_solo_user_id=None,
                song_id=song_id,
                submitted_by_user_id=admin_id,
                initial_queue=initial_queue,
                notes=js_trim(body.notes or "") or None,
                created_at=now,
            )
        )
        position = await next_bottom_position(db, body.sessionId, initial_queue)
        await db.execute(
            insert(QueueEntry).values(
                id=str(uuid.uuid4()),
                checkin_id=checkin_id,
                session_id=body.sessionId,
                entity_pair_id=pair_id,
                entity_solo_user_id=None,
                queue_type=initial_queue,
                position=position,
                entered_queue_at=now,
            )
        )
        await record_queue_event(
            db,
            session_id=body.sessionId,
            checkin_id=checkin_id,
            action="checked_in",
            from_queue=None,
            from_position=None,
            to_queue=initial_queue,
            to_position=position,
            actor_user_id=admin_id,
            reason="admin_test_injection",
            created_at=now,
        )
        await db.commit()
    except Exception as exc:  # noqa: BLE001 - any failure is a 409, as in Node
        await db.rollback()
        logger.error(
            with_log_prefix(
                LOG_FAILURE,
                f"admin_checkin_inject_failed session={body.sessionId} "
                f"division={body.divisionName} admin={admin_id} "
                f"stub_user={stub_user_id} stub_partner={stub_partner_id} "
                f"pair={pair_id}: {exc!r}",
            )
        )
        raise _conflict(
            "Check-in conflicted with concurrent activity; please retry"
        ) from exc

    return success(
        {
            "id": checkin_id,
            "sessionId": body.sessionId,
            "divisionName": body.divisionName,
            "initialQueue": initial_queue,
            "pair": {
                "id": pair_id,
                "partner_b_id": stub_partner_id,
                "display_name": (
                    f"{leader_first} {leader_last} & {follower_first} {follower_last}"
                ),
            },
        }
    )


def _join_names(*parts: str | None) -> str:
    # [first, last].filter(Boolean).join(" "), without the trim full_name adds.
    return " ".join(p for p in parts if p)


@router.get(
    "/test",
    response_model=TestInjectionListResponse,
    summary="List test injections",
    description=(
        "Requires deejaytools.testdata.read. Every synthetic pair "
        "(admin-injected-*@test.local leaders), newest first, with its check-ins "
        "and their queue state; off_queue when completed, withdrawn or never "
        "checked in."
    ),
    responses=AUTH,
)
async def list_test_injections(
    _caller: Caller = Depends(require_scope(READ_SCOPE)),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List synthetic injections."""
    rows = (
        await db.execute(
            select(
                Pair.id.label("pair_id"),
                Pair.created_at.label("pair_created_at"),
                User.first_name.label("leader_first"),
                User.last_name.label("leader_last"),
                Partner.first_name.label("follower_first"),
                Partner.last_name.label("follower_last"),
                Checkin.session_id,
                Session.name.label("session_name"),
                Checkin.division_name,
                QueueEntry.queue_type,
                QueueEntry.position,
            )
            .select_from(User)
            .join(Pair, Pair.user_a_id == User.id)
            .outerjoin(Partner, Partner.id == Pair.partner_b_id)
            .outerjoin(Checkin, Checkin.entity_pair_id == Pair.id)
            .outerjoin(Session, Session.id == Checkin.session_id)
            .outerjoin(QueueEntry, QueueEntry.checkin_id == Checkin.id)
            .where(User.email.like(STUB_EMAIL_PATTERN))
            .order_by(desc(Pair.created_at))
        )
    ).all()
    return success_list(
        [
            {
                "pair_id": r.pair_id,
                "created_at": r.pair_created_at,
                "leader_name": _join_names(r.leader_first, r.leader_last),
                "follower_name": (
                    _join_names(r.follower_first, r.follower_last)
                    if r.follower_first or r.follower_last
                    else None
                ),
                "session_id": r.session_id,
                "session_name": r.session_name,
                "division_name": r.division_name,
                "queue_status": r.queue_type or "off_queue",
                "position": r.position,
            }
            for r in rows
        ]
    )


@router.delete(
    "/test",
    response_model=DeletedCountResponse,
    summary="Delete all test injections",
    description=(
        "Requires deejaytools.testdata.write. Hard-deletes every row tied to a "
        "synthetic user, in foreign-key order: queue events, queue entries, "
        "runs, check-ins, pairs, partners, songs, users. deleted is the number "
        "of synthetic users removed."
    ),
    responses=AUTH,
)
async def delete_test_injections(
    _caller: Caller = Depends(require_scope(WRITE_SCOPE)),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Delete every synthetic injection."""
    stub_user_ids = list(
        (
            await db.execute(select(User.id).where(User.email.like(STUB_EMAIL_PATTERN)))
        ).scalars()
    )
    if not stub_user_ids:
        return success({"deleted": 0})

    stub_pair_ids = list(
        (
            await db.execute(select(Pair.id).where(Pair.user_a_id.in_(stub_user_ids)))
        ).scalars()
    )
    stub_checkin_ids: list[str] = []
    if stub_pair_ids:
        stub_checkin_ids = list(
            (
                await db.execute(
                    select(Checkin.id).where(Checkin.entity_pair_id.in_(stub_pair_ids))
                )
            ).scalars()
        )

    await db.commit()
    try:
        if stub_checkin_ids:
            await db.execute(
                delete(QueueEvent).where(QueueEvent.checkin_id.in_(stub_checkin_ids))
            )
            await db.execute(
                delete(QueueEntry).where(QueueEntry.checkin_id.in_(stub_checkin_ids))
            )
            await db.execute(delete(Run).where(Run.checkin_id.in_(stub_checkin_ids)))
            await db.execute(delete(Checkin).where(Checkin.id.in_(stub_checkin_ids)))
        if stub_pair_ids:
            await db.execute(delete(Pair).where(Pair.id.in_(stub_pair_ids)))
        await db.execute(delete(Partner).where(Partner.user_id.in_(stub_user_ids)))
        await db.execute(delete(Song).where(Song.user_id.in_(stub_user_ids)))
        await db.execute(delete(User).where(User.id.in_(stub_user_ids)))
        await db.commit()
    except Exception:
        # Node has no handler here: the failure is a 500.
        await db.rollback()
        raise

    return success({"deleted": len(stub_user_ids)})
