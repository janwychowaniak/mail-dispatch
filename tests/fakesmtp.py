"""A scripted SMTP server inside the test process (SPEC §10.1).

It runs on its own thread and event loop, records every connection, command, envelope and raw
message, and can answer any code at any stage, a multi-line greeting included; announce or not
announce STARTTLS, AUTH and SIZE; speak TLS after STARTTLS or from the connection on; stay
silent; or close the connection.

A reply is scripted per verb in `responses`: a string (one reply, `\\r\\n` between the lines of
a multi-line one), SILENT, CLOSE, a Delayed reply, or a callable that returns one of those.
The verb `END` is the reply after the final dot, `LOGIN_USER` and `LOGIN_PASSWORD` the replies
inside `AUTH LOGIN`.
"""

from __future__ import annotations

import asyncio
import base64
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from certs import Certificates


class _Marker:
    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return self.name


SILENT = _Marker("SILENT")
CLOSE = _Marker("CLOSE")


@dataclass(frozen=True)
class Delayed:
    seconds: float
    reply: str


@dataclass(frozen=True)
class ThenClose:
    """Send the reply, then break the connection."""

    reply: str


@dataclass
class Session:
    commands: list[str] = field(default_factory=list)
    mail_from: str | None = None
    rcpt_to: list[str] = field(default_factory=list)
    data: bytes | None = None
    wire: bytes = b""
    final_dot: bool = False
    tls: bool = False
    auth: list[tuple[str, ...]] = field(default_factory=list)
    closed_by_client: bool = False
    finished: bool = False


