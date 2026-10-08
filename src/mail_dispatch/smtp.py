"""The service's own SMTP client, on asyncio [D44].

`smtplib` is not used: it encodes `AUTH` as ASCII while the password is UTF-8, switches
mechanisms after a refusal, resolves names without a time limit, and falls back to `HELO`.

This module holds what a send and the health probe share: reaching the server under one
deadline, reading replies, `EHLO` and its capabilities, `STARTTLS`. Every socket operation runs
under a time limit that the caller supplies, so a send can use an idle limit per operation
[D21] and the probe one deadline for all of it [D37].
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import socket
import ssl
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from .errors import ErrorCode
from .settings import Settings

Stage = Literal[
    "connect", "greeting", "ehlo", "starttls", "auth", "mail_from", "rcpt_to", "data", "data_end"
]
Address = tuple[int, tuple[Any, ...]]
Resolver = Callable[[str, int], Awaitable[Sequence[Address]]]
Limit = Callable[[], float]

_READ_SIZE = 65536
_MAX_REPLY_LINE = 65536
_REPLY_LINE = re.compile(rb"([0-9]{3})([ -]?)(.*)", re.DOTALL)
_ENHANCED = re.compile(r"([245])\.([0-9]{1,3})\.([0-9]{1,3})(?![0-9.])")


async def system_resolver(host: str, port: int) -> Sequence[Address]:
    """Name resolution, in the order the resolver returns the addresses (§5.2)."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [(family, sockaddr) for family, _, _, _, sockaddr in infos]


def tls_context(settings: Settings) -> ssl.SSLContext:
    """TLS 1.2 or newer; verification against the system's authorities plus `SMTP_CA_FILE`."""
    if settings.smtp_tls_verify:
        context = ssl.create_default_context()
        if settings.ca_pem is not None:
            context.load_verify_locations(cadata=settings.ca_pem)
    else:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


@dataclass(frozen=True, slots=True)
class Reply:
    code: int
    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        """The lines joined with `\\n`, without their codes (§5.2)."""
        return "\n".join(self.lines)

    @property
    def kind(self) -> int:
        return self.code // 100

    @property
    def enhanced(self) -> str | None:
        """The enhanced status code (`5.1.1`), for the log, which never carries the text."""
        found = _ENHANCED.match(self.lines[0]) if self.lines else None
        return ".".join(found.groups()) if found else None


class UpstreamError(Exception):
    """A conversation that cannot go on: the code of §5.5, the stage, and the server's reply
    or, when the server said nothing usable, a fixed description [D43]."""

    def __init__(
        self, code: ErrorCode, stage: Stage, reply: Reply | None, description: str = ""
    ) -> None:
        super().__init__(code, stage)
        self.code: ErrorCode = code
        self.stage: Stage = stage
        self.reply = reply
        self.description = description

    @property
    def upstream(self) -> dict[str, object]:
        if self.reply is not None:
            return {"stage": self.stage, "code": self.reply.code, "message": self.reply.text}
        return {"stage": self.stage, "code": None, "message": self.description}


@dataclass(frozen=True, slots=True)
class Capabilities:
    starttls: bool = False
    size_announced: bool = False
    size_max_bytes: int | None = None
    auth_methods: tuple[str, ...] = ()
    eightbitmime: bool = False
    smtputf8: bool = False
    pipelining: bool = False


def parse_ehlo(reply: Reply) -> Capabilities:
    """The keywords of an `EHLO` reply; `AUTH=` (the legacy form) is read too (§6)."""
    starttls = size_announced = eightbitmime = smtputf8 = pipelining = False
    size: int | None = None
    methods: list[str] = []
    for line in reply.lines[1:]:
        words = line.split()
        if not words:
            continue
        keyword = words[0].upper()
        if keyword == "STARTTLS":
            starttls = True
        elif keyword == "SIZE":
            size_announced = True
            if len(words) > 1 and words[1].isdigit() and int(words[1]) > 0:
                size = int(words[1])
        elif keyword == "AUTH" or keyword.startswith("AUTH="):
            mechanisms = [*([keyword[5:]] if keyword.startswith("AUTH=") else []), *words[1:]]
            for mechanism in mechanisms:
                if mechanism and mechanism.upper() not in methods:
                    methods.append(mechanism.upper())
        elif keyword == "8BITMIME":
            eightbitmime = True
        elif keyword == "SMTPUTF8":
            smtputf8 = True
        elif keyword == "PIPELINING":
            pipelining = True
    return Capabilities(
        starttls, size_announced, size, tuple(methods), eightbitmime, smtputf8, pipelining
    )


