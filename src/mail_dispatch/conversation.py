"""The conversation of one send (SPEC §5.2), recipient by recipient.

`Progress` records what was reached — the stage, the replies of `RCPT TO`, whether the final
`CRLF.CRLF` write was issued — and outlives the conversation, because the envelope of an
`INTERNAL_ERROR` carries what was reached when the exception was raised [D42].

The stage becomes `data_end` when the final write is issued, not when a reply arrives, and
nothing awaits between the two, so a cancellation (§5.6) either comes before that write or
finds it issued [D22] [D47].
"""

from __future__ import annotations

import asyncio
import base64
import re
import ssl
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from .errors import ErrorCode
from .settings import Settings
from .smtp import (
    Capabilities,
    Connection,
    Reply,
    Resolver,
    Stage,
    UpstreamError,
    connect,
    ehlo,
    expect_greeting,
    secure,
)

Status = Literal["accepted", "rejected", "deferred"]

QUIT_LIMIT = 1.0
_CHUNK = 65536
_LINE_START_DOT = re.compile(rb"(?m)^\.")


@dataclass(frozen=True, slots=True)
class RecipientResult:
    address: str
    field: str
    status: Status
    reply: Reply

    def as_json(self) -> dict[str, object]:
        return {
            "address": self.address,
            "field": self.field,
            "status": self.status,
            "smtp": {"code": self.reply.code, "message": self.reply.text},
        }


@dataclass(slots=True)
class Progress:
    """What the conversation reached; read by the handler whatever way the send ends."""

    clock: Callable[[], float]
    stage: Stage | None = None
    started: float | None = None
    finished: float | None = None
    recipients: list[RecipientResult] = field(default_factory=list)
    final_write_issued: bool = False
    tls: bool = False
    authenticated: bool = False
    final_reply: Reply | None = None

    def duration_ms(self) -> int | None:
        if self.started is None:
            return None
        end = self.finished if self.finished is not None else self.clock()
        return int((end - self.started) * 1000)

    @property
    def recipients_reported(self) -> bool:
        """`recipients[]` belongs in an error envelope exactly at these stages [D38]."""
        return self.stage in ("rcpt_to", "data", "data_end")


def dot_stuff(data: bytes) -> bytes:
    """RFC 5321 §4.5.2: a line that starts with `.` gets a second one."""
    return _LINE_START_DOT.sub(b"..", data)


def _fail(code: ErrorCode, stage: Stage, reply: Reply) -> UpstreamError:
    return UpstreamError(code, stage, reply)


def _by_class(stage: Stage, reply: Reply, rejected: ErrorCode) -> UpstreamError:
    """4xx is transient, 5xx is `rejected`, anything else is out of course [D39]."""
    if reply.kind == 4:
        return _fail("UPSTREAM_TRANSIENT", stage, reply)
    if reply.kind == 5:
        return _fail(rejected, stage, reply)
    return _fail("UPSTREAM_ERROR", stage, reply)


