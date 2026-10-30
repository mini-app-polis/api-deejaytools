"""The OpenAPI document says what the API really does."""

from __future__ import annotations

from typing import Any

import pytest

from api_deejaytools.main import app


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    app.openapi_schema = None
    return app.openapi()


def _operations(schema: dict[str, Any]) -> list[dict[str, Any]]:
    return [op for path in schema["paths"].values() for op in path.values()]


def test_a_refused_request_is_advertised_as_400_not_422(schema: dict[str, Any]) -> None:
    assert not [
        op["operationId"] for op in _operations(schema) if "422" in op["responses"]
    ]
    assert "HTTPValidationError" not in schema["components"]["schemas"]
    sync = schema["paths"]["/v1/auth/sync"]["post"]
    assert sync["responses"]["400"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorEnvelope"
    }


@pytest.mark.parametrize(
    ("path", "names"),
    [
        ("/v1/sessions", {"event_id"}),
        ("/v1/runs", {"session_id", "event_id", "limit"}),
        ("/v1/events/{id}/people", {"q"}),
    ],
)
def test_query_parameters_are_documented(
    schema: dict[str, Any], path: str, names: set[str]
) -> None:
    params = schema["paths"][path]["get"].get("parameters", [])
    assert {p["name"] for p in params if p["in"] == "query"} >= names
