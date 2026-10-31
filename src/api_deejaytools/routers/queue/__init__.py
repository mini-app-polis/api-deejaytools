"""``/v1/queue`` (docs/API.md).

Floor-manager actions on a session's queues, and the queue reads.

- promote (``promote``), complete and incomplete (``outcomes``), move-down
  and withdraw (``placement``): behind ``deejaytools.queue.manage`` on the
  event of the body's queue entry (event-scoped, ADR-0012). Each is one
  transaction that starts by locking the session row (ADR-0006,
  "Concurrency — pessimistic"), except move-down, which runs without the
  lock. A failed transaction answers 409 ``conflict``; there is no
  server-side retry.
- active and waiting (``reads``): public (API-008), without check-in notes,
  which are for the event's staff only. active/staff, priority and
  non-priority: behind ``deejaytools.queue.read`` on the path session's
  event, with notes. All are cached for 3 s per session and invalidated by
  every mutation of that session.

``router`` holds every route, in that order. It takes the modules' routes
themselves rather than including their routers, so ``router.routes`` lists
each route (``zod_types.document_zod_bodies`` reads them there).
"""

from __future__ import annotations

from fastapi import APIRouter

from . import outcomes, placement, promote, reads

router = APIRouter()
for _module in (promote, outcomes, placement, reads):
    router.routes.extend(_module.router.routes)
