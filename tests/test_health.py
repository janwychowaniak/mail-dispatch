"""Acceptance case 18 of SPEC §10.2: the lazy, cached health probe (§6) [D25] [D37] [D43]."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from certs import SERVER_NAME, Certificates
from conftest import ServiceFactory, substituted_resolver
from fakesmtp import SILENT, Delayed, FakeSMTP
from fastapi.testclient import TestClient

from mail_dispatch import __version__
from mail_dispatch.app import create_app
from mail_dispatch.settings import Settings


def health(service: ServiceFactory, **settings: Any) -> dict[str, Any]:
    with service(**settings) as client:
        response = client.get("/v1/health")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert "dispatch_id" not in body
    return body


def test_case_18_capabilities_after_starttls(
    service: ServiceFactory, fake: FakeSMTP, certificates: Certificates
) -> None:
    fake.starttls = True
    fake.capabilities = ["PIPELINING"]
    fake.tls_capabilities = ["SIZE 52428800", "8BITMIME", "SMTPUTF8", "PIPELINING"]
    fake.auth = ["plain", "LOGIN"]
    body = health(
        service,
        smtp_tls="starttls",
        smtp_ca_file=str(certificates.ca),
        smtp_username="user",
        smtp_password="password",
    )
    assert body["ok"] is True
    assert body["version"] == __version__
    assert isinstance(body["uptime_seconds"], int)
    upstream = body["upstream"]
    assert upstream["host"] == SERVER_NAME
    assert upstream["port"] == fake.port
    assert upstream["tls_mode"] == "starttls"
    assert upstream["auth_configured"] is True
    assert upstream["status"] == "ok"
    assert upstream["checked_age_seconds"] == 0
    assert upstream["capabilities"] == {
        "size_max_bytes": 52428800,
        "starttls": True,
        "auth_methods": ["PLAIN", "LOGIN"],
        "eightbitmime": True,
        "smtputf8": True,
        "pipelining": True,
    }
    assert "error" not in upstream
    # The probe never authenticates and never starts a transaction.
    assert fake.wait_until(lambda: fake.last.finished)
    assert fake.last.commands == [
        "EHLO client.example.org",
        "STARTTLS",
        "EHLO client.example.org",
        "QUIT",
    ]
    assert fake.last.auth == []


def test_case_18_two_calls_within_the_ttl_make_one_probe(
    service: ServiceFactory, fake: FakeSMTP
) -> None:
    with service(health_cache_ttl_seconds=60) as client:
        first = client.get("/v1/health").json()
        second = client.get("/v1/health").json()
    assert first["upstream"]["status"] == second["upstream"]["status"] == "ok"
    assert fake.connections == 1


def test_a_stale_cache_probes_again(fake: FakeSMTP) -> None:
    now = [100.0]
    settings = Settings(
        smtp_host=SERVER_NAME,
        smtp_port=fake.port,
        smtp_tls="none",
        smtp_ehlo_name="client.example.org",
        health_cache_ttl_seconds=10,
    )
    app = create_app(
        settings, clock=lambda: now[0], resolver=substituted_resolver({SERVER_NAME: fake.port})
    )
    with TestClient(app) as client:
        client.get("/v1/health")
        now[0] += 7.9
        assert client.get("/v1/health").json()["upstream"]["checked_age_seconds"] == 7
        assert fake.connections == 1
        now[0] += 3
        assert client.get("/v1/health").json()["upstream"]["checked_age_seconds"] == 0
        assert fake.connections == 2


def test_case_18_size_without_a_value(service: ServiceFactory, fake: FakeSMTP) -> None:
    fake.capabilities = ["SIZE"]
    capabilities = health(service)["upstream"]["capabilities"]
    assert capabilities["size_max_bytes"] is None
    fake.capabilities = ["SIZE 0"]
    assert health(service)["upstream"]["capabilities"]["size_max_bytes"] is None


def test_legacy_auth_form_is_read(service: ServiceFactory, fake: FakeSMTP) -> None:
    fake.capabilities = ["AUTH=LOGIN PLAIN", "AUTH CRAM-MD5 login"]
    capabilities = health(service)["upstream"]["capabilities"]
    assert capabilities["auth_methods"] == ["LOGIN", "PLAIN", "CRAM-MD5"]
    assert capabilities["starttls"] is False


def test_case_18_server_switched_off(service: ServiceFactory, fake: FakeSMTP) -> None:
    fake.stop()
    body = health(service, health_cache_ttl_seconds=10)
    assert body["ok"] is False
    upstream = body["upstream"]
    assert upstream["status"] == "down"
    assert "capabilities" not in upstream
    assert upstream["checked_age_seconds"] <= 10
    assert upstream["error"]["stage"] == "connect"
    assert upstream["error"]["code"] is None


def test_case_18_tls_failed_with_credentials_and_no_starttls(
    service: ServiceFactory, fake: FakeSMTP
) -> None:
    body = health(
        service,
        smtp_tls="starttls-opportunistic",
        smtp_username="user",
        smtp_password="password",
    )
    assert body["ok"] is False
    assert body["upstream"]["status"] == "tls_failed"
    assert body["upstream"]["error"]["stage"] == "starttls"


def test_case_18_implicit_with_a_bad_certificate(
    service: ServiceFactory, implicit_fake: FakeSMTP
) -> None:
    body = health(service, server=implicit_fake, smtp_tls="implicit")
    assert body["upstream"]["status"] == "tls_failed"
    assert body["upstream"]["error"] == {
        "stage": "connect",
        "code": None,
        "message": "the server's certificate failed verification",
    }


def test_the_server_reply_is_reported(service: ServiceFactory, fake: FakeSMTP) -> None:
    fake.greeting = "554 5.3.2 Not accepting connections"
    upstream = health(service)["upstream"]
    assert upstream["status"] == "down"
    assert upstream["error"] == {
        "stage": "greeting",
        "code": 554,
        "message": "5.3.2 Not accepting connections",
    }


@pytest.mark.parametrize(
    "setup",
    [
        lambda fake: setattr(fake, "silent_greeting", True),
        lambda fake: fake.responses.__setitem__("EHLO", SILENT),
    ],
)
def test_the_probe_has_one_deadline(service: ServiceFactory, fake: FakeSMTP, setup: Any) -> None:
    setup(fake)
    started = time.monotonic()
    body = health(service, health_probe_timeout_seconds=0.4, smtp_timeout_seconds=30)
    assert time.monotonic() - started < 0.4 + 1.0
    assert body["upstream"]["status"] == "timeout"
    assert body["upstream"]["error"]["code"] is None


def test_quit_lies_outside_the_measurement(service: ServiceFactory, fake: FakeSMTP) -> None:
    """[D37]: a slow QUIT changes neither the result nor its age, and the response still comes
    within the deadline."""
    fake.responses["QUIT"] = Delayed(5, "221 bye")
    started = time.monotonic()
    body = health(service, health_probe_timeout_seconds=0.5)
    assert time.monotonic() - started < 0.5 + 1.0
    assert body["upstream"]["status"] == "ok"


def test_concurrent_requests_share_one_probe(fake: FakeSMTP) -> None:
    from mail_dispatch.health import Health
    from mail_dispatch.smtp import tls_context

    fake.responses["EHLO"] = Delayed(0.2, "250 fake.example.org")
    settings = Settings(
        smtp_host=SERVER_NAME, smtp_port=fake.port, smtp_tls="none", smtp_ehlo_name="c.example.org"
    )
    probe = Health(settings, tls_context(settings), substituted_resolver({SERVER_NAME: fake.port}))

    async def together() -> list[Any]:
        return list(await asyncio.gather(*(probe.current() for _ in range(5))))

    results = asyncio.run(together())
    assert {result.status for result in results} == {"ok"}
    assert len({id(result) for result in results}) == 1
    assert fake.connections == 1


def test_password_never_appears_in_health(
    service: ServiceFactory, fake: FakeSMTP, certificates: Certificates
) -> None:
    fake.starttls = True
    fake.auth = ["PLAIN"]
    body = health(
        service,
        smtp_tls="starttls",
        smtp_ca_file=str(certificates.ca),
        smtp_username="user",
        smtp_password="Sup3r-S3cret",
    )
    assert "Sup3r-S3cret" not in repr(body)


def test_an_exception_in_health_is_enveloped(
    service: ServiceFactory, fake: FakeSMTP, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[D33]: an INTERNAL_ERROR raised while handling health carries a dispatch_id too."""
    from mail_dispatch import health as health_module

    def explode(reply: Any) -> None:
        raise RuntimeError("probe")

    monkeypatch.setattr(health_module, "expect_greeting", explode)
    with service() as client:
        response = client.get("/v1/health")
        assert response.status_code == 500
        body = response.json()
        assert body["error"]["code"] == "INTERNAL_ERROR"
        assert body["dispatch_id"]
        # The failed probe is not cached as a measurement; the next request probes again.
        monkeypatch.undo()
        assert client.get("/v1/health").json()["upstream"]["status"] == "ok"
