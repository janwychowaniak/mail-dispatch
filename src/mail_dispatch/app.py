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
from .errors import ApiError, error_response, new_dispatch_id
from .intake import check_content_type, parse_json, read_body
from .jsonlog import log_event
from .settings import Settings

Clock = Callable[[], float]


def create_app(settings: Settings, *, clock: Clock = time.monotonic) -> FastAPI:
    app = FastAPI(
        title="mail-dispatch",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        # `/v1/send/` is NOT_FOUND, not a redirect (§3).
        redirect_slashes=False,
    )
    app.state.settings = settings
    app.state.clock = clock
    app.state.started = clock()

    app.add_exception_handler(StarletteHTTPException, _routing_error)
    app.add_middleware(EnvelopeGuard)
    app.add_api_route("/v1/send", send, methods=["POST"])
    app.add_api_route("/v1/health", health, methods=["GET"])
    return app


async def send(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    dispatch_id = new_dispatch_id()
    try:
        check_content_type(request)
        body = await read_body(request, settings.max_request_bytes)
        parse_json(body)
        raise NotImplementedError("validation, composition and the conversation")
    except ApiError as error:
        _log_request(request, dispatch_id, error.status, error.code)
        return error_response(dispatch_id, error)
    except Exception as exc:
        _log_request(request, dispatch_id, 500, "INTERNAL_ERROR", exception=exc)
        return error_response(dispatch_id, _internal_error())


async def health(request: Request) -> Response:
    raise NotImplementedError("the health probe")


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
