"""Shared fixtures. The suite is offline and must stay that way (SPEC §10.1)."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from mail_dispatch.app import create_app
from mail_dispatch.settings import Settings

SETTINGS_VARIABLES = [name.upper() for name in Settings.model_fields]


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own environment never leaks into a test's configuration."""
    for name in list(os.environ):
        if name.upper() in SETTINGS_VARIABLES:
            monkeypatch.delenv(name)


@pytest.fixture
def settings() -> Settings:
    return Settings(smtp_host="mail.example.org", smtp_ehlo_name="client.example.org")


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client
