"""JSON lines on stdout, one event per request (SPEC §9), and the entry point [D28]."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mail_dispatch import __main__ as entry
from mail_dispatch.jsonlog import JsonFormatter, configure_logging, log_event


def events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    formatter = JsonFormatter()
    return [
        json.loads(formatter.format(record))
        for record in caplog.records
        if record.name == "mail_dispatch"
    ]


def test_an_event_is_one_json_line(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        log_event("request", dispatch_id="d", status=400, code="INVALID_REQUEST")
    (line,) = [JsonFormatter().format(record) for record in caplog.records]
    assert "\n" not in line
    entry_ = json.loads(line)
    assert entry_["event"] == "request"
    assert entry_["level"] == "INFO"
    assert entry_["dispatch_id"] == "d"
    assert entry_["ts"].endswith("Z")


def test_an_exception_is_logged_by_its_type_alone() -> None:
    try:
        raise ValueError("text that may quote content")
    except ValueError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", None, sys.exc_info())
    entry_ = json.loads(JsonFormatter().format(record))
    assert entry_["exception"] == "ValueError"
    assert "quote content" not in json.dumps(entry_)
    assert entry_["logger"] == "x"
    assert entry_["message"] == "boom"


def test_every_request_logs_one_event_with_its_dispatch_id(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        dispatch_id = client.get("/nowhere").json()["dispatch_id"]
    (event,) = events(caplog)
    assert event["event"] == "request"
    assert event["dispatch_id"] == dispatch_id
    assert event["code"] == "NOT_FOUND"
    assert event["status"] == 404


def test_configure_logging_writes_json_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        configure_logging("WARNING")
        logging.getLogger("uvicorn.error").warning("from uvicorn")
        log_event("hidden")
        line = capsys.readouterr().out.strip()
    finally:
        root.handlers[:], level = saved
        root.setLevel(level)
    assert json.loads(line) == {
        "ts": json.loads(line)["ts"],
        "level": "WARNING",
        "logger": "uvicorn.error",
        "message": "from uvicorn",
    }


@pytest.fixture
def quiet_root() -> Any:
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    yield
    root.handlers[:], level = saved
    root.setLevel(level)


def test_invalid_configuration_stops_startup(
    quiet_root: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SMTP_HOST", "mail.example.org")
    monkeypatch.setenv("SMTP_TLS", "none")
    monkeypatch.setenv("SMTP_USERNAME", "user")
    monkeypatch.setenv("SMTP_PASSWORD", "s3cr3t-password")
    started: list[object] = []
    monkeypatch.setattr(entry.uvicorn, "run", lambda *a, **k: started.append(a))
    assert entry.main() == 2
    assert started == []
    out = capsys.readouterr().out
    event = json.loads(out)
    assert event["event"] == "config_error"
    assert "SMTP_TLS=none" in event["message"]
    assert "s3cr3t" not in out


def test_valid_configuration_serves_on_the_configured_port(
    quiet_root: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SMTP_HOST", "mail.example.org")
    monkeypatch.setenv("SMTP_EHLO_NAME", "client.example.org")
    monkeypatch.setenv("PORT", "8025")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    assert entry.main() == 0
    (kwargs,) = calls
    assert kwargs["port"] == 8025
    assert kwargs["access_log"] is False
    assert kwargs["log_config"] is None
