"""More zod rules, for request models (alongside ``validation``).

- ``trimmed(...)``: zod's ``z.string().trim()`` followed by checks, which run
  on the trimmed value and leave the trimmed value in the body. Use it as
  ``Annotated[str, trimmed(min_length=1, max_length=100)]``.
- ``Division``: ``z.enum(DIVISIONS)``.
- ``zod_query``: a query string validated as zod validates one through
  ``@hono/zod-validator``, where a repeated key arrives as an array.
- ``zod_body``: a JSON body validated as ``zValidator("json", ...)`` does.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, FastAPI, Request
from fastapi.dependencies.models import Dependant
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from pydantic import AfterValidator, BeforeValidator, ValidationError

from .domain import DIVISIONS
from .validation import ZodModel
from .zod_coerce import js_trim


def trimmed(
    *,
    min_length: int | None = None,
    max_length: int | None = None,
    pattern: str | None = None,
    pattern_message: str = "Invalid string",
) -> AfterValidator:
    """Trim, then check: ``z.string().trim().regex().min().max()``."""
    # JavaScript semantics for the common divergences: $ only at the very
    # end (not before a trailing newline) and \d only for ASCII digits.
    compiled = (
        re.compile(pattern.replace("$", r"\Z").replace(r"\d", "[0-9]"))
        if pattern
        else None
    )

    def _check(value: str) -> str:
        value = js_trim(value)
        if compiled is not None and not compiled.search(value):
            raise ValueError(pattern_message)
        if min_length is not None and len(value) < min_length:
            raise ValueError(
                f"Too small: expected string to have >={min_length} characters"
            )
        if max_length is not None and len(value) > max_length:
            raise ValueError(
                f"Too big: expected string to have <={max_length} characters"
            )
        return value

    return AfterValidator(_check)


def _division(value: str) -> str:
    if value not in DIVISIONS:
        raise ValueError(f"Invalid option: expected one of {', '.join(DIVISIONS)}")
    return value


Division = Annotated[str, AfterValidator(_division)]
"""zod's ``z.enum(DIVISIONS)``."""


def _string_only(value: Any) -> Any:
    # Strict mode already refuses non-strings; this keeps the message plain
    # when a repeated query key arrives as a list.
    if isinstance(value, list):
        raise ValueError("Invalid input: expected string, received array")
    return value


QueryStr = Annotated[str, BeforeValidator(_string_only)]
"""A query-string value zod declared ``z.string()``: one value, not repeated."""


M = TypeVar("M", bound=ZodModel)


def parse_zod_query(request: Request, model: type[M]) -> M:
    """Validate the query string against ``model``; a repeated key arrives
    as a list, as ``@hono/zod-validator`` hands zod an array."""
    raw: dict[str, Any] = {}
    for key in request.query_params.keys():
        values = request.query_params.getlist(key)
        raw[key] = values[0] if len(values) == 1 else values
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise RequestValidationError(
            [
                {**err, "loc": ("query", *err.get("loc", ()))}
                for err in exc.errors(include_url=False)
            ]
        ) from exc


def zod_query(model: type[M]) -> Callable[[Request], M]:
    """A dependency validating the query string against ``model``.

    Declare it after the auth dependency so a missing token wins over a bad
    query, as Hono's middleware order has it. A route with no auth
    dependency calls ``parse_zod_query`` in its handler instead: a request
    parser declared as a dependency reads, to a static audit of route
    guards (AUTH-003), as a guard it cannot resolve.
    """

    def _dependency(request: Request) -> M:
        return parse_zod_query(request, model)

    return _dependency


# hono's validator: a JSON body only under a JSON content type.
_JSON_CONTENT_TYPE = re.compile(
    r"application/([a-z-.]+\+)?json(;\s*[a-zA-Z0-9\-]+=([^;]+))*", re.IGNORECASE
)


class MalformedJsonError(Exception):
    """A JSON body that does not parse.

    Not an ``ApiError``: hono throws an HTTPException that deejaytools-api's
    onError does not recognise, so it answers 500 INTERNAL (with this
    message outside production), and so does this service.
    """


def _no_constants(name: str) -> Any:
    # JSON.parse has no NaN or Infinity literals; Python's json does.
    raise ValueError(f"Unexpected token {name}")


async def parse_zod_body(request: Request, model: type[M]) -> M:
    """Parse and validate the JSON body against ``model``, as hono's
    ``zValidator("json", ...)`` does: without a JSON content type the body
    is ``{}`` (so an all-optional PATCH with no body changes nothing), and
    malformed JSON is a 500 (``MalformedJsonError``)."""
    value: Any = {}
    content_type = request.headers.get("content-type")
    if content_type and _JSON_CONTENT_TYPE.fullmatch(content_type):
        text = (await request.body()).decode("utf-8", errors="replace")
        try:
            value = json.loads(
                text.removeprefix("\ufeff"), parse_constant=_no_constants
            )
        except ValueError as exc:
            raise MalformedJsonError("Malformed JSON in request body") from exc
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        raise RequestValidationError(
            [
                {**err, "loc": ("body", *err.get("loc", ()))}
                for err in exc.errors(include_url=False)
            ]
        ) from exc


def zod_body(model: type[M]) -> Callable[[Request], Any]:
    """A dependency parsing and validating the JSON body against ``model``
    (``parse_zod_body``).

    Declare it after the auth dependency, which then runs first, as hono's
    middleware order has it: FastAPI parses a plain body parameter before
    any dependency. A route with no auth dependency calls ``parse_zod_body``
    in its handler and is marked with ``documents_zod_body`` instead (see
    ``zod_query`` for why).
    """

    async def _dependency(request: Request) -> M:
        return await parse_zod_body(request, model)

    _dependency.__zod_body__ = model  # type: ignore[attr-defined]
    return _dependency


E = TypeVar("E", bound=Callable[..., Any])


def documents_zod_body(model: type[ZodModel]) -> Callable[[E], E]:
    """Mark a handler that calls ``parse_zod_body(request, model)`` itself,
    so ``document_zod_bodies`` documents its body. Apply it below the route
    decorator."""

    def mark(endpoint: E) -> E:
        endpoint.__zod_body__ = model  # type: ignore[attr-defined]
        return endpoint

    return mark


def document_zod_bodies(app: FastAPI, routers: Iterable[APIRouter]) -> None:
    """Put the request bodies ``zod_body`` validates on ``routers``' routes
    into the OpenAPI schema.

    FastAPI documents only bodies it parses itself; these are parsed by a
    dependency, so they are added after the schema is generated.
    """

    def _models(dependant: Dependant) -> list[type[ZodModel]]:
        found = []
        for dep in dependant.dependencies:
            model = getattr(dep.call, "__zod_body__", None)
            if model is not None:
                found.append(model)
            found.extend(_models(dep))
        return found

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        for route in (r for router in routers for r in router.routes):
            if not isinstance(route, APIRoute) or not route.include_in_schema:
                continue
            models = _models(route.dependant)
            marked = getattr(route.endpoint, "__zod_body__", None)
            if marked is not None:
                models.append(marked)
            for model in models:
                model_schema = model.model_json_schema(
                    ref_template="#/components/schemas/{model}"
                )
                components.update(model_schema.pop("$defs", {}))
                components[model.__name__] = model_schema
                for method in route.methods or ():
                    operation = schema["paths"][route.path_format].get(method.lower())
                    if operation is not None:
                        operation["requestBody"] = {
                            "required": any(
                                f.is_required() for f in model.model_fields.values()
                            ),
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": f"#/components/schemas/{model.__name__}"
                                    }
                                }
                            },
                        }
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
