"""SPEC §10.3: mutated requests never give anything but a 4xx envelope or a correct send, never
open a connection before validation, and never end in an exception outside the envelope."""

from __future__ import annotations

import copy
import json
import random
from typing import Any

import pytest
from builders import PNG, b64, request
from conftest import ServiceFactory
from fakesmtp import FakeSMTP
from mailparse import assert_wire_format

STRINGS = [
    "",
    " ",
    "\t",
    "\x00",
    "\x85",
    "\x9f",
    "\ud800",
    "\udfff",
    "=?utf-8?b?QQ==?=",
    "a@example.org",
    "Name <a@example.org>",
    "ż" * 300,
    "x" * 10_000,
    "\N{LINE SEPARATOR}",
    "\N{ZERO WIDTH NO-BREAK SPACE}",
    "a\r\nBcc: injected@example.org",
    "QUJD",
    "!!!!",
    "..",
    "multipart/mixed",
    "text/plain; boundary=x",
]
VALUES: list[Any] = [None, 0, -1, 1.5, True, [], {}, [None], {"a": "b"}, [[]], *STRINGS]


def baseline() -> dict[str, Any]:
    return request(
        to=[{"address": "to@example.org", "name": "To"}],
        cc=[{"address": "cc@example.org"}],
        bcc=[{"address": "bcc@example.org"}],
        reply_to=[{"address": "reply@example.org"}],
        html="<p>Hi</p>",
        inline=[{"cid": "logo", "content_type": "image/png", "content_base64": b64(PNG)}],
        attachments=[{"filename": "a.pdf", "content_base64": b64(b"%PDF")}],
        headers={"X-Tag": "value"},
    )


def paths(node: Any, prefix: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    found = [prefix] if prefix else []
    if isinstance(node, dict):
        for key, value in node.items():
            found.extend(paths(value, (*prefix, key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(paths(value, (*prefix, index)))
    return found


def mutate(document: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    mutated = copy.deepcopy(document)
    for _ in range(rng.randint(1, 3)):
        target = rng.choice(paths(mutated))
        parent: Any = mutated
        for step in target[:-1]:
            parent = parent[step]
        key = target[-1]
        action = rng.random()
        if action < 0.15 and isinstance(parent, dict):
            del parent[key]
        elif action < 0.3 and isinstance(parent[key], str) and "base64" in str(key):
            parent[key] = "".join(
                rng.choice("ABCD+/=-_ \t\r\n!") for _ in range(rng.randint(0, 40))
            )
        elif action < 0.4 and isinstance(parent, list):
            parent.extend(copy.deepcopy(parent[key]) for _ in range(rng.randint(1, 3)))
        elif action < 0.5 and isinstance(parent, dict):
            parent[rng.choice(["x", "Bcc", "content-type", "X-\x85", key + "x"])] = rng.choice(
                VALUES
            )
        else:
            parent[key] = copy.deepcopy(rng.choice(VALUES))
    return mutated


def check(client: Any, fake: FakeSMTP, document: Any) -> int:
    before = fake.connections
    body = json.dumps(document, ensure_ascii=True).encode()
    response = client.post("/v1/send", content=body, headers={"content-type": "application/json"})
    assert response.headers["content-type"] == "application/json"
    payload = response.json()
    if response.status_code == 200:
        assert payload["ok"] is True
        assert fake.wait_until(lambda: len(fake.messages) >= 1)
        assert_wire_format(fake.messages[-1])
    else:
        assert 400 <= response.status_code < 500, (response.status_code, payload, document)
        assert payload["ok"] is False
        assert fake.connections == before, "a refused request opened a connection"
    status: int = response.status_code
    return status


@pytest.mark.parametrize("seed", range(8))
def test_mutated_requests(service: ServiceFactory, fake: FakeSMTP, seed: int) -> None:
    rng = random.Random(seed)
    statuses: set[int] = set()
    with service() as client:
        for _ in range(40):
            statuses.add(check(client, fake, mutate(baseline(), rng)))
    assert statuses - {200}, "every mutation was accepted, so none exercised validation"


def test_thousands_of_recipients(service: ServiceFactory, fake: FakeSMTP) -> None:
    document = request(to=[{"address": f"r{n}@example.org"} for n in range(5000)])
    with service() as client:
        assert check(client, fake, document) == 400


def test_header_injection_is_refused(service: ServiceFactory, fake: FakeSMTP) -> None:
    for document in (
        request(subject="hi\r\nBcc: victim@example.org"),
        request(to=[{"address": "a@example.org", "name": "x\nBcc: victim@example.org"}]),
        request(headers={"X-A": "b\r\nBcc: victim@example.org"}),
        request(attachments=[{"filename": "a\r\nBcc: v@example.org", "content_base64": ""}]),
    ):
        with service() as client:
            assert check(client, fake, document) == 400
