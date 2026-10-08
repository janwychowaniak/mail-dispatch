"""Shared fixtures. The suite is offline and must stay that way (SPEC §10.1)."""

from __future__ import annotations

import os

import pytest

from mail_dispatch.settings import Settings

SETTINGS_VARIABLES = [name.upper() for name in Settings.model_fields]


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own environment never leaks into a test's configuration."""
    for name in list(os.environ):
        if name.upper() in SETTINGS_VARIABLES:
            monkeypatch.delenv(name)
