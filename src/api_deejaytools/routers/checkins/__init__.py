"""``/v1/checkins`` (docs/API.md).

A dancer checks an entity into a session's waiting queue (``create``),
lists their live check-ins (``mine``), and withdraws their own
(``withdraw``). Writes are behind ``deejaytools.checkins.write``, the list
behind ``deejaytools.checkins.read``; an admin, or the DJ or event manager
of the session's event, may check in for someone else with
``on_behalf_of_user_id``, which is a second decision
(``deejaytools.delegation.act`` on the session's event, ADR-0008 and
ADR-0012).

Check-in and withdraw are queue transactions: they lock the session row
first, change the queue, and auto-fill the active queue (ADR-0006).

``router`` holds every route, in that order. It takes the modules' routes
themselves rather than including their routers, so ``router.routes`` lists
each route (``zod_types.document_zod_bodies`` reads them there).
"""

from __future__ import annotations

from fastapi import APIRouter

from . import create, mine, withdraw

router = APIRouter()
for _module in (create, mine, withdraw):
    router.routes.extend(_module.router.routes)