class Connection:
    """One SMTP connection: replies, commands and writes, each under `limit()` seconds.

    Failures become UpstreamError at the current `stage`: a timeout is UPSTREAM_TIMEOUT, a
    closed or broken connection and a reply that does not parse are UPSTREAM_ERROR.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        limit: Limit,
        stage: Stage = "greeting",
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._limit = limit
        self._buffer = bytearray()
        self.stage: Stage = stage
        self.broken = False

    def _timeout(self) -> UpstreamError:
        self.broken = True
        return UpstreamError(
            "UPSTREAM_TIMEOUT", self.stage, None, "the server did not answer in time"
        )

    def _closed(self) -> UpstreamError:
        self.broken = True
        return UpstreamError("UPSTREAM_ERROR", self.stage, None, "the server closed the connection")

    def _violation(self, description: str) -> UpstreamError:
        self.broken = True
        return UpstreamError("UPSTREAM_ERROR", self.stage, None, description)

    async def _read_line(self) -> bytes:
        while True:
            end = self._buffer.find(b"\n")
            if end > _MAX_REPLY_LINE:
                raise self._violation("the server sent a reply line that is too long")
            if end >= 0:
                line = bytes(self._buffer[: end + 1])
                del self._buffer[: end + 1]
                return line
            if len(self._buffer) > _MAX_REPLY_LINE:
                raise self._violation("the server sent a reply line that is too long")
            try:
                async with asyncio.timeout(self._limit()):
                    data = await self._reader.read(_READ_SIZE)
            except TimeoutError:
                raise self._timeout() from None
            except OSError:
                raise self._closed() from None
            if not data:
                raise self._closed()
            self._buffer += data

    async def read_reply(self) -> Reply:
        """One reply, multi-line included; its code is taken from the last line (§5.2)."""
        lines: list[str] = []
        while True:
            raw = (await self._read_line()).rstrip(b"\r\n")
            parsed = _REPLY_LINE.fullmatch(raw)
            if parsed is None:
                raise self._violation("the server sent a reply that is not SMTP")
            code, separator, text = parsed.groups()
            lines.append(text.decode("utf-8", errors="replace"))
            if separator != b"-":
                return Reply(int(code), tuple(lines))

    async def write(self, data: bytes) -> None:
        try:
            self._writer.write(data)
            async with asyncio.timeout(self._limit()):
                await self._writer.drain()
        except TimeoutError:
            raise self._timeout() from None
        except OSError:
            raise self._closed() from None

    async def command(self, line: str) -> Reply:
        await self.write(line.encode("ascii") + b"\r\n")
        return await self.read_reply()

    async def start_tls(self, context: ssl.SSLContext, server_hostname: str) -> None:
        if self._buffer:
            # Anything sent before the handshake would be read as if it came under TLS.
            raise self._violation("the server sent data before the TLS handshake")
        try:
            limit = self._limit()
            async with asyncio.timeout(limit):
                await self._writer.start_tls(
                    context, server_hostname=server_hostname, ssl_handshake_timeout=limit
                )
        except TimeoutError:
            raise self._timeout() from None
        except ssl.SSLCertVerificationError:
            self.broken = True
            raise UpstreamError(
                "UPSTREAM_TLS", self.stage, None, "the server's certificate failed verification"
            ) from None
        except (ssl.SSLError, OSError):
            self.broken = True
            raise UpstreamError(
                "UPSTREAM_TLS", self.stage, None, "the TLS handshake failed"
            ) from None

    async def quit(self, limit: float) -> None:
        """`QUIT` on a best-effort basis: its reply, and any failure, are ignored."""
        if self.broken:
            return
        with contextlib.suppress(Exception):
            async with asyncio.timeout(limit):
                self._writer.write(b"QUIT\r\n")
                await self._writer.drain()
                await self.read_reply()

    async def close(self, limit: float = 1.0) -> None:
        """Close, waiting at most `limit` for the transport; past it, abort."""
        self._writer.close()
        try:
            async with asyncio.timeout(max(limit, 0.0)):
                await self._writer.wait_closed()
        except Exception:
            self.abort()

    def abort(self) -> None:
        """Close at once, without `QUIT`: the HTTP client went away (§5.6)."""
        transport = self._writer.transport
        transport.abort()


async def connect(
    *,
    host: str,
    port: int,
    implicit_tls: ssl.SSLContext | None,
    deadline: float,
    limit: Limit,
    resolver: Resolver,
) -> Connection:
    """Resolve and connect under one deadline, trying addresses in the resolver's order.

    All attempts failing within the deadline is UPSTREAM_UNREACHABLE; the deadline passing
    before an attempt was settled is UPSTREAM_TIMEOUT [D21]. In `implicit` mode the handshake
    follows under `limit`, and its stage is still `connect`.
    """
    loop = asyncio.get_running_loop()

    def remaining() -> float:
        return deadline - loop.time()

    timeout = UpstreamError(
        "UPSTREAM_TIMEOUT", "connect", None, "the server did not answer in time"
    )
    try:
        async with asyncio.timeout_at(deadline):
            addresses = await resolver(host, port)
    except TimeoutError:
        raise timeout from None
    except OSError:
        raise UpstreamError(
            "UPSTREAM_UNREACHABLE", "connect", None, "the server's name could not be resolved"
        ) from None
    if not addresses:
        raise UpstreamError(
            "UPSTREAM_UNREACHABLE", "connect", None, "the server's name could not be resolved"
        )

    connected: socket.socket | None = None
    for family, sockaddr in addresses:
        if remaining() <= 0:
            raise timeout
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setblocking(False)
        try:
            async with asyncio.timeout_at(deadline):
                await loop.sock_connect(sock, sockaddr)
        except TimeoutError:
            sock.close()
            raise timeout from None
        except OSError:
            sock.close()
            continue
        except BaseException:
            sock.close()
            raise
        connected = sock
        break
    if connected is None:
        raise UpstreamError(
            "UPSTREAM_UNREACHABLE", "connect", None, "the connection could not be established"
        )

    if implicit_tls is None:
        reader, writer = await asyncio.open_connection(sock=connected)
        return Connection(reader, writer, limit)
    handshake = limit()
    try:
        async with asyncio.timeout(handshake):
            reader, writer = await asyncio.open_connection(
                sock=connected,
                ssl=implicit_tls,
                server_hostname=host,
                ssl_handshake_timeout=handshake,
            )
    except TimeoutError:
        connected.close()
        raise timeout from None
    except ssl.SSLCertVerificationError:
        connected.close()
        raise UpstreamError(
            "UPSTREAM_TLS", "connect", None, "the server's certificate failed verification"
        ) from None
    except (ssl.SSLError, OSError):
        connected.close()
        raise UpstreamError("UPSTREAM_TLS", "connect", None, "the TLS handshake failed") from None
    except BaseException:
        connected.close()
        raise
    return Connection(reader, writer, limit)


def expect_greeting(reply: Reply) -> None:
    """2xx goes on; 4xx is transient; 5xx and anything else are UPSTREAM_ERROR (§5.5)."""
    if reply.kind == 2:
        return
    code: ErrorCode = "UPSTREAM_TRANSIENT" if reply.kind == 4 else "UPSTREAM_ERROR"
    raise UpstreamError(code, "greeting", reply)


async def ehlo(connection: Connection, name: str) -> Capabilities:
    """`EHLO`, with no `HELO` fallback [D20]."""
    connection.stage = "ehlo"
    reply = await connection.command(f"EHLO {name}")
    if reply.kind == 2:
        return parse_ehlo(reply)
    code: ErrorCode = "UPSTREAM_TRANSIENT" if reply.kind == 4 else "UPSTREAM_ERROR"
    raise UpstreamError(code, "ehlo", reply)


async def secure(
    connection: Connection,
    capabilities: Capabilities,
    *,
    settings: Settings,
    context: ssl.SSLContext,
) -> tuple[Capabilities, bool]:
    """The TLS rules of §7.2 for the STARTTLS modes: the capabilities after them, and whether
    the session is now under TLS. A failed attempt never falls back to plain text [D26]."""
    mode = settings.smtp_tls
    if mode == "implicit":
        return capabilities, True
    if mode == "none":
        return capabilities, False
    connection.stage = "starttls"
    if not capabilities.starttls:
        if mode == "starttls":
            raise UpstreamError(
                "UPSTREAM_TLS", "starttls", None, "the server did not announce STARTTLS"
            )
        if settings.credentials_configured:
            raise UpstreamError(
                "UPSTREAM_TLS",
                "starttls",
                None,
                "the server did not announce STARTTLS, and credentials never travel without TLS",
            )
        return capabilities, False
    reply = await connection.command("STARTTLS")
    if reply.kind in (4, 5):
        raise UpstreamError("UPSTREAM_TLS", "starttls", reply)
    if reply.kind != 2:
        raise UpstreamError("UPSTREAM_ERROR", "starttls", reply)
    await connection.start_tls(context, settings.smtp_host)
    return await ehlo(connection, settings.smtp_ehlo_name), True
