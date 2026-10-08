"""Shared fixtures. The suite is offline and writes nothing outside its temporary directory
(SPEC §10.1, case 20).

Two guards run around every test. The network guard lets a socket connect only to the
loopback address and records every connection; name resolution is substituted, so a lookup
that reaches the system resolver for anything but a numeric address fails the test. The write
guard records every file opened for writing outside the temporary directory.
"""

from __future__ import annotations

import builtins
import io
import os
import socket
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from certs import SERVER_NAME, Certificates, generate
from fakesmtp import FakeSMTP
from fastapi.testclient import TestClient

from mail_dispatch.app import create_app
from mail_dispatch.settings import Settings
from mail_dispatch.smtp import Address

SETTINGS_VARIABLES = [name.upper() for name in Settings.model_fields]
LOOPBACK = {"127.0.0.1", "::1"}


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own environment never leaks into a test's configuration."""
    for name in list(os.environ):
        if name.upper() in SETTINGS_VARIABLES:
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def connections(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """Every outgoing connection of the test; anything but loopback fails it (case 20)."""
    made: list[tuple[str, int]] = []
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def connect(self: socket.socket, address: Any) -> None:
        host, port = address[0], address[1]
        if host not in LOOPBACK:
            raise AssertionError(f"a test attempted a connection to {host}")
        made.append((host, port))
        real_connect(self, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            socket.inet_pton(socket.AF_INET6 if ":" in str(host) else socket.AF_INET, str(host))
        except (OSError, TypeError):
            if host is not None:
                raise AssertionError(f"a test reached the system resolver for {host}") from None
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return made


@pytest.fixture(autouse=True)
def _no_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[None]:
    """Nothing is written outside the harness's temporary directory (case 20) [D29]."""
    allowed = [Path(tempfile.gettempdir()).resolve(), tmp_path_factory.getbasetemp().resolve()]
    violations: list[str] = []
    real_open = builtins.open
    real_os_open = os.open

    def outside(path: Any) -> bool:
        if isinstance(path, int):
            return False
        resolved = Path(os.fsdecode(path)).resolve()
        return not any(resolved.is_relative_to(base) for base in allowed)

    def guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if any(flag in mode for flag in "wax+") and outside(file):
            violations.append(os.fsdecode(file))
        return real_open(file, mode, *args, **kwargs)

    def guarded_os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        writing = flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC)
        if writing and outside(path):
            violations.append(os.fsdecode(path))
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    yield
    assert violations == [], f"files written outside the temporary directory: {violations}"


@pytest.fixture(scope="session")
def certificates(tmp_path_factory: pytest.TempPathFactory) -> Certificates:
    return generate(tmp_path_factory.mktemp("certs"))


@pytest.fixture
def settings() -> Settings:
    return Settings(smtp_host="mail.example.org", smtp_ehlo_name="client.example.org")


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def fake(certificates: Certificates) -> Iterator[FakeSMTP]:
    server = FakeSMTP(certificates).start()
    yield server
    server.stop()


@pytest.fixture
def implicit_fake(certificates: Certificates) -> Iterator[FakeSMTP]:
    server = FakeSMTP(certificates, implicit=True).start()
    yield server
    server.stop()


def substituted_resolver(port_by_name: dict[str, int]) -> Callable[[str, int], Any]:
    """Resolves only the names it is given, to loopback; anything else does not resolve."""

    async def resolve(host: str, port: int) -> Sequence[Address]:
        if host == "127.0.0.1":
            return [(socket.AF_INET, ("127.0.0.1", port))]
        if host not in port_by_name:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [(socket.AF_INET, ("127.0.0.1", port))]

    return resolve


ServiceFactory = Callable[..., Any]


@pytest.fixture
def service(fake: FakeSMTP) -> Iterator[ServiceFactory]:
    """`with service(**settings) as client:` — the app talking to the fake, by its name."""

    @contextmanager
    def make(server: FakeSMTP = fake, **overrides: Any) -> Iterator[TestClient]:
        values: dict[str, Any] = {
            "smtp_host": SERVER_NAME,
            "smtp_port": server.port,
            "smtp_tls": "none",
            "smtp_ehlo_name": "client.example.org",
            "smtp_timeout_seconds": 2,
            "health_probe_timeout_seconds": 2,
        }
        values.update(overrides)
        app = create_app(
            Settings(**values), resolver=substituted_resolver({SERVER_NAME: server.port})
        )
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client

    yield make
