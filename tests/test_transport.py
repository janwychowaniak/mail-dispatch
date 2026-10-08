"""Acceptance cases 14-17 of SPEC §10.2: reaching the server, timeouts, TLS and AUTH."""

from __future__ import annotations

import logging
import socket
import time
from typing import Any

import pytest
from builders import request
from certs import Certificates
from conftest import ServiceFactory
from fakesmtp import CLOSE, SILENT, FakeSMTP, ThenClose
from mailparse import parse, text_of


def send(service: ServiceFactory, **settings: Any) -> tuple[int, dict[str, Any]]:
    with service(**settings) as client:
        response = client.post("/v1/send", json=request())
    return response.status_code, response.json()


def upstream_error(service: ServiceFactory, code: str, **settings: Any) -> dict[str, Any]:
    status, body = send(service, **settings)
    assert body["ok"] is False, body
    assert body["error"]["code"] == code, body
    assert status in (502, 503, 504)
    error: dict[str, Any] = body["error"]
    # Every UPSTREAM_* envelope carries the composed message's identity and size [D42].
    assert {"message_id", "size_bytes", "duration_ms"} <= set(error)
    return error


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


# Case 14 -----------------------------------------------------------------------------------


def test_case_14_closed_port(service: ServiceFactory) -> None:
    error = upstream_error(service, "UPSTREAM_UNREACHABLE", smtp_port=free_port())
    assert error["upstream"]["stage"] == "connect"
    assert error["upstream"]["code"] is None
    assert "recipients" not in error


def test_case_14_name_that_does_not_resolve(service: ServiceFactory, fake: FakeSMTP) -> None:
    error = upstream_error(service, "UPSTREAM_UNREACHABLE", smtp_host="nowhere.example.org")
    assert error["upstream"] == {
        "stage": "connect",
        "code": None,
        "message": "the server's name could not be resolved",
    }
    assert fake.connections == 0


def test_case_14_closed_before_the_greeting(service: ServiceFactory, fake: FakeSMTP) -> None:
    fake.close_before_greeting = True
    error = upstream_error(service, "UPSTREAM_ERROR")
    assert error["upstream"]["stage"] == "greeting"
    assert error["upstream"]["code"] is None


def test_case_14_broken_while_the_content_is_sent(service: ServiceFactory, fake: FakeSMTP) -> None:
    fake.break_in_data = True
    with service() as client:
        response = client.post("/v1/send", json=request(text="line\n" * 400_000))
    error = response.json()["error"]
    assert error["code"] == "UPSTREAM_ERROR"
    # Both are acceptable: which one depends on buffers.
    assert error["upstream"]["stage"] in ("data", "data_end")
    assert error["upstream"]["code"] is None
    assert [r["status"] for r in error["recipients"]] == ["accepted"]


def test_case_14_broken_after_the_final_reply(service: ServiceFactory, fake: FakeSMTP) -> None:
    fake.responses["END"] = ThenClose("250 2.0.0 Queued")
    status, body = send(service)
    assert status == 200
    assert body["accepted"] == 1
    assert text_of(parse(fake.messages[0])) == "Hello, world."


def test_quit_without_a_reply_changes_nothing(service: ServiceFactory, fake: FakeSMTP) -> None:
    fake.responses["QUIT"] = CLOSE
    status, _ = send(service)
    assert status == 200


def test_addresses_are_tried_in_the_resolver_order(fake: FakeSMTP) -> None:
    """[D21]: one deadline for resolution and every attempt, addresses in the resolver's order."""
    from fastapi.testclient import TestClient

    from mail_dispatch.app import create_app
    from mail_dispatch.settings import Settings

    closed = free_port()

    async def resolve(host: str, port: int) -> list[tuple[int, tuple[Any, ...]]]:
        return [
            (socket.AF_INET, ("127.0.0.1", closed)),
            (socket.AF_INET, ("127.0.0.1", fake.port)),
        ]

    settings = Settings(
        smtp_host="smtp.example.org", smtp_tls="none", smtp_ehlo_name="client.example.org"
    )
    with TestClient(create_app(settings, resolver=resolve)) as client:
        response = client.post("/v1/send", json=request())
    assert response.status_code == 200
    assert fake.connections == 1


def test_connect_deadline_covers_resolution(fake: FakeSMTP) -> None:
    import asyncio

    from fastapi.testclient import TestClient

    from mail_dispatch.app import create_app
    from mail_dispatch.settings import Settings

    async def slow(host: str, port: int) -> list[tuple[int, tuple[Any, ...]]]:
        await asyncio.sleep(5)
        return []

    settings = Settings(
        smtp_host="smtp.example.org",
        smtp_tls="none",
        smtp_ehlo_name="client.example.org",
        smtp_timeout_seconds=0.2,
    )
    started = time.monotonic()
    with TestClient(create_app(settings, resolver=slow)) as client:
        response = client.post("/v1/send", json=request())
    assert time.monotonic() - started < 2
    error = response.json()["error"]
    assert response.status_code == 504
    assert error["code"] == "UPSTREAM_TIMEOUT"
    assert error["upstream"]["stage"] == "connect"


