"""`POST /v1/send`: validate, compose, converse, report (SPEC §4, §5).

Everything up to step 6 of §4.7 happens before a connection is opened. What was reached —
the composed message, the conversation and its stage — is kept until the response is written,
so that an exception at any point still yields an envelope that says as much as is known
[D42], and an exception after the final write reads as an unknown outcome [D22].

When the HTTP client goes away, the issuing of the final `CRLF.CRLF` write decides [D47]:
before it the conversation is aborted and nothing is handed over; after it the service waits
for the server's reply and logs it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .compose import Composed, compose
from .conversation import Conversation, Progress
from .errors import ApiError, error_response, new_dispatch_id
from .health import Health
from .intake import check_content_type, parse_json, read_body
from .jsonlog import log_event
from .settings import Settings
from .smtp import Resolver, UpstreamError
from .validation import MessageRequest, validate

_UPSTREAM_MESSAGES = {
    "UPSTREAM_UNREACHABLE": "the SMTP server could not be reached",
    "UPSTREAM_TIMEOUT": "the SMTP server did not answer in time",
    "UPSTREAM_TLS": "TLS with the SMTP server could not be used as configured",
    "UPSTREAM_AUTH": "authentication with the SMTP server failed",
    "UPSTREAM_REJECTED": "the SMTP server refused the message",
    "UPSTREAM_TRANSIENT": "the SMTP server refused the message for now",
    "UPSTREAM_ERROR": "the SMTP conversation failed",
}
_INTERNAL_UPSTREAM_MESSAGE = "the service failed during the conversation"


@dataclass(frozen=True, slots=True)
class Prepared:
    message: MessageRequest
    composed: Composed


@dataclass(slots=True)
class Reached:
    """What one send has reached so far, for whichever way it ends."""

    progress: Progress
    message: MessageRequest | None = None
    composed: Composed | None = None
    log: dict[str, object] = field(default_factory=dict)


def prepare(body: bytes, settings: Settings, server_size: int | None) -> Prepared:
    """Steps 2-6 of §4.7: parse, validate, compose, and check the composed size [D19]."""
    message = validate(parse_json(body), max_recipients=settings.max_recipients)
    composed = compose(message, message_id_domain=settings.message_id_domain)
    size = len(composed.data)
    limit, source = settings.max_message_bytes, "max_message_bytes"
    if server_size is not None and server_size < limit:
        limit, source = server_size, "server_size"
    if size > limit:
        raise ApiError(
            "MESSAGE_TOO_LARGE",
            "the composed message exceeds the limit",
            limit_bytes=limit,
            actual_bytes=size,
            limit_source=source,
        )
    return Prepared(message, composed)


async def _disconnected(receive: Callable[[], Awaitable[Any]]) -> None:
    """Returns when the HTTP client has gone away; the body has been read by then."""
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return


class SendHandler:
    def __init__(
        self,
        settings: Settings,
        context: ssl.SSLContext,
        resolver: Resolver,
        health: Health,
        clock: Callable[[], float],
    ) -> None:
        self.settings = settings
        self.context = context
        self.resolver = resolver
        self.health = health
        self.clock = clock

    async def __call__(self, request: Request) -> Response:
        dispatch_id = new_dispatch_id()
        reached = Reached(Progress(self.clock))
        watcher: asyncio.Task[None] | None = None
        try:
            check_content_type(request)
            body = await read_body(request, self.settings.max_request_bytes)
            watcher = asyncio.create_task(_disconnected(request.receive))
            prepared = await asyncio.to_thread(
                prepare, body, self.settings, self.health.fresh_size_limit()
            )
            reached.message, reached.composed = prepared.message, prepared.composed
            return await self._send(dispatch_id, reached, watcher)
        except ApiError as error:
            return self._error(dispatch_id, reached, error)
        except UpstreamError as error:
            return self._error(dispatch_id, reached, self._upstream_error(reached, error))
        except Exception as exc:
            return self._error(dispatch_id, reached, self._internal_error(reached), exc)
        finally:
            if watcher is not None:
                watcher.cancel()

    async def _send(
        self, dispatch_id: str, reached: Reached, watcher: asyncio.Task[None]
    ) -> Response:
        message, composed = reached.message, reached.composed
        assert message is not None and composed is not None
        if watcher.done():
            # Gone before the connection is opened: none is opened (§5.6).
            return self._aborted(dispatch_id, reached)
        conversation = Conversation(self.settings, self.context, self.resolver, reached.progress)
        talking = asyncio.create_task(
            conversation.run(
                composed.data,
                len(composed.data),
                message.sender.address,
                [(r.mailbox.address, r.field) for r in message.recipients],
            )
        )
        await asyncio.wait({talking, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if not talking.done() and not reached.progress.final_write_issued:
            talking.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await talking
            if talking.cancelled():
                return self._aborted(dispatch_id, reached)
        if watcher.done():
            # After the final write nothing can be taken back: the reply is awaited and logged
            # under the same dispatch_id (§5.6).
            reached.log["client"] = "gone"
        await talking
        return self._success(dispatch_id, reached)

    def _success(self, dispatch_id: str, reached: Reached) -> Response:
        message, composed, progress = reached.message, reached.composed, reached.progress
        assert message is not None and composed is not None
        counts = {
            status: sum(1 for r in progress.recipients if r.status == status)
            for status in ("accepted", "rejected", "deferred")
        }
        body = {
            "ok": True,
            "dispatch_id": dispatch_id,
            "message_id": composed.message_id,
            "upstream": {
                "host": self.settings.smtp_host,
                "port": self.settings.smtp_port,
                "tls": progress.tls,
                "authenticated": progress.authenticated,
            },
            "recipients": [r.as_json() for r in progress.recipients],
            **counts,
            "size_bytes": len(composed.data),
            "duration_ms": progress.duration_ms(),
        }
        response = JSONResponse(body)
        self._log(dispatch_id, reached, status=200, result="sent", **counts)
        return response

    def _reached_fields(self, reached: Reached) -> dict[str, object]:
        fields: dict[str, object] = {}
        if reached.composed is not None:
            fields["message_id"] = reached.composed.message_id
            fields["size_bytes"] = len(reached.composed.data)
        duration = reached.progress.duration_ms()
        if duration is not None:
            fields["duration_ms"] = duration
        if reached.progress.recipients_reported:
            fields["recipients"] = [r.as_json() for r in reached.progress.recipients]
        return fields

    def _upstream_error(self, reached: Reached, error: UpstreamError) -> ApiError:
        reached.log["reply"] = _reply_log(error)
        return ApiError(
            error.code,
            _UPSTREAM_MESSAGES[error.code],
            upstream=error.upstream,
            **self._reached_fields(reached),
        )

    def _internal_error(self, reached: Reached) -> ApiError:
        fields: dict[str, object] = {}
        if reached.progress.stage is not None:
            fields["upstream"] = {
                "stage": reached.progress.stage,
                "code": None,
                "message": _INTERNAL_UPSTREAM_MESSAGE,
            }
        fields.update(self._reached_fields(reached))
        return ApiError("INTERNAL_ERROR", "an unexpected error occurred in the service", **fields)

    def _error(
        self,
        dispatch_id: str,
        reached: Reached,
        error: ApiError,
        exception: BaseException | None = None,
    ) -> Response:
        extra: dict[str, object] = {}
        if "field" in error.fields:
            extra["field"] = error.fields["field"]
        if exception is not None:
            extra["exception"] = type(exception).__name__
        self._log(dispatch_id, reached, status=error.status, result=error.code, **extra)
        return error_response(dispatch_id, error)

    def _aborted(self, dispatch_id: str, reached: Reached) -> Response:
        """The client is gone, so this response reaches nobody and the log is the only trace.

        499 is the conventional "client closed request"; it is never seen by a consumer.
        """
        self._log(dispatch_id, reached, status=499, result="aborted_by_client")
        return Response(status_code=499)

    def _log(
        self, dispatch_id: str, reached: Reached, *, status: int, result: str, **extra: object
    ) -> None:
        """One event per request: identifiers, sizes, stage, codes, counters — never the text
        of a reply, the subject, the content or the full recipient list [D30]."""
        fields: dict[str, object] = {"dispatch_id": dispatch_id, "status": status, "result": result}
        message, progress = reached.message, reached.progress
        if message is not None:
            fields["sender"] = message.sender.address
            fields["recipients"] = len(message.recipients)
            fields["recipient_domains"] = sorted(
                {r.mailbox.domain.lower() for r in message.recipients}
            )
        if reached.composed is not None:
            fields["size_bytes"] = len(reached.composed.data)
        if progress.stage is not None:
            fields["stage"] = progress.stage
            fields["recipient_codes"] = [_code_log(r.reply) for r in progress.recipients]
        if progress.final_reply is not None:
            fields["reply"] = _code_log(progress.final_reply)
        fields.update(reached.log)
        fields.update(extra)
        duration = progress.duration_ms()
        if duration is not None:
            fields["duration_ms"] = duration
        level = logging.ERROR if status >= 500 and result == "INTERNAL_ERROR" else logging.INFO
        log_event("send", level=level, **fields)


def _code_log(reply: Any) -> dict[str, object]:
    entry: dict[str, object] = {"code": reply.code}
    if reply.enhanced is not None:
        entry["enhanced"] = reply.enhanced
    return entry


def _reply_log(error: UpstreamError) -> dict[str, object] | None:
    return None if error.reply is None else _code_log(error.reply)
