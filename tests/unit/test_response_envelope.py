"""Every success is the fleet's shared envelope, with the shared ``Meta``."""

from __future__ import annotations

from typing import Any

from mini_app_polis.api.contract import Meta

from api_deejaytools.errors import success, success_list

# Liveness and deploy probes, answered with a bare body (routers/meta.py).
UNWRAPPED = {"/health", "/version"}


def test_success_meta_is_the_shared_meta_with_one_of_one() -> None:
    body = success({"id": "x"})
    assert body["data"] == {"id": "x"}
    assert Meta.model_validate(body["meta"]) == Meta(count=1, total=1, version="v1")


def test_success_list_meta_counts_the_items() -> None:
    assert Meta.model_validate(success_list([1, 2, 3])["meta"]) == Meta(
        count=3, total=3, version="v1"
    )
    assert success_list([])["meta"] == {"version": "v1", "count": 0, "total": 0}


def _json_successes(doc: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    out = []
    for path, operations in doc["paths"].items():
        for method, op in operations.items():
            for status, response in op["responses"].items():
                content = response.get("content", {}).get("application/json")
                if status.startswith("2") and content:
                    out.append((f"{method.upper()} {path} {status}", path, content))
    return out


def test_every_success_response_model_is_an_envelope() -> None:
    from api_deejaytools.main import app

    app.openapi_schema = None
    doc = app.openapi()
    schemas = doc["components"]["schemas"]
    assert schemas["Meta"]["required"] == ["count", "total", "version"]
    successes = _json_successes(doc)
    assert successes
    for name, path, content in successes:
        if path in UNWRAPPED:
            continue
        ref = content["schema"]["$ref"].rsplit("/", 1)[-1]
        meta = schemas[ref]["properties"]["meta"]
        assert meta["$ref"] == "#/components/schemas/Meta", name
        assert "meta" in schemas[ref]["required"], name