class FakeSMTP:
    def __init__(self, certificates: Certificates | None = None, *, implicit: bool = False) -> None:
        self.certificates = certificates
        self.implicit = implicit
        self.greeting = "220 fake.example.org ESMTP"
        self.capabilities: list[str] = ["8BITMIME", "PIPELINING"]
        self.tls_capabilities: list[str] | None = None
        self.starttls = False
        self.auth: list[str] = []
        self.auth_before_tls = False
        self.credentials: tuple[str, str] = ("user", "password")
        self.responses: dict[str, Any] = {}
        self.silent_greeting = False
        self.close_before_greeting = False
        self.break_in_data = False
        self.use_other_certificate = False
        self.sessions: list[Session] = []
        self.messages: list[bytes] = []
        self.connections = 0
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._server: asyncio.Server | None = None
        self.port = 0

    # Lifecycle ---------------------------------------------------------------------------

    def start(self) -> FakeSMTP:
        self._thread.start()
        future = asyncio.run_coroutine_threadsafe(self._start(), self._loop)
        future.result(timeout=5)
        return self

    async def _start(self) -> None:
        context = self._server_context() if self.implicit else None
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, ssl=context)
        self.port = self._server.sockets[0].getsockname()[1]

    def stop(self) -> None:
        if not self._thread.is_alive():
            return

        async def _stop() -> None:
            if self._server is not None:
                self._server.close()
                self._server.close_clients()
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        asyncio.run_coroutine_threadsafe(_stop(), self._loop).result(timeout=5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def wait_until(self, predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    @property
    def last(self) -> Session:
        return self.sessions[-1]

    def _server_context(self) -> ssl.SSLContext:
        assert self.certificates is not None
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        if self.use_other_certificate:
            context.load_cert_chain(self.certificates.other_cert, self.certificates.other_key)
        else:
            context.load_cert_chain(self.certificates.cert, self.certificates.key)
        return context

    # The conversation ----------------------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        session = Session(tls=self.implicit)
        self.sessions.append(session)
        try:
            await self._converse(session, reader, writer)
        except (ConnectionError, ssl.SSLError, OSError, asyncio.IncompleteReadError):
            session.closed_by_client = True
        finally:
            session.finished = True
            writer.close()

    async def _silence(self, session: Session, reader: asyncio.StreamReader) -> None:
        while await reader.read(65536):
            pass
        session.closed_by_client = True

    async def _reply(
        self,
        session: Session,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        verb: str,
        line: str,
        default: str,
    ) -> str | None:
        """Send the scripted or default reply; None when the conversation is over."""
        scripted = self.responses.get(verb, default)
        if callable(scripted):
            scripted = scripted(session, line)
        if isinstance(scripted, Delayed):
            await asyncio.sleep(scripted.seconds)
            scripted = scripted.reply
        if scripted is SILENT:
            await self._silence(session, reader)
            return None
        if scripted is CLOSE:
            writer.close()
            return None
        if isinstance(scripted, ThenClose):
            writer.write(scripted.reply.encode("utf-8") + b"\r\n")
            await writer.drain()
            writer.transport.abort()
            return None
        assert isinstance(scripted, str)
        writer.write(scripted.encode("utf-8") + b"\r\n")
        await writer.drain()
        return scripted

    def _ehlo_lines(self, session: Session) -> list[str]:
        if session.tls and self.tls_capabilities is not None:
            keywords = list(self.tls_capabilities)
        else:
            keywords = list(self.capabilities)
        if self.starttls and not session.tls:
            keywords.append("STARTTLS")
        if self.auth and (session.tls or self.auth_before_tls):
            keywords.append("AUTH " + " ".join(self.auth))
        lines = ["fake.example.org", *keywords]
        return [f"250-{line}" for line in lines[:-1]] + [f"250 {lines[-1]}"]

    async def _converse(
        self, session: Session, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self.close_before_greeting:
            return
        if self.silent_greeting:
            await self._silence(session, reader)
            return
        writer.write(self.greeting.encode() + b"\r\n")
        await writer.drain()
        while True:
            raw = await reader.readline()
            if not raw:
                session.closed_by_client = True
                return
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            session.commands.append(line)
            verb = line.split(" ", 1)[0].upper()
            if verb == "EHLO":
                default = "\r\n".join(self._ehlo_lines(session))
                if await self._reply(session, reader, writer, "EHLO", line, default) is None:
                    return
            elif verb == "STARTTLS":
                sent = await self._reply(
                    session, reader, writer, "STARTTLS", line, "220 2.0.0 Go ahead"
                )
                if sent is None:
                    return
                if sent.startswith("2"):
                    await writer.start_tls(self._server_context())
                    session.tls = True
            elif verb == "AUTH":
                if not await self._auth(session, reader, writer, line):
                    return
            elif verb == "MAIL":
                session.mail_from = line
                if await self._reply(session, reader, writer, "MAIL", line, "250 2.1.0 OK") is None:
                    return
            elif verb == "RCPT":
                session.rcpt_to.append(line[len("RCPT TO:") :])
                if await self._reply(session, reader, writer, "RCPT", line, "250 2.1.5 OK") is None:
                    return
            elif verb == "DATA":
                sent = await self._reply(session, reader, writer, "DATA", line, "354 Go ahead")
                if sent is None:
                    return
                if sent.startswith("354") and not await self._data(session, reader, writer):
                    return
            elif verb == "QUIT":
                await self._reply(session, reader, writer, "QUIT", line, "221 2.0.0 Bye")
                return
            else:
                if (
                    await self._reply(session, reader, writer, verb, line, "502 5.5.2 Unknown")
                    is None
                ):
                    return

    async def _data(
        self, session: Session, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> bool:
        lines: list[bytes] = []
        while True:
            raw = await reader.readline()
            if not raw:
                session.closed_by_client = True
                return False
            if self.break_in_data:
                writer.transport.abort()
                return False
            session.wire += raw
            if raw == b".\r\n":
                session.final_dot = True
                break
            # Undo the client's dot-stuffing.
            lines.append(raw[1:] if raw.startswith(b"..") else raw)
        session.data = b"".join(lines)
        scripted = self.responses.get("END")
        if isinstance(scripted, ThenClose) and scripted.reply.startswith("2"):
            self.messages.append(session.data)
        sent = await self._reply(session, reader, writer, "END", "", "250 2.0.0 Queued")
        if sent is not None and sent.startswith("2"):
            self.messages.append(session.data)
        return sent is not None

    async def _auth(
        self,
        session: Session,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        line: str,
    ) -> bool:
        words = line.split()
        mechanism = words[1].upper() if len(words) > 1 else ""
        if mechanism == "PLAIN" and len(words) == 3:
            _, user, password = base64.b64decode(words[2]).decode("utf-8").split("\0")
            session.auth.append(("PLAIN", user, password))
            good = (user, password) == self.credentials
            default = "235 2.7.0 Accepted" if good else "535 5.7.8 Bad credentials"
            return await self._reply(session, reader, writer, "AUTH", line, default) is not None
        if mechanism == "LOGIN":
            if await self._reply(session, reader, writer, "AUTH", line, "334 VXNlcm5hbWU6") is None:
                return False
            user = base64.b64decode((await reader.readline()).strip()).decode("utf-8")
            session.auth.append(("LOGIN", user))
            if (
                sent := await self._reply(
                    session, reader, writer, "LOGIN_USER", "", "334 UGFzc3dvcmQ6"
                )
            ) is None:
                return False
            if not sent.startswith("334"):
                return True
            password = base64.b64decode((await reader.readline()).strip()).decode("utf-8")
            session.auth[-1] = ("LOGIN", user, password)
            good = (user, password) == self.credentials
            default = "235 2.7.0 Accepted" if good else "535 5.7.8 Bad credentials"
            return (
                await self._reply(session, reader, writer, "LOGIN_PASSWORD", "", default)
                is not None
            )
        return (
            await self._reply(session, reader, writer, "AUTH", line, "504 5.5.4 Unrecognized")
            is not None
        )
