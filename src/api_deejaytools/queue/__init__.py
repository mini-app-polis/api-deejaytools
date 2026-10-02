"""The floor-trial queue model (deejaytools-api src/lib/queue/*, ADR-005).

- ``run_counts``: completed runs per entity and division, for admission.
- ``single_entry``: the one-live-entry-per-session rule.
- ``admission``: which waiting queue a check-in enters, and the promotion gates.
- ``compaction``: closing gaps and appending at the bottom of a queue.
- ``fill``: the session lock every queue transaction starts with, and auto-fill.

Every mutating transaction starts with ``lock_session_for_fill`` (``SELECT …
FOR UPDATE`` on the session row), which serializes queue changes per session.
"""

from __future__ import annotations

from .admission import (
    AdmissionContext,
    AdmissionError,
    InitialQueue,
    PromotionGate,
    can_promote_non_priority,
    can_promote_priority,
    determine_initial_queue,
    load_admission_context,
)
from .compaction import QueueType, compact_after_removal, next_bottom_position
from .fill import LockedSession, fill_active_queue, lock_session_for_fill
from .run_counts import EntityRef, runs_for_entity_in_event, runs_for_entity_in_session
from .single_entry import entity_has_live_entry

__all__ = [
    "AdmissionContext",
    "AdmissionError",
    "EntityRef",
    "InitialQueue",
    "LockedSession",
    "PromotionGate",
    "QueueType",
    "can_promote_non_priority",
    "can_promote_priority",
    "compact_after_removal",
    "determine_initial_queue",
    "entity_has_live_entry",
    "fill_active_queue",
    "load_admission_context",
    "lock_session_for_fill",
    "next_bottom_position",
    "runs_for_entity_in_event",
    "runs_for_entity_in_session",
]
