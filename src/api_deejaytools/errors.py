"""The response envelope and error helpers, on deejaytools-api's wire contract.

Shapes (deejaytools-api docs/API.md, ADR-009):

    success       {"data": <object>, "meta": {"version": "v1"}}
    success list  {"data": [...],    "meta": {"version": "v1", "count": n}}
    error         {"error": {"code": "...", "message": "..."}}

These are deejaytools-api's shapes rather than common-python-utils'
``Envelope`` / ``ErrorEnvelope`` (XSTACK-005): the shared ``Meta`` requires
``count`` and ``total`` on every response and ``ErrorDetail`` carries a
``details`` key, and the web app must see exactly what it sees today.
``meta.version`` is the literal ``"v1"`` that common-typescript-utils
writes, not the package version.

Error codes are deejaytools-api's, not the Python house style: upper case
for most, and the mixed-case ones API.md lists. Normalizing them is a later
change made together with the web app.
"""

from __future__ import annotations

from typing import Any

import sentry_sdk
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mini_app_polis.environment import is_production
from mini_app_polis.logger import LOG_FAILURE, get_logger, with_log_prefix
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = get_logger()

API_VERSION = "v1"


class ErrorCode:
    """The error codes deejaytools-api answers with, spelled as it spells them."""

    UNAUTHORIZED = "UNAUTHORIZED"
    USER_NOT_SYNCED = "USER_NOT_SYNCED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    BAD_REQUEST = "BAD_REQUEST"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    INTERNAL = "INTERNAL"
    EMAIL_BELONGS_TO_ANOTHER_ACCOUNT = "EMAIL_BELONGS_TO_ANOTHER_ACCOUNT"


class Meta(BaseModel):
    """Metadata on a single-object response."""

    version: str = Field(API_VERSION, description="API version, always 'v1'.")


class ListMeta(Meta):
    """Metadata on a list response."""

    count: int = Field(..., ge=0, description="Number of items in data.")


class ErrorBody(BaseModel):
    """The error object inside the error envelope."""

    code: str = Field(..., description="Machine-readable error code.")
    message: str = Field(..., description="Human-readable explanation.")


class ErrorResponse(BaseModel):
    """The error envelope every failure answers with."""

    error: ErrorBody = Field(..., description="What went wrong.")


def success(data: Any) -> dict[str, Any]:
    """Wrap one object in the success envelope."""
    return {"data": data, "meta": {"version": API_VERSION}}


def success_list(items: list[Any]) -> dict[str, Any]:
    """Wrap a list in the success envelope, with ``meta.count``."""
    return {"data": items, "meta": {"version": API_VERSION, "count": len(items)}}


def error_body(code: str, message: str) -> dict[str, Any]:
    """The error envelope as a plain dict."""
    return {"error": {"code": code, "message": message}}


class ApiError(Exception):
    """An error with a status, a code and a message, raised to answer a request."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(f"{status_code} {code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message


def api_error(status_code: int, code: str, message: str) -> ApiError:
    """Build an ``ApiError`` to raise."""
    return ApiError(status_code, code, message)


def unauthorized() -> ApiError:
    """401 for a missing or invalid credential, worded as deejaytools-api words it."""
    return api_error(401, ErrorCode.UNAUTHORIZED, "Authentication required")


def user_not_synced() -> ApiError:
    """401 for a valid credential from someone who has not called /v1/auth/sync."""
    return api_error(401, ErrorCode.USER_NOT_SYNCED, "Call POST /v1/auth/sync first")


def forbidden() -> ApiError:
    """403 for a missing scope. The message is deejaytools-api's, for every scope."""
    return api_error(403, ErrorCode.FORBIDDEN, "Admin access required")


def _validation_message(exc: RequestValidationError) -> str:
    """Summarize field errors as ``path: message; ...``, as the zod helper did.

    The text differs from zod's (ADR-009 allows it); the code, status and
    envelope are the contract.
    """
    parts = []
    for err in exc.errors():
        loc = [str(p) for p in err.get("loc", ())]
        if loc and loc[0] in {"body", "query", "path", "header"}:
            loc = loc[1:]
        parts.append(f"{'.'.join(loc)}: {err.get('msg', 'invalid')}")
    return "; ".join(parts) or "Invalid request"


_STATUS_CODES = {
    400: ErrorCode.BAD_REQUEST,
    401: ErrorCode.UNAUTHORIZED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
}


def install_error_handlers(app: FastAPI) -> None:
    """Render every failure in the error envelope with deejaytools-api's codes."""

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code, content=error_body(exc.code, exc.message)
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # 400, not FastAPI's 422: deejaytools-api answers 400 VALIDATION_ERROR.
        return JSONResponse(
            status_code=400,
            content=error_body(ErrorCode.VALIDATION_ERROR, _validation_message(exc)),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Hono answers an unknown path and a known path with the wrong method
        # alike: 404 NOT_FOUND "Not found". FastAPI would say 405 for the
        # second, so it is folded into the first.
        if exc.status_code in (404, 405):
            return JSONResponse(
                status_code=404, content=error_body(ErrorCode.NOT_FOUND, "Not found")
            )
        code = _STATUS_CODES.get(exc.status_code, ErrorCode.INTERNAL)
        return JSONResponse(
            status_code=exc.status_code, content=error_body(code, str(exc.detail))
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        sentry_sdk.capture_exception(exc)
        logger.exception(
            with_log_prefix(
                LOG_FAILURE,
                f"unhandled {type(exc).__name__} on {request.method} {request.url.path}",
            )
        )
        # As deejaytools-api: a generic message in production, the
        # exception's own message elsewhere.
        message = "Internal server error" if is_production() else str(exc)
        return JSONResponse(
            status_code=500, content=error_body(ErrorCode.INTERNAL, message)
        )
