"""`GET /v1/health`: the service is alive, and how the server is (SPEC §6) [D25].

The probe — connect, `EHLO`, and in the STARTTLS modes `STARTTLS` and `EHLO` again; never
`AUTH` or `MAIL FROM` — runs lazily, at most once per `HEALTH_CACHE_TTL_SECONDS`, under one
deadline for all of it [D37]. Concurrent requests that find the cache stale wait for one shared
probe. The measurement ends with the reply to the last `EHLO`; `QUIT` is sent afterwards in
whatever time the deadline leaves and changes neither the result nor its age.
"""

from __future__ import annotations

import asyncio
import dataclasses
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from . import __version__
from .settings import Settings
from .smtp import (
    Capabilities,
    Connection,
    Resolver,
    UpstreamError,
    connect,
    ehlo,
    expect_greeting,
    secure,
)

ProbeStatus = Literal["ok", "down", "timeout", "tls_failed"]


@dataclass(frozen=True, slots=True)
class ProbeResult:
    status: ProbeStatus
    measured_at: float
    capabilities: Capabilities | None = None
    error: dict[str, object] | None = None


def _status(error: UpstreamError) -> ProbeStatus:
    if error.code == "UPSTREAM_TIMEOUT":
        return "timeout"
    if error.code == "UPSTREAM_TLS":
        return "tls_failed"
    return "down"


class Health:
    def __init__(
        self,
        settings: Settings,
        context: ssl.SSLContext,
        resolver: Resolver,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.context = context
        self.resolver = resolver
        self.clock = clock
        self.started = clock()
        self._cached: ProbeResult | None = None
        self._probing: asyncio.Task[ProbeResult] | None = None

    def fresh(self) -> ProbeResult | None:
        """The last measurement, when it is no older than the TTL."""
        cached = self._cached
        if (
            cached is None
            or self.clock() - cached.measured_at > self.settings.health_cache_ttl_seconds
        ):
            return None
        return cached

    def fresh_size_limit(self) -> int | None:
        """The server's `SIZE` from a fresh measurement, for step 6 of §4.7 [D19]."""
        fresh = self.fresh()
        if fresh is None or fresh.capabilities is None:
            return None
        return fresh.capabilities.size_max_bytes

    async def current(self) -> ProbeResult:
        fresh = self.fresh()
        if fresh is not None:
            return fresh
        if self._probing is None or self._probing.done():
            self._probing = asyncio.create_task(self._probe())
        return await asyncio.shield(self._probing)

    async def _probe(self) -> ProbeResult:
        settings = self.settings
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settings.health_probe_timeout_seconds

        def remaining() -> float:
            return deadline - loop.time()

        connection: Connection | None = None
        try:
            connection = await connect(
                host=settings.smtp_host,
                port=settings.smtp_port,
                implicit_tls=self.context if settings.smtp_tls == "implicit" else None,
                deadline=deadline,
                limit=remaining,
                resolver=self.resolver,
            )
            connection.stage = "greeting"
            expect_greeting(await connection.read_reply())
            first = await ehlo(connection, settings.smtp_ehlo_name)
            last, _ = await secure(connection, first, settings=settings, context=self.context)
            # STARTTLS from the first EHLO, everything else from the last one (§6).
            capabilities = dataclasses.replace(last, starttls=first.starttls)
            result = ProbeResult("ok", self.clock(), capabilities=capabilities)
        except UpstreamError as error:
            result = ProbeResult(_status(error), self.clock(), error=error.upstream)
        except BaseException:
            if connection is not None:
                connection.abort()
            raise
        if connection is not None:
            await connection.quit(remaining())
            await connection.close(remaining())
        self._cached = result
        return result

    async def body(self) -> dict[str, object]:
        result = await self.current()
        settings = self.settings
        now = self.clock()
        upstream: dict[str, object] = {
            "host": settings.smtp_host,
            "port": settings.smtp_port,
            "tls_mode": settings.smtp_tls,
            "auth_configured": settings.credentials_configured,
            "status": result.status,
            "checked_age_seconds": int(max(now - result.measured_at, 0)),
        }
        if result.capabilities is not None:
            capabilities = result.capabilities
            upstream["capabilities"] = {
                "size_max_bytes": capabilities.size_max_bytes,
                "starttls": capabilities.starttls,
                "auth_methods": list(capabilities.auth_methods),
                "eightbitmime": capabilities.eightbitmime,
                "smtputf8": capabilities.smtputf8,
                "pipelining": capabilities.pipelining,
            }
        if result.error is not None:
            upstream["error"] = result.error
        return {
            "ok": result.status == "ok",
            "version": __version__,
            "uptime_seconds": int(now - self.started),
            "upstream": upstream,
        }