# Case 15 -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("setup", "stage"),
    [
        (lambda fake: fake.responses.__setitem__("EHLO", SILENT), "ehlo"),
        (lambda fake: setattr(fake, "silent_greeting", True), "greeting"),
        (lambda fake: fake.responses.__setitem__("RCPT", SILENT), "rcpt_to"),
        (lambda fake: fake.responses.__setitem__("END", SILENT), "data_end"),
    ],
)
def test_case_15_silence(service: ServiceFactory, fake: FakeSMTP, setup: Any, stage: str) -> None:
    setup(fake)
    started = time.monotonic()
    error = upstream_error(service, "UPSTREAM_TIMEOUT", smtp_timeout_seconds=0.3)
    assert time.monotonic() - started < 0.3 + 1.5
    assert error["upstream"]["stage"] == stage
    assert error["upstream"]["code"] is None
    # After a timeout no QUIT: the socket has just shown that it does not answer.
    assert fake.wait_until(lambda: fake.last.finished)
    assert "QUIT" not in fake.last.commands


def test_quit_after_a_refusal(service: ServiceFactory, fake: FakeSMTP) -> None:
    fake.responses["MAIL"] = "550 5.7.1 no"
    upstream_error(service, "UPSTREAM_REJECTED")
    assert fake.wait_until(lambda: fake.last.finished)
    assert fake.last.commands[-1] == "QUIT"


# Case 16 -----------------------------------------------------------------------------------


def tls(certificates: Certificates, **settings: Any) -> dict[str, Any]:
    return {"smtp_ca_file": str(certificates.ca), **settings}


def test_case_16_starttls_not_announced(service: ServiceFactory, fake: FakeSMTP) -> None:
    error = upstream_error(service, "UPSTREAM_TLS", smtp_tls="starttls")
    assert error["upstream"] == {
        "stage": "starttls",
        "code": None,
        "message": "the server did not announce STARTTLS",
    }
    assert not any(c.startswith("MAIL") for c in fake.last.commands)


def test_case_16_opportunistic_without_starttls_sends_plain(
    service: ServiceFactory, fake: FakeSMTP
) -> None:
    status, body = send(service, smtp_tls="starttls-opportunistic")
    assert status == 200
    assert body["upstream"]["tls"] is False
    assert fake.last.tls is False
    assert text_of(parse(fake.messages[0])) == "Hello, world."


def test_case_16_opportunistic_with_starttls_refused(
    service: ServiceFactory, fake: FakeSMTP
) -> None:
    fake.starttls = True
    fake.responses["STARTTLS"] = "554 5.7.0 No"
    error = upstream_error(service, "UPSTREAM_TLS", smtp_tls="starttls-opportunistic")
    assert error["upstream"]["stage"] == "starttls"
    assert not any(c.startswith("MAIL") for c in fake.last.commands)


def test_case_16_untrusted_certificate(service: ServiceFactory, fake: FakeSMTP) -> None:
    fake.starttls = True
    error = upstream_error(service, "UPSTREAM_TLS", smtp_tls="starttls")
    assert error["upstream"] == {
        "stage": "starttls",
        "code": None,
        "message": "the server's certificate failed verification",
    }
    assert fake.messages == []


def test_case_16_trusted_with_the_ca_file(
    service: ServiceFactory, fake: FakeSMTP, certificates: Certificates
) -> None:
    fake.starttls = True
    status, body = send(service, **tls(certificates, smtp_tls="starttls"))
    assert status == 200
    assert body["upstream"]["tls"] is True
    assert fake.last.tls is True
    assert fake.last.commands[:2] == ["EHLO client.example.org", "STARTTLS"]
    assert fake.last.commands[2] == "EHLO client.example.org"
    assert text_of(parse(fake.messages[0])) == "Hello, world."


def test_certificate_for_another_name(
    service: ServiceFactory, fake: FakeSMTP, certificates: Certificates
) -> None:
    fake.starttls = True
    fake.use_other_certificate = True
    error = upstream_error(service, "UPSTREAM_TLS", **tls(certificates, smtp_tls="starttls"))
    assert error["upstream"]["message"] == "the server's certificate failed verification"


def test_verification_off_accepts_any_certificate(service: ServiceFactory, fake: FakeSMTP) -> None:
    fake.starttls = True
    fake.use_other_certificate = True
    status, body = send(service, smtp_tls="starttls", smtp_tls_verify=False)
    assert status == 200
    assert body["upstream"]["tls"] is True


