"""Request documents for tests: one valid baseline, changed one field at a time."""

from __future__ import annotations

import base64
import copy
from typing import Any

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def request(**changes: Any) -> dict[str, Any]:
    """A valid request with `changes` applied; a value of `...` removes the field."""
    document: dict[str, Any] = {
        "from": {"address": "sender@example.org", "name": "Sender"},
        "to": [{"address": "to@example.org"}],
        "subject": "Hello",
        "text": "Hello, world.",
    }
    for key, value in changes.items():
        if value is ...:
            document.pop(key, None)
        else:
            document[key] = copy.deepcopy(value)
    return document