class Conversation:
    def __init__(
        self,
        settings: Settings,
        context: ssl.SSLContext,
        resolver: Resolver,
        progress: Progress,
    ) -> None:
        self.settings = settings
        self.context = context
        self.resolver = resolver
        self.progress = progress
        self.connection: Connection | None = None

    def _enter(self, stage: Stage) -> None:
        self.progress.stage = stage
        if self.connection is not None:
            self.connection.stage = stage

    async def run(
        self, message: bytes, size: int, sender: str, recipients: Sequence[tuple[str, str]]
    ) -> None:
        """Converse until the final reply; raise UpstreamError when the send cannot go on.

        The connection is closed on every way out. After a failure `QUIT` is sent on a
        best-effort basis within one second — never after a timeout or a broken connection —
        and a cancellation (the HTTP client went away) closes the socket at once.
        """
        settings = self.settings
        progress = self.progress
        loop = asyncio.get_running_loop()
        progress.started = progress.clock()
        self._enter("connect")
        try:
            implicit = self.context if settings.smtp_tls == "implicit" else None
            self.connection = await connect(
                host=settings.smtp_host,
                port=settings.smtp_port,
                implicit_tls=implicit,
                deadline=loop.time() + settings.smtp_timeout_seconds,
                limit=lambda: settings.smtp_timeout_seconds,
                resolver=self.resolver,
            )
            progress.tls = implicit is not None
            await self._converse(message, size, sender, recipients)
        except asyncio.CancelledError:
            if self.connection is not None:
                self.connection.abort()
            raise
        except UpstreamError:
            if self.connection is not None:
                await self.connection.quit(QUIT_LIMIT)
                await self.connection.close()
            raise
        except BaseException:
            # An exception in the service: closed without the final write if it was not yet
            # issued, and without anything more if it was.
            if self.connection is not None:
                self.connection.abort()
            raise
        else:
            assert self.connection is not None
            # The message is sent; neither QUIT nor the close can change that (§5.1).
            await self.connection.quit(QUIT_LIMIT)
            await self.connection.close()
        finally:
            progress.finished = progress.clock()

    async def _converse(
        self, message: bytes, size: int, sender: str, recipients: Sequence[tuple[str, str]]
    ) -> None:
        connection = self.connection
        assert connection is not None
        settings = self.settings
        progress = self.progress

        self._enter("greeting")
        expect_greeting(await connection.read_reply())
        self._enter("ehlo")
        capabilities = await ehlo(connection, settings.smtp_ehlo_name)
        if settings.smtp_tls in ("starttls", "starttls-opportunistic"):
            self._enter("starttls")
        capabilities, under_tls = await secure(
            connection, capabilities, settings=settings, context=self.context
        )
        progress.stage = connection.stage
        progress.tls = under_tls

        if settings.credentials_configured:
            self._enter("auth")
            if not under_tls:
                raise UpstreamError(
                    "UPSTREAM_TLS", "auth", None, "credentials never travel without TLS"
                )
            await self._authenticate(connection, capabilities)
            progress.authenticated = True

        self._enter("mail_from")
        size_parameter = f" SIZE={size}" if capabilities.size_announced else ""
        reply = await connection.command(f"MAIL FROM:<{sender}>{size_parameter}")
        if reply.kind != 2:
            raise _by_class("mail_from", reply, "UPSTREAM_REJECTED")

        self._enter("rcpt_to")
        for address, field_name in recipients:
            reply = await connection.command(f"RCPT TO:<{address}>")
            status: Status
            if reply.kind == 2:
                status = "accepted"
            elif reply.kind == 4:
                status = "deferred"
            elif reply.kind == 5:
                status = "rejected"
            else:
                # A 3xx, a 1xx or a code outside every class ends the whole send [D39].
                raise _fail("UPSTREAM_ERROR", "rcpt_to", reply)
            progress.recipients.append(RecipientResult(address, field_name, status, reply))
        if not any(r.status == "accepted" for r in progress.recipients):
            # DATA is not sent [D18]; the first reply of the deciding class is reported.
            deferred = [r.reply for r in progress.recipients if r.status == "deferred"]
            if deferred:
                raise _fail("UPSTREAM_TRANSIENT", "rcpt_to", deferred[0])
            raise _fail("UPSTREAM_REJECTED", "rcpt_to", progress.recipients[0].reply)

        self._enter("data")
        reply = await connection.command("DATA")
        if reply.code != 354:
            # After a 2xx to DATA the content is not sent: it would be read as commands.
            raise _by_class("data", reply, "UPSTREAM_REJECTED")
        stuffed = dot_stuff(message)
        # The message ends in CRLF; that CRLF goes out with the terminating dot.
        body = memoryview(stuffed)[:-2]
        for start in range(0, len(body), _CHUNK):
            await connection.write(bytes(body[start : start + _CHUNK]))
        await self._final_write(connection)

        reply = await connection.read_reply()
        if reply.kind != 2:
            raise _by_class("data_end", reply, "UPSTREAM_REJECTED")
        progress.final_reply = reply

    async def _final_write(self, connection: Connection) -> None:
        # No await between the stage and the write: a cancellation lands before both or after.
        self._enter("data_end")
        self.progress.final_write_issued = True
        await connection.write(b"\r\n.\r\n")

    async def _authenticate(self, connection: Connection, capabilities: Capabilities) -> None:
        """`PLAIN` when announced, otherwise `LOGIN`; one attempt, no switching [D27]."""
        password = self.settings.password
        username = self.settings.smtp_username
        assert password is not None and username is not None
        secret = password.get_secret_value()
        if "PLAIN" in capabilities.auth_methods:
            token = _b64("\0" + username + "\0" + secret)
            reply = await connection.command(f"AUTH PLAIN {token}")
            if reply.kind != 2:
                raise _by_class("auth", reply, "UPSTREAM_AUTH")
            return
        if "LOGIN" not in capabilities.auth_methods:
            raise UpstreamError(
                "UPSTREAM_AUTH",
                "auth",
                None,
                "the server announced none of the supported mechanisms (PLAIN, LOGIN)",
            )
        reply = await connection.command("AUTH LOGIN")
        for answer in (username, secret):
            if reply.code != 334:
                raise _by_class("auth", reply, "UPSTREAM_AUTH")
            reply = await connection.command(_b64(answer))
        if reply.kind != 2:
            raise _by_class("auth", reply, "UPSTREAM_AUTH")


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")
