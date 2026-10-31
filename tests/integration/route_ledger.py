"""The route ledger (TEST-018): every route the app registers is called by
some integration test, or is listed here with the reason it cannot be.

Every request the suite makes is recorded by its route template, whichever
client made it. When a run that covers the whole integration suite ends,
the run fails on a route nothing called, on an exemption for a route a test
calls, and on an exemption for a route the app does not register, so an
exemption cannot outlive its reason.
A run of part of the suite (a file, ``-k``, a deselection) checks nothing.
"""

from __future__ import annotations

from typing import Any

from fastapi.routing import APIRoute

#: Routes no integration test can call, each with the reason.
EXEMPT: dict[tuple[str, str], str] = {}

CALLED: set[tuple[str, str]] = set()

_original_handle = APIRoute.handle


async def _recording_handle(
    self: APIRoute, scope: Any, receive: Any, send: Any
) -> None:
    CALLED.add((scope["method"], self.path))
    await _original_handle(self, scope, receive, send)


def install() -> None:
    """Record every request any test client makes, by route template."""
    APIRoute.handle = _recording_handle  # type: ignore[method-assign]


def registered(app: Any) -> set[tuple[str, str]]:
    """``(method, path template)`` for every route the app registers."""
    return {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }


def problems(app: Any) -> list[str]:
    """What the ledger refuses, one line each; empty when it is satisfied."""
    routes = registered(app)
    out = [
        f"not called by any test: {method} {path}"
        for method, path in sorted(routes - CALLED - EXEMPT.keys())
    ]
    out += [
        f"exempt but called by a test; remove its exemption: {method} {path}"
        for method, path in sorted(EXEMPT.keys() & CALLED)
    ]
    out += [
        f"exempt but not a registered route; remove its exemption: {method} {path}"
        for method, path in sorted(EXEMPT.keys() - routes)
    ]
    return out
