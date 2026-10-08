"""The HTTP application: two endpoints, and every error in one envelope (SPEC §3).

No error leaves the envelope of §5.3: an unknown path, a method not allowed — `HEAD`
included — and an unexpected exception are JSON too, each with a fresh `dispatch_id` [D33].
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .dispatch import SendHandler
from .errors import ApiError, error_response, new_dispatch_id
from .health import Health
from .jsonlog import log_event
from .settings import Settings
from .smtp import Resolver, system_resolver, tls_context

Clock = Callable[[], float]


def create_app(
    settings: Settings,
    *,
    clock: Clock = time.monotonic,
    resolver: Resolver = system_resolver,
) -> FastAPI:
    """The application; tests substitute the clock and name resolution (§10.1)."""
    app = FastAPI(
        title="mail-dispatch",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        # `/v1/send/` is NOT_FOUND, not a redirect (§3).
        redirect_slashes=False,
    )
    context = tls_context(settings)
    health = Health(settings, context, resolver, clock)
    send = SendHandler(settings, context, resolver, health, time.monotonic)

    async def health_endpoint() -> Response:
        body = await health.body()
        log_event("health", status=200, upstream_status=body["upstream"]["status"])  # type: ignore[index]
        return JSONResponse(body)

    app.state.settings = settings
    app.state.health = health
    app.add_exception_handler(StarletteHTTPException, _routing_error)
    app.add_middleware(EnvelopeGuard)
    app.add_api_route("/v1/send", send, methods=["POST"])
    app.add_api_route("/v1/health", health_endpoint, methods=["GET"])
    return app


async def _routing_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)
    dispatch_id = new_dispatch_id()
    if exc.status_code == 404:
        error = ApiError("NOT_FOUND", "there is no such path")
    elif exc.status_code == 405:
        error = ApiError("METHOD_NOT_ALLOWED", "the method is not allowed on this path")
    else:
        error = _internal_error()
    _log_request(request, dispatch_id, error.status, error.code)
    return error_response(dispatch_id, error, headers=exc.headers)


def _internal_error() -> ApiError:
    return ApiError("INTERNAL_ERROR", "an unexpected error occurred in the service")


def _log_request(
    request: Request,
    dispatch_id: str,
    status: int,
    code: str,
    *,
    exception: BaseException | None = None,
) -> None:
    fields: dict[str, object] = {
        "dispatch_id": dispatch_id,
        "method": request.method,
        "path": request.url.path,
        "status": status,
        "code": code,
    }
    if exception is not None:
        fields["exception"] = type(exception).__name__
    log_event("request", level=logging.ERROR if status >= 500 else logging.INFO, **fields)


class EnvelopeGuard:
    """The last resort: an exception that escaped a handler still gets the envelope.

    Starlette's own server-error middleware would answer in plain text and re-raise, which
    puts a traceback — and whatever the exception quotes — into the log.
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
            dispatch_id = new_dispatch_id()
            log_event(
                "request",
                level=logging.ERROR,
                dispatch_id=dispatch_id,
                method=scope.get("method"),
                path=scope.get("path"),
                status=500,
                code="INTERNAL_ERROR",
                exception=type(exc).__name__,
            )
            if not started:
                response: JSONResponse = error_response(dispatch_id, _internal_error())
                await response(scope, receive, send)
