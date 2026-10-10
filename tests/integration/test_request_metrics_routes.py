"""The per-route metric allow list names routes the app actually serves.

A template that matches no route publishes nothing, silently, and its
dashboard panel draws an empty line.
"""

from __future__ import annotations

from api_deejaytools.main import METRICS_ROUTES, app


def test_every_metrics_route_is_a_served_route_template() -> None:
    served = set(app.openapi()["paths"])
    assert set(METRICS_ROUTES) <= served, set(METRICS_ROUTES) - served