def test_case_16_implicit(
    service: ServiceFactory, implicit_fake: FakeSMTP, certificates: Certificates
) -> None:
    status, body = send(service, server=implicit_fake, **tls(certificates, smtp_tls="implicit"))
    assert status == 200
    assert body["upstream"]["tls"] is True
    assert implicit_fake.last.tls is True
    assert text_of(parse(implicit_fake.messages[0])) == "Hello, world."


def test_case_16_implicit_with_a_bad_certificate(
    service: ServiceFactory, implicit_fake: FakeSMTP
) -> None:
    error = upstream_error(service, "UPSTREAM_TLS", server=implicit_fake, smtp_tls="implicit")
    assert error["upstream"] == {
        "stage": "connect",
        "code": None,
        "message": "the server's certificate failed verification",
    }


def test_case_16_implicit_with_a_failed_handshake(
    service: ServiceFactory, fake: FakeSMTP, certificates: Certificates
) -> None:
    # A server that speaks plain text where TLS is expected.
    error = upstream_error(service, "UPSTREAM_TLS", **tls(certificates, smtp_tls="implicit"))
    assert error["upstream"] == {
        "stage": "connect",
        "code": None,
        "message": "the TLS handshake failed",
    }


def test_starttls_never_falls_back_after_a_failed_handshake(
    service: ServiceFactory, fake: FakeSMTP
) -> None:
    """[D26]: a failed attempt is UPSTREAM_TLS in the opportunistic mode too."""
    fake.starttls = True
    error = upstream_error(service, "UPSTREAM_TLS", smtp_tls="starttls-opportunistic")
    assert error["upstream"]["stage"] == "starttls"
    assert fake.messages == []


# Case 17 -----------------------------------------------------------------------------------


def credentials(
    certificates: Certificates, password: str = "password", **settings: Any
) -> dict[str, Any]:
    return tls(
        certificates,
        smtp_tls="starttls",
        smtp_username="user",
        smtp_password=password,
        **settings,
    )


@pytest.fixture
def secured(fake: FakeSMTP) -> FakeSMTP:
    fake.starttls = True
    fake.auth = ["PLAIN", "LOGIN"]
    return fake


def test_case_17_auth_announced_after_starttls(
    service: ServiceFactory, secured: FakeSMTP, certificates: Certificates
) -> None:
    status, body = send(service, **credentials(certificates))
    assert status == 200
    assert body["upstream"] == {
        "host": "smtp.example.org",
        "port": secured.port,
        "tls": True,
        "authenticated": True,
    }
    assert secured.last.auth == [("PLAIN", "user", "password")]
    assert "AUTH PLAIN" not in " ".join(secured.last.commands[:2])
    assert text_of(parse(secured.messages[0])) == "Hello, world."


def test_case_17_login_when_plain_is_not_announced(
    service: ServiceFactory, secured: FakeSMTP, certificates: Certificates
) -> None:
    secured.auth = ["LOGIN"]
    status, _ = send(service, **credentials(certificates))
    assert status == 200
    assert secured.last.auth == [("LOGIN", "user", "password")]


def test_the_password_is_utf8(
    service: ServiceFactory, secured: FakeSMTP, certificates: Certificates
) -> None:
    secured.credentials = ("user", "pässwörd€")
    status, _ = send(service, **credentials(certificates, password="pässwörd€"))
    assert status == 200
    assert secured.last.auth == [("PLAIN", "user", "pässwörd€")]


def test_case_17_no_auth_announced(
    service: ServiceFactory, secured: FakeSMTP, certificates: Certificates
) -> None:
    secured.auth = []
    error = upstream_error(service, "UPSTREAM_AUTH", **credentials(certificates))
    assert error["upstream"]["stage"] == "auth"
    assert error["upstream"]["code"] is None


def test_case_17_wrong_password(
    service: ServiceFactory,
    secured: FakeSMTP,
    certificates: Certificates,
    caplog: pytest.LogCaptureFixture,
) -> None:
    password = "not-the-right-password"
    with caplog.at_level(logging.DEBUG):
        status, body = send(service, **credentials(certificates, password=password))
    assert status == 502
    error = body["error"]
    assert error["code"] == "UPSTREAM_AUTH"
    assert error["upstream"] == {"stage": "auth", "code": 535, "message": "5.7.8 Bad credentials"}
    assert sum(1 for c in secured.last.commands if c.startswith("AUTH")) == 1
    import base64

    token = base64.b64encode(f"\0user\0{password}".encode()).decode()
    for text in (
        repr(body),
        caplog.text,
        repr([getattr(r, "fields", None) for r in caplog.records]),
    ):
        assert password not in text
        assert token not in text


