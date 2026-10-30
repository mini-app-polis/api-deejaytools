"""The response envelope and error helpers, on the web app's wire contract.

Shapes (docs/API.md, ADR-0010):

    success       {"data": <object>, "meta": {"version": "v1"}}
    success list  {"data": [...],    "meta": {"version": "v1", "count": n}}
    error         {"error": {"code": "...", "message": "..."}}

These are the web app's shapes rather than common-python-utils'
``Envelope`` / ``ErrorEnvelope`` (XSTACK-005): the shared ``Meta`` requires
``count`` and ``total`` on every response and ``ErrorDetail`` carries a
``details`` key, and the web app must see exactly what it sees today.
``meta.version`` is the literal ``"v1"`` that common-typescript-utils
writes, not the package version.

Error codes are the web app's, not the Python house style: upper case
for most, and the mixed-case ones docs/API.md lists. Normalizing them is a later
change made together with the web app.
"""

from __future__ import annotations

from typing import Any

import sentry_sdk
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mini_app_polis import activity
from mini_app_polis.api.contract.envelope import ErrorEnvelope
from mini_app_polis.environment import is_production
from mini_app_polis.logger import LOG_FAILURE, get_logger, with_log_prefix
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = get_logger()

API_VERSION = "v1"


class ErrorCode:
    """The error codes the web app expects, spelled as it spells them."""

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


#: The error envelope every failure answers with: the fleet's shared model
#: (XSTACK-002). This service never sends its optional ``details``.
ErrorResponse = ErrorEnvelope


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


def bad_request(message: str) -> ApiError:
    """400 ``BAD_REQUEST`` with ``message``."""
    return api_error(400, ErrorCode.BAD_REQUEST, message)


def not_found(what: str) -> ApiError:
    """404 ``NOT_FOUND``: ``<what> not found``."""
    return api_error(404, ErrorCode.NOT_FOUND, f"{what} not found")


def conflict(message: str) -> ApiError:
    """409 ``conflict`` with ``message``."""
    return api_error(409, "conflict", message)


def unauthorized() -> ApiError:
    """401 for a missing or invalid credential, in the contract's wording."""
    return api_error(401, ErrorCode.UNAUTHORIZED, "Authentication required")


def user_not_synced() -> ApiError:
    """401 for a valid credential from someone who has not called /v1/auth/sync."""
    return api_error(401, ErrorCode.USER_NOT_SYNCED, "Call POST /v1/auth/sync first")


def forbidden() -> ApiError:
    """403 for a missing scope. The message is the same for every scope."""
    return api_error(403, ErrorCode.FORBIDDEN, "Admin access required")


def _validation_message(exc: RequestValidationError) -> str:
    """Summarize field errors as ``path: message; ...``.

    The text differs from zod's (ADR-0010 allows it); the code, status and
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
    """Render every failure in the error envelope with the contract's codes."""

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        if exc.status_code >= 500:
            # The fault message names the code (CHUNK_ERROR, INTERNAL); the
            # message is the caller's, and may echo what they sent.
            activity.record_fault_detail(request, f"ApiError {exc.code}")
        return JSONResponse(
            status_code=exc.status_code, content=error_body(exc.code, exc.message)
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # 400, not FastAPI's 422: the contract is 400 VALIDATION_ERROR.
        return JSONResponse(
            status_code=400,
            content=error_body(ErrorCode.VALIDATION_ERROR, _validation_message(exc)),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # An unknown path and a known path with the wrong method are answered
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

    # Not an exception handler: Starlette serves the catch-all handler from
    # its outermost layer, outside CORS, so a 500 would go out without CORS
    # headers and reach the browser as a network error. This middleware is
    # added first, so it sits inside every other one (CORS included).
    app.add_middleware(UnhandledErrorMiddleware)


class UnhandledErrorMiddleware:
    """Answer an unhandled exception with 500 INTERNAL in the error envelope.

    Reported to Sentry and logged. A generic message in
    production, the exception's own message elsewhere. If the response had
    already started there is nothing to answer with, and it re-raises.

    The 500 it answers is what the Discord fault report outside it sees, so
    it records the exception's type and Sentry event id on the scope for
    that report — never the exception's text, which for a DBAPI error is
    the statement.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:
            if started:
                raise
            event_id = sentry_sdk.capture_exception(exc)
            activity.record_fault_detail(
                scope, activity.fault_detail(exc, event_id=event_id, capture=None)
            )
            logger.exception(
                with_log_prefix(
                    LOG_FAILURE,
                    f"unhandled {type(exc).__name__} on {scope['method']} {scope['path']}",
                )
            )
            message = "Internal server error" if is_production() else str(exc)
            response = JSONResponse(
                status_code=500, content=error_body(ErrorCode.INTERNAL, message)
            )
            await response(scope, receive, send)
