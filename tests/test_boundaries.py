"""Acceptance cases 21 and 22 of SPEC §10.2: the boundary of §5.4 and §5.6 [D22] [D42] [D47].

An exception is forced on both sides of the final write; the HTTP client goes away on both
sides of it. Case 22 runs the service under a real uvicorn, since only a real socket can go
away.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
import uvicorn
from builders import request
from certs import SERVER_NAME
from conftest import ServiceFactory, substituted_resolver
from fakesmtp import SILENT, Delayed, FakeSMTP
from mailparse import assert_wire_format, parse, text_of

from mail_dispatch import conversation, dispatch
from mail_dispatch.app import create_app
from mail_dispatch.settings import Settings


class Forced(RuntimeError):
    pass


def assert_the_message(raw: bytes | None) -> None:
    """What the fake received is the whole message of `request()`, read independently (§10.1)."""
    assert raw is not None
    assert_wire_format(raw)
    message = parse(raw)
    assert message["Subject"] == "Hello"
    assert text_of(message) == "Hello, world."


def internal_error(service: ServiceFactory) -> dict[str, Any]:
    with service() as client:
        response = client.post("/v1/send", json=request())
    assert response.status_code == 500
    body = response.json()
    assert body["ok"] is False
    assert body["dispatch_id"]
    error: dict[str, Any] = body["error"]
    assert error["code"] == "INTERNAL_ERROR"
    assert "Forced" not in error["message"]
    return error


def test_case_21a_before_composition(
    service: ServiceFactory, fake: FakeSMTP, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: Any, **kwargs: Any) -> None:
        raise Forced("composition")

    monkeypatch.setattr(dispatch, "compose", explode)
    error = internal_error(service)
    assert set(error) == {"code", "message"}
    assert fake.connections == 0


def test_case_21b_during_the_conversation_before_the_final_write(
    service: ServiceFactory, fake: FakeSMTP, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(data: bytes) -> bytes:
        raise Forced("while the content is prepared")

    monkeypatch.setattr(conversation, "dot_stuff", explode)
    error = internal_error(service)
    assert error["upstream"] == {
        "stage": "data",
        "code": None,
        "message": "the service failed during the conversation",
    }
    assert {"message_id", "size_bytes", "duration_ms"} <= set(error)
    assert [r["status"] for r in error["recipients"]] == ["accepted"]
    assert fake.wait_until(lambda: fake.last.finished)
    assert fake.last.final_dot is False
    assert fake.messages == []


def test_case_21b_at_an_earlier_stage(
    service: ServiceFactory, fake: FakeSMTP, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(reply: Any) -> None:
        raise Forced("greeting")

    monkeypatch.setattr(conversation, "expect_greeting", explode)
    error = internal_error(service)
    assert error["upstream"]["stage"] == "greeting"
    assert "recipients" not in error
    assert {"message_id", "size_bytes", "duration_ms"} <= set(error)


def test_case_21c_after_the_final_write_before_the_reply(
    service: ServiceFactory, fake: FakeSMTP, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = conversation.Conversation._final_write

    async def write_then_explode(self: Any, connection: Any) -> None:
        await original(self, connection)
        raise Forced("after the final dot")

    monkeypatch.setattr(conversation.Conversation, "_final_write", write_then_explode)
    error = internal_error(service)
    assert error["upstream"]["stage"] == "data_end"
    assert error["message_id"].startswith("<")
    assert fake.wait_until(lambda: fake.last.final_dot and fake.last.data is not None)
    assert_the_message(fake.last.data)


def test_case_21d_after_the_final_reply(
    service: ServiceFactory,
    fake: FakeSMTP,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def explode(*args: Any, **kwargs: Any) -> None:
        raise Forced("while the response is built")

    monkeypatch.setattr(dispatch, "JSONResponse", explode)
    with caplog.at_level(logging.INFO):
        error = internal_error(service)
    assert error["upstream"]["stage"] == "data_end"
    assert error["message_id"].startswith("<")
    assert [r["status"] for r in error["recipients"]] == ["accepted"]
    assert fake.wait_until(lambda: bool(fake.messages))
    assert_the_message(fake.messages[-1])
    (event,) = [r for r in caplog.records if r.name == "mail_dispatch"]
    assert event.levelno == logging.ERROR
    assert event.fields["stage"] == "data_end"  # type: ignore[attr-defined]
    assert event.fields["exception"] == "Forced"  # type: ignore[attr-defined]


# Case 22 -----------------------------------------------------------------------------------


@contextmanager
def running(fake: FakeSMTP, **overrides: Any) -> Iterator[int]:
    settings = Settings(
        smtp_host=SERVER_NAME,
        smtp_port=fake.port,
        smtp_tls="none",
        smtp_ehlo_name="client.example.org",
        smtp_timeout_seconds=10,
        **overrides,
    )
    app = create_app(settings, resolver=substituted_resolver({SERVER_NAME: fake.port}))
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_config=None, access_log=False, lifespan="off"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    port: int = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def post(port: int, document: Any) -> socket.socket:
    body = json.dumps(document).encode()
    connection = socket.create_connection(("127.0.0.1", port))
    connection.sendall(
        b"POST /v1/send HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )
    return connection


def send_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        r.fields  # type: ignore[attr-defined]
        for r in caplog.records
        if r.name == "mail_dispatch" and r.getMessage() == "send"
    ]


def wait_for(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def test_case_22a_client_gone_before_the_final_write(
    fake: FakeSMTP, caplog: pytest.LogCaptureFixture
) -> None:
    fake.responses["RCPT"] = SILENT
    with caplog.at_level(logging.INFO), running(fake) as port:
        connection = post(port, request())
        assert fake.wait_until(lambda: bool(fake.sessions) and bool(fake.last.rcpt_to))
        time.sleep(0.1)
        connection.close()
        # The conversation is aborted: the fake sees its socket closed, at once and without QUIT.
        assert fake.wait_until(lambda: fake.last.closed_by_client, timeout=3)
        assert wait_for(lambda: send_events(caplog))
    assert fake.messages == []
    assert "QUIT" not in fake.last.commands
    (event,) = send_events(caplog)
    assert event["result"] == "aborted_by_client"
    assert event["stage"] == "rcpt_to"


def test_case_22b_client_gone_after_the_final_write(
    fake: FakeSMTP, caplog: pytest.LogCaptureFixture
) -> None:
    fake.responses["END"] = Delayed(1.0, "250 2.0.0 Queued as 12345")
    with caplog.at_level(logging.INFO), running(fake) as port:
        connection = post(port, request())
        assert fake.wait_until(lambda: bool(fake.sessions) and fake.last.final_dot)
        connection.close()
        # Nothing can be taken back: the reply is awaited, and the message is recorded.
        assert fake.wait_until(lambda: bool(fake.messages), timeout=5)
        assert wait_for(lambda: send_events(caplog))
    assert_the_message(fake.messages[-1])
    (event,) = send_events(caplog)
    assert event["result"] == "sent"
    assert event["stage"] == "data_end"
    assert event["reply"] == {"code": 250, "enhanced": "2.0.0"}
    assert event["client"] == "gone"
    assert "Queued as" not in repr(event)


def test_client_gone_before_the_connection_is_opened(
    fake: FakeSMTP, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client gone while the message is composed: no connection is opened at all."""
    original = dispatch.prepare

    def slow_prepare(*args: Any) -> Any:
        time.sleep(0.5)
        return original(*args)

    monkeypatch.setattr(dispatch, "prepare", slow_prepare)
    with caplog.at_level(logging.INFO), running(fake) as port:
        connection = post(port, request())
        time.sleep(0.1)
        connection.close()
        assert wait_for(lambda: send_events(caplog))
    (event,) = send_events(caplog)
    assert event["result"] == "aborted_by_client"
    assert "stage" not in event
    assert fake.connections == 0


def test_a_real_server_answers_normally(fake: FakeSMTP) -> None:
    with running(fake) as port:
        connection = post(port, request())
        connection.settimeout(5)
        received = b""
        while b"\r\n\r\n" not in received or not received.rstrip().endswith(b"}"):
            chunk = connection.recv(65536)
            if not chunk:
                break
            received += chunk
        connection.close()
    head, body = received.split(b"\r\n\r\n", 1)
    assert head.startswith(b"HTTP/1.1 200")
    assert json.loads(body)["accepted"] == 1


def test_the_stage_is_data_end_when_the_final_write_is_issued(
    service: ServiceFactory, fake: FakeSMTP, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[D22] [D47]: not when a reply arrives, and not once the write has drained — at the
    moment it is issued, so a disconnection noticed during the write already waits."""
    from mail_dispatch import smtp

    seen: list[str] = []
    original = smtp.Connection.write

    async def watching(self: Any, data: bytes) -> None:
        if data.endswith(b"\r\n.\r\n"):
            seen.append(self.stage)
        await original(self, data)

    monkeypatch.setattr(smtp.Connection, "write", watching)
    with service() as client:
        assert client.post("/v1/send", json=request()).status_code == 200
    assert seen == ["data_end"]