def test_case_17_login_refused_after_the_user_name(
    service: ServiceFactory, secured: FakeSMTP, certificates: Certificates
) -> None:
    secured.auth = ["LOGIN"]
    secured.responses["LOGIN_USER"] = "535 5.7.8 No such user"
    error = upstream_error(service, "UPSTREAM_AUTH", **credentials(certificates))
    assert error["upstream"] == {"stage": "auth", "code": 535, "message": "5.7.8 No such user"}
    assert sum(1 for c in secured.last.commands if c.startswith("AUTH")) == 1
    assert not any(c.startswith("MAIL") for c in secured.last.commands)


@pytest.mark.parametrize(
    ("verb", "reply", "code"),
    [
        ("LOGIN_USER", "235 2.7.0 Too early", "UPSTREAM_ERROR"),
        ("LOGIN_USER", "354 odd", "UPSTREAM_ERROR"),
        ("LOGIN_USER", "454 4.7.0 Try later", "UPSTREAM_TRANSIENT"),
        ("LOGIN_PASSWORD", "334 more?", "UPSTREAM_ERROR"),
        ("AUTH", "454 4.7.0 Try later", "UPSTREAM_TRANSIENT"),
    ],
)
def test_auth_login_out_of_course(
    service: ServiceFactory,
    secured: FakeSMTP,
    certificates: Certificates,
    verb: str,
    reply: str,
    code: str,
) -> None:
    """[D39]: in AUTH LOGIN a 2xx or a 3xx other than 334 is out of course; 4xx/5xx keep their
    meaning."""
    secured.auth = ["LOGIN"]
    secured.responses[verb] = reply
    error = upstream_error(service, code, **credentials(certificates))
    assert error["upstream"]["stage"] == "auth"


def test_auth_plain_answered_with_334(
    service: ServiceFactory, secured: FakeSMTP, certificates: Certificates
) -> None:
    """[D39]: `AUTH PLAIN` carries its initial response, so a challenge is out of course and
    nothing more is sent."""
    secured.auth = ["PLAIN"]
    secured.responses["AUTH"] = "334 VXNlcm5hbWU6"
    error = upstream_error(service, "UPSTREAM_ERROR", **credentials(certificates))
    assert error["upstream"] == {"stage": "auth", "code": 334, "message": "VXNlcm5hbWU6"}
    assert sum(1 for c in secured.last.commands if c.startswith("AUTH")) == 1
    assert not any(c.startswith("MAIL") for c in secured.last.commands)


def test_case_17_credentials_never_travel_without_tls(
    service: ServiceFactory, fake: FakeSMTP
) -> None:
    fake.auth = ["PLAIN"]
    fake.auth_before_tls = True
    error = upstream_error(
        service,
        "UPSTREAM_TLS",
        smtp_tls="starttls-opportunistic",
        smtp_username="user",
        smtp_password="password",
    )
    assert error["upstream"]["stage"] == "starttls"
    assert fake.last.auth == []
    assert not any(c.startswith("AUTH") for c in fake.last.commands)


# Protocol violations -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("greeting", "message"),
    [
        ("hello there", "the server sent a reply that is not SMTP"),
        ("220 " + "x" * 70_000, "the server sent a reply line that is too long"),
    ],
)
def test_a_reply_that_is_not_smtp(
    service: ServiceFactory, fake: FakeSMTP, greeting: str, message: str
) -> None:
    fake.greeting = greeting
    error = upstream_error(service, "UPSTREAM_ERROR")
    assert error["upstream"] == {"stage": "greeting", "code": None, "message": message}


def test_data_before_the_tls_handshake_is_refused(
    service: ServiceFactory, fake: FakeSMTP, certificates: Certificates
) -> None:
    """A reply that arrives together with the STARTTLS go-ahead would be read as if it came
    under TLS; the conversation ends instead."""
    fake.starttls = True
    fake.responses["STARTTLS"] = "220 2.0.0 Go ahead\r\n250 injected"
    error = upstream_error(service, "UPSTREAM_ERROR", **tls(certificates, smtp_tls="starttls"))
    assert error["upstream"] == {
        "stage": "starttls",
        "code": None,
        "message": "the server sent data before the TLS handshake",
    }


def test_starttls_answered_out_of_course(
    service: ServiceFactory, fake: FakeSMTP, certificates: Certificates
) -> None:
    fake.starttls = True
    fake.responses["STARTTLS"] = "354 what"
    error = upstream_error(service, "UPSTREAM_ERROR", **tls(certificates, smtp_tls="starttls"))
    assert error["upstream"] == {"stage": "starttls", "code": 354, "message": "what"}


def test_the_system_resolver_keeps_the_resolver_order() -> None:
    import asyncio

    from mail_dispatch.smtp import system_resolver

    addresses = asyncio.run(system_resolver("127.0.0.1", 25))
    assert addresses == [(socket.AF_INET, ("127.0.0.1", 25))]
