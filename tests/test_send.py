"""Acceptance cases 1-13, 19 and 20 of SPEC §10.2, through HTTP and the fake server.

Every case in which the message reached the fake ends with an assertion on the raw message it
recorded, parsed independently with the standard library (§10.1).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from typing import Any

import pytest
from builders import PNG, b64, request
from conftest import ServiceFactory
from fakesmtp import FakeSMTP
from mailparse import assert_wire_format, display_name, header_names, parse, structure, text_of


def send(service: ServiceFactory, document: Any, **settings: Any) -> tuple[int, dict[str, Any]]:
    with service(**settings) as client:
        response = client.post("/v1/send", json=document)
    assert response.headers["content-type"] == "application/json"
    return response.status_code, response.json()


def delivered(fake: FakeSMTP) -> bytes:
    assert fake.wait_until(lambda: bool(fake.messages)), "the fake recorded no message"
    raw = fake.messages[-1]
    assert_wire_format(raw)
    return raw


def refused_without_connection(
    service: ServiceFactory, fake: FakeSMTP, document: Any, code: str, **settings: Any
) -> dict[str, Any]:
    status, body = send(service, document, **settings)
    assert body["ok"] is False
    assert body["error"]["code"] == code
    assert 400 <= status < 500
    assert fake.connections == 0, "a 4xx must never open a connection"
    error: dict[str, Any] = body["error"]
    return error


# Case 1 ------------------------------------------------------------------------------------


def test_case_1_text_only(service: ServiceFactory, fake: FakeSMTP) -> None:
    text = "First line\n.starts with a dot\n.\n..two dots\nlast"
    status, body = send(service, request(text=text, subject=""))
    assert status == 200
    raw = delivered(fake)
    message = parse(raw)
    assert message.get_content_type() == "text/plain"
    assert message["Content-Transfer-Encoding"] == "quoted-printable"
    assert text_of(message) == text
    assert message["Subject"] == ""
    assert "Subject" in header_names(raw)
    assert message["MIME-Version"] == "1.0"
    assert re.fullmatch(r"\w{3}, \d\d \w{3} \d{4} \d\d:\d\d:\d\d \+0000", str(message["Date"]))
    assert message["Message-ID"] == body["message_id"]
    assert re.fullmatch(r"<[0-9a-f]{32}@example\.org>", body["message_id"])
    # size_bytes is the message on the wire with CRLF and without dot-stuffing.
    assert body["size_bytes"] == len(raw)
    # Dot transparency: stuffed on the wire, unstuffed by the fake, identical when decoded.
    assert b"\r\n..starts with a dot\r\n" in fake.last.wire
    assert b"\r\n..\r\n" in fake.last.wire


def test_case_1_non_ascii_text_is_base64(service: ServiceFactory, fake: FakeSMTP) -> None:
    text = "Привет, мир! Как дела?\n" * 4
    status, _ = send(service, request(text=text))
    assert status == 200
    message = parse(delivered(fake))
    assert message["Content-Transfer-Encoding"] == "base64"
    assert text_of(message) == text


# Case 2 ------------------------------------------------------------------------------------


def test_case_2_html_only(service: ServiceFactory, fake: FakeSMTP) -> None:
    status, _ = send(service, request(text=..., html="<p>Hello</p>"))
    assert status == 200
    message = parse(delivered(fake))
    assert message.get_content_type() == "text/html"
    assert text_of(message) == "<p>Hello</p>"


def test_case_2_html_with_inline(service: ServiceFactory, fake: FakeSMTP) -> None:
    inline = [
        {
            "cid": "logo",
            "content_type": "image/png",
            "content_base64": b64(PNG),
            "filename": "logo.png",
        },
        {"cid": "spacer", "content_type": "image/gif", "content_base64": b64(b"GIF89a")},
    ]
    status, _ = send(service, request(text=..., html='<img src="cid:logo">', inline=inline))
    assert status == 200
    message = parse(delivered(fake))
    assert structure(message) == ("multipart/related", ["text/html", "image/png", "image/gif"])
    assert message.get_param("type") == "text/html"
    _, logo, spacer = message.iter_parts()
    assert logo["Content-ID"] == "<logo>"
    assert logo.get_content_disposition() == "inline"
    assert logo.get_payload(decode=True) == PNG
    assert spacer.get_filename() is None
    assert "filename" not in str(spacer["Content-Disposition"])


# Case 3 ------------------------------------------------------------------------------------


def test_case_3_full_structure(service: ServiceFactory, fake: FakeSMTP) -> None:
    pdf = b"%PDF-1.7\n" + bytes(range(256)) * 50
    attachments = [
        {"filename": "report.pdf", "content_type": "application/pdf", "content_base64": b64(pdf)},
        {
            "filename": "notes.txt",
            "content_type": "text/plain",
            "content_base64": b64(b"plain text\n"),
        },
        {"filename": "data.qqq", "content_base64": b64(b"\x00\x01")},
        {"filename": "forwarded.eml", "content_base64": b64(b"Subject: x\r\n\r\nbody")},
        {"filename": "table.CSV", "content_base64": b64(b"a,b\n")},
    ]
    inline = [{"cid": "logo", "content_type": "image/png", "content_base64": b64(PNG)}]
    document = request(html='<img src="cid:logo">', inline=inline, attachments=attachments)
    status, _ = send(service, document)
    assert status == 200
    message = parse(delivered(fake))
    assert structure(message) == (
        "multipart/mixed",
        [
            (
                "multipart/alternative",
                ["text/plain", ("multipart/related", ["text/html", "image/png"])],
            ),
            "application/pdf",
            "text/plain",
            "application/octet-stream",
            "application/octet-stream",
            "text/csv",
        ],
    )
    parts = list(message.iter_parts())[1:]
    for part, given in zip(parts, attachments, strict=True):
        payload = part.get_payload(decode=True)
        assert isinstance(payload, bytes)
        expected = base64.b64decode(given["content_base64"])
        assert hashlib.sha256(payload).hexdigest() == hashlib.sha256(expected).hexdigest()
        assert part["Content-Transfer-Encoding"] == "base64"
        assert part.get_filename() == given["filename"]


def test_case_3_boundary_parameter_is_refused(service: ServiceFactory, fake: FakeSMTP) -> None:
    attachments = [
        {"filename": "a.txt", "content_type": "text/plain; boundary=x", "content_base64": ""}
    ]
    error = refused_without_connection(
        service, fake, request(attachments=attachments), "INVALID_CONTENT"
    )
    assert error["field"] == "attachments[0].content_type"


# Case 4 ------------------------------------------------------------------------------------


def test_case_4_encoded_headers(service: ServiceFactory, fake: FakeSMTP) -> None:
    long = "x" * 1000
    document = request(
        subject="Zażółć gęślą jaźń — raport",
        **{"from": {"address": "sender@example.org", "name": "Łukasz Żółw"}},
        to=[{"address": "to@example.org", "name": "   "}],
        cc=[{"address": "cc@example.org", "name": long}],
        attachments=[
            {"filename": "raport końcowy.pdf", "content_base64": ""},
            {"filename": "a" * 196 + ".txt", "content_base64": ""},
        ],
    )
    status, _ = send(service, document)
    assert status == 200
    raw = delivered(fake)
    message = parse(raw)
    assert message["Subject"] == "Zażółć gęślą jaźń — raport"
    assert message["From"].addresses[0].display_name == "Łukasz Żółw"
    assert message["To"].addresses[0].display_name == ""
    assert display_name(raw, "Cc") == long
    assert [p.get_filename() for p in list(message.iter_parts())[1:]] == [
        "raport końcowy.pdf",
        "a" * 196 + ".txt",
    ]
    assert b"=?utf-8?b?" in raw
    assert b"filename*=utf-8''" in raw
    assert b"filename*0=" in raw


def test_case_4_unfoldable_subject(service: ServiceFactory, fake: FakeSMTP) -> None:
    long = "y" * 1000
    status, _ = send(service, request(subject=long))
    assert status == 200
    raw = delivered(fake)
    assert parse(raw)["Subject"] == long
    assert all(len(line) <= 998 for line in raw.split(b"\r\n"))


# Case 5 ------------------------------------------------------------------------------------


def test_case_5_envelope_and_headers(service: ServiceFactory, fake: FakeSMTP) -> None:
    document = request(
        **{"from": {"address": "self@example.org"}},
        to=...,
        cc=[{"address": "cc@example.org"}],
        bcc=[{"address": "hidden@example.org"}, {"address": "self@example.org"}],
        reply_to=[{"address": "r1@example.org"}, {"address": "r2@example.org", "name": "Second"}],
    )
    status, body = send(service, document)
    assert status == 200
    assert body["accepted"] == 3
    assert fake.last.rcpt_to == ["<cc@example.org>", "<hidden@example.org>", "<self@example.org>"]
    raw = delivered(fake)
    assert b"hidden@example.org" not in raw
    message = parse(raw)
    assert message["To"] is None
    assert message["Bcc"] is None
    assert [a.addr_spec for a in message["Cc"].addresses] == ["cc@example.org"]
    assert [a.addr_spec for a in message["Reply-To"].addresses] == [
        "r1@example.org",
        "r2@example.org",
    ]
    assert header_names(raw).count("Reply-To") == 1
    assert fake.last.mail_from == "MAIL FROM:<self@example.org>"


# Case 6 ------------------------------------------------------------------------------------


def test_case_6_custom_headers(service: ServiceFactory, fake: FakeSMTP) -> None:
    references = " ".join("<" + "r" * 498 + ">" for _ in range(3))
    headers = {
        "In-Reply-To": "<parent@example.org>",
        "References": references,
        "X-Encoded": "=?utf-8?q?caf=C3=A9?=",
    }
    document = request(
        cc=[{"address": "cc@example.org"}], reply_to=[{"address": "r@example.org"}], headers=headers
    )
    status, _ = send(service, document)
    assert status == 200
    raw = delivered(fake)
    assert header_names(raw) == [
        "From",
        "To",
        "Cc",
        "Reply-To",
        "Subject",
        "Date",
        "Message-ID",
        "MIME-Version",
        "In-Reply-To",
        "References",
        "X-Encoded",
        "Content-Type",
        "Content-Transfer-Encoding",
    ]
    head = raw.split(b"\r\n\r\n", 1)[0].decode("ascii")
    assert "\r\nIn-Reply-To: <parent@example.org>\r\n" in head
    assert "\r\nX-Encoded: =?utf-8?q?caf=C3=A9?=\r\n" in head
    unfolded = re.sub(r"\r\n(?= )", "", head)
    assert "\r\nReferences: " + references + "\r\n" in unfolded
    # Folded at the spaces: three lines, each within 998.
    references_lines = re.search(r"References: [^\r]*(\r\n [^\r]*)*", head)
    assert references_lines is not None
    assert references_lines.group().count("\r\n") == 2


@pytest.mark.parametrize(
    ("headers", "field"),
    [
        ({"References": "<" + "x" * 1000 + ">"}, "headers.References"),
        ({"bCC": "x@example.org"}, "headers.bCC"),
        ({"content-type": "text/html"}, "headers.content-type"),
        ({"X-Tag": "a", "x-tag": "b"}, "headers.x-tag"),
    ],
)
def test_case_6_header_refusals(
    service: ServiceFactory, fake: FakeSMTP, headers: dict[str, str], field: str
) -> None:
    error = refused_without_connection(service, fake, request(headers=headers), "INVALID_HEADER")
    assert error["field"] == field


def test_case_6_unfoldable_cid(service: ServiceFactory, fake: FakeSMTP) -> None:
    inline = [{"cid": "c" * 1000, "content_type": "image/png", "content_base64": ""}]
    error = refused_without_connection(
        service, fake, request(html="", inline=inline), "INVALID_HEADER"
    )
    assert (error["field"], error["index"]) == ("inline[0].cid", 0)


# Case 7 ------------------------------------------------------------------------------------


@pytest.mark.parametrize("char", ["\r", "\n", "\t", "\x00"])
@pytest.mark.parametrize(
    ("make", "field", "index"),
    [
        (lambda c: request(subject="a" + c + "b"), "subject", None),
        (lambda c: request(to=[{"address": "to@example.org", "name": "a" + c}]), "to[0].name", 0),
        (lambda c: request(to=[{"address": "to@example.org", "name": c}]), "to[0].name", 0),
        (lambda c: request(to=[{"address": "to" + c + "@example.org"}]), "to[0].address", 0),
        (
            lambda c: request(attachments=[{"filename": "a" + c, "content_base64": ""}]),
            "attachments[0].filename",
            0,
        ),
        (
            lambda c: request(
                html="",
                inline=[{"cid": "a" + c, "content_type": "image/png", "content_base64": ""}],
            ),
            "inline[0].cid",
            0,
        ),
        (
            lambda c: request(
                attachments=[
                    {"filename": "a", "content_type": "text/plain" + c, "content_base64": ""}
                ]
            ),
            "attachments[0].content_type",
            0,
        ),
        (lambda c: request(headers={"X-A": "b" + c}), "headers.X-A", None),
    ],
)
def test_case_7_control_characters(
    service: ServiceFactory, fake: FakeSMTP, char: str, make: Any, field: str, index: int | None
) -> None:
    error = refused_without_connection(service, fake, make(char), "INVALID_HEADER")
    assert error["field"] == field
    assert error.get("index") == index


# Case 8 ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "a@b@example.org",
        "Name <a@example.org>",
        "a@example.org.",
        "a.@example.org",
        "a@-example.org",
        "a@example-.org",
        "x" * 65 + "@example.org",
        "a@" + "b" * 64 + ".org",
        "a@" + ".".join(["b" * 63] * 4),
    ],
)
def test_case_8_address_grammar(service: ServiceFactory, fake: FakeSMTP, address: str) -> None:
    document = request(to=[{"address": "ok@example.org"}, {"address": address}])
    error = refused_without_connection(service, fake, document, "INVALID_ADDRESS")
    assert (error["field"], error["index"]) == ("to[1].address", 1)


@pytest.mark.parametrize(
    ("document", "field", "index"),
    [
        (
            request(to=[{"address": "a@example.org"}], bcc=[{"address": "a@Example.ORG"}]),
            "bcc[0].address",
            0,
        ),
        (
            request(reply_to=[{"address": "r@example.org"}, {"address": "r@example.org"}]),
            "reply_to[1].address",
            1,
        ),
        (request(to=...), None, None),
        (request(text=...), None, None),
        (
            request(
                html=..., inline=[{"cid": "a", "content_type": "image/png", "content_base64": ""}]
            ),
            "inline",
            None,
        ),
        (request(**{"from": {"name": "No Address"}}), "from.address", None),
        (request(unknown=1), "unknown", None),
    ],
)
def test_case_8_invalid_requests(
    service: ServiceFactory, fake: FakeSMTP, document: Any, field: str | None, index: int | None
) -> None:
    error = refused_without_connection(service, fake, document, "INVALID_REQUEST")
    assert error.get("field") == field
    assert error.get("index") == index


def test_case_8_repeated_key_and_lone_surrogate(service: ServiceFactory, fake: FakeSMTP) -> None:
    with service() as client:
        for body in (
            b'{"subject": "a", "subject": "b"}',
            b'{"from": {"address": "a@example.org"}, "to": [{"address": "b@example.org"}],'
            b' "subject": "\\ud800", "text": "x"}',
        ):
            response = client.post(
                "/v1/send", content=body, headers={"content-type": "application/json"}
            )
            assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert fake.connections == 0


# Case 9 ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("document", "field"),
    [
        (
            request(attachments=[{"filename": "a", "content_base64": "%%%%"}]),
            "attachments[0].content_base64",
        ),
        (
            request(attachments=[{"filename": "a", "content_base64": "QUI"}]),
            "attachments[0].content_base64",
        ),
        (
            request(
                html="",
                inline=[
                    {"cid": "x", "content_type": "image/png", "content_base64": ""},
                    {"cid": "x", "content_type": "image/png", "content_base64": ""},
                ],
            ),
            "inline[1].cid",
        ),
        (
            request(
                attachments=[
                    {"filename": "a", "content_type": "multipart/mixed", "content_base64": ""}
                ]
            ),
            "attachments[0].content_type",
        ),
        (
            request(
                attachments=[
                    {"filename": "a", "content_type": "message/rfc822", "content_base64": ""}
                ]
            ),
            "attachments[0].content_type",
        ),
        (
            request(attachments=[{"filename": "dir/a", "content_base64": ""}]),
            "attachments[0].filename",
        ),
        (
            request(attachments=[{"filename": "dir\\a", "content_base64": ""}]),
            "attachments[0].filename",
        ),
        (
            request(attachments=[{"filename": "..", "content_base64": ""}]),
            "attachments[0].filename",
        ),
    ],
)
def test_case_9_invalid_content(
    service: ServiceFactory, fake: FakeSMTP, document: Any, field: str
) -> None:
    error = refused_without_connection(service, fake, document, "INVALID_CONTENT")
    assert error["field"] == field


def test_case_9_two_dots_inside_a_name(service: ServiceFactory, fake: FakeSMTP) -> None:
    attachments = [{"filename": "report..pdf", "content_base64": b64(b"%PDF")}]
    status, _ = send(service, request(attachments=attachments))
    assert status == 200
    message = parse(delivered(fake))
    assert list(message.iter_parts())[1].get_filename() == "report..pdf"


# Case 10 -----------------------------------------------------------------------------------


def test_case_10_request_too_large(service: ServiceFactory, fake: FakeSMTP) -> None:
    error = refused_without_connection(
        service, fake, request(text="x" * 500), "REQUEST_TOO_LARGE", max_request_bytes=200
    )
    assert error["limit_bytes"] == 200
    assert error["actual_bytes"] > 200
    assert error["limit_source"] == "max_request_bytes"


def test_case_10_message_too_large(service: ServiceFactory, fake: FakeSMTP) -> None:
    error = refused_without_connection(
        service, fake, request(text="x" * 500), "MESSAGE_TOO_LARGE", max_message_bytes=300
    )
    assert error["limit_bytes"] == 300
    assert error["actual_bytes"] > 300
    assert error["limit_source"] == "max_message_bytes"


def test_case_10_too_many_recipients(service: ServiceFactory, fake: FakeSMTP) -> None:
    document = request(to=[{"address": f"t{n}@example.org"} for n in range(3)])
    error = refused_without_connection(
        service, fake, document, "TOO_MANY_RECIPIENTS", max_recipients=2
    )
    assert (error["limit"], error["actual"], error["limit_source"]) == (2, 3, "max_recipients")


def test_case_10_server_size_after_a_fresh_probe(service: ServiceFactory, fake: FakeSMTP) -> None:
    fake.capabilities = ["SIZE 400"]
    with service(health_cache_ttl_seconds=60) as client:
        assert client.get("/v1/health").json()["upstream"]["capabilities"]["size_max_bytes"] == 400
        probes = fake.connections
        response = client.post("/v1/send", json=request(text="x" * 500))
    error = response.json()["error"]
    assert error["code"] == "MESSAGE_TOO_LARGE"
    assert (error["limit_bytes"], error["limit_source"]) == (400, "server_size")
    assert error["actual_bytes"] > 400
    assert fake.connections == probes


def test_case_10_server_size_without_a_fresh_probe(service: ServiceFactory, fake: FakeSMTP) -> None:
    fake.capabilities = ["SIZE 400"]
    fake.responses["MAIL"] = "552 5.3.4 Message size exceeds fixed limit"
    status, body = send(service, request(text="x" * 500))
    assert status == 502
    error = body["error"]
    assert error["code"] == "UPSTREAM_REJECTED"
    assert error["upstream"] == {
        "stage": "mail_from",
        "code": 552,
        "message": "5.3.4 Message size exceeds fixed limit",
    }
    assert re.fullmatch(r"MAIL FROM:<sender@example\.org> SIZE=\d+", fake.last.mail_from or "")
    assert fake.last.mail_from == f"MAIL FROM:<sender@example.org> SIZE={error['size_bytes']}"


def test_size_parameter_only_when_announced(service: ServiceFactory, fake: FakeSMTP) -> None:
    status, _ = send(service, request())
    assert status == 200
    assert fake.last.mail_from == "MAIL FROM:<sender@example.org>"
    fake.capabilities = ["SIZE"]
    status, body = send(service, request())
    assert status == 200
    assert fake.last.mail_from == f"MAIL FROM:<sender@example.org> SIZE={body['size_bytes']}"


# Case 11 -----------------------------------------------------------------------------------


def test_case_11_partial_acceptance(service: ServiceFactory, fake: FakeSMTP) -> None:
    def rcpt(session: Any, line: str) -> str:
        if "<bcc@" in line:
            return "450 4.2.0 <bcc@example.org>: Greylisted, try later"
        if "<cc@" in line:
            return "550 5.1.1 <cc@example.org>: Recipient address rejected"
        return "250 2.1.5 OK"

    fake.responses["RCPT"] = rcpt
    document = request(
        bcc=[{"address": "bcc@example.org"}],
        cc=[{"address": "cc@example.org"}],
        to=[{"address": "to@example.org"}],
    )
    status, body = send(service, document)
    assert status == 200
    assert (body["accepted"], body["rejected"], body["deferred"]) == (1, 1, 1)
    assert body["recipients"] == [
        {
            "address": "to@example.org",
            "field": "to",
            "status": "accepted",
            "smtp": {"code": 250, "message": "2.1.5 OK"},
        },
        {
            "address": "cc@example.org",
            "field": "cc",
            "status": "rejected",
            "smtp": {"code": 550, "message": "5.1.1 <cc@example.org>: Recipient address rejected"},
        },
        {
            "address": "bcc@example.org",
            "field": "bcc",
            "status": "deferred",
            "smtp": {"code": 450, "message": "4.2.0 <bcc@example.org>: Greylisted, try later"},
        },
    ]
    message = parse(delivered(fake))
    assert [a.addr_spec for a in message["Cc"].addresses] == ["cc@example.org"]


def test_multi_line_replies_are_joined(service: ServiceFactory, fake: FakeSMTP) -> None:
    fake.responses["RCPT"] = "250-2.1.5 first\r\n250-second\r\n250 third"
    _, body = send(service, request())
    assert body["recipients"][0]["smtp"] == {"code": 250, "message": "2.1.5 first\nsecond\nthird"}


# Case 12 -----------------------------------------------------------------------------------


def test_case_12_all_rejected(service: ServiceFactory, fake: FakeSMTP) -> None:
    fake.responses["RCPT"] = lambda session, line: "550 5.1.1 unknown " + line[8:]
    document = request(to=[{"address": "a@example.org"}, {"address": "b@example.org"}])
    status, body = send(service, document)
    assert status == 502
    error = body["error"]
    assert error["code"] == "UPSTREAM_REJECTED"
    assert error["upstream"] == {
        "stage": "rcpt_to",
        "code": 550,
        "message": "5.1.1 unknown <a@example.org>",
    }
    assert [r["status"] for r in error["recipients"]] == ["rejected", "rejected"]
    assert "accepted" not in error and "rejected" not in error
    assert {"message_id", "size_bytes", "duration_ms"} <= set(error)
    assert "DATA" not in fake.last.commands
    assert fake.messages == []


@pytest.mark.parametrize(
    ("verb", "stage", "recipients"),
    [("MAIL", "mail_from", False), ("DATA", "data", True), ("END", "data_end", True)],
)
def test_case_12_rejected_at_each_stage(
    service: ServiceFactory, fake: FakeSMTP, verb: str, stage: str, recipients: bool
) -> None:
    fake.responses[verb] = "554 5.7.1 Refused"
    status, body = send(service, request())
    assert status == 502
    error = body["error"]
    assert error["code"] == "UPSTREAM_REJECTED"
    assert error["upstream"] == {"stage": stage, "code": 554, "message": "5.7.1 Refused"}
    assert ("recipients" in error) is recipients
    if recipients:
        assert [r["status"] for r in error["recipients"]] == ["accepted"]
    assert fake.messages == []


# Case 13 -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("setup", "stage"),
    [
        (lambda fake: setattr(fake, "greeting", "421 4.3.2 Busy"), "greeting"),
        (lambda fake: fake.responses.__setitem__("MAIL", "451 4.3.0 Try later"), "mail_from"),
        (lambda fake: fake.responses.__setitem__("RCPT", "450 4.2.0 Greylisted"), "rcpt_to"),
        (lambda fake: fake.responses.__setitem__("END", "452 4.3.1 Out of space"), "data_end"),
    ],
)
def test_case_13_transient(service: ServiceFactory, fake: FakeSMTP, setup: Any, stage: str) -> None:
    setup(fake)
    with service() as client:
        response = client.post("/v1/send", json=request())
    assert response.status_code == 503
    assert "retry-after" not in response.headers
    error = response.json()["error"]
    assert error["code"] == "UPSTREAM_TRANSIENT"
    assert error["upstream"]["stage"] == stage
    assert 400 <= error["upstream"]["code"] < 500


def test_case_13_first_transient_reply_is_reported(service: ServiceFactory, fake: FakeSMTP) -> None:
    fake.responses["RCPT"] = lambda session, line: (
        "550 5.1.1 no" if "a@" in line else "451 4.7.1 later " + line[8:]
    )
    document = request(
        to=[
            {"address": "a@example.org"},
            {"address": "b@example.org"},
            {"address": "c@example.org"},
        ]
    )
    status, body = send(service, document)
    assert status == 503
    assert body["error"]["upstream"] == {
        "stage": "rcpt_to",
        "code": 451,
        "message": "4.7.1 later <b@example.org>",
    }
    assert [r["status"] for r in body["error"]["recipients"]] == [
        "rejected",
        "deferred",
        "deferred",
    ]


def test_case_13_multi_line_greeting(service: ServiceFactory, fake: FakeSMTP) -> None:
    fake.greeting = "220-fake.example.org first\r\n220-second line\r\n220 ready"
    status, _ = send(service, request())
    assert status == 200
    delivered(fake)


def test_case_13_ehlo_refused_without_helo(service: ServiceFactory, fake: FakeSMTP) -> None:
    fake.responses["EHLO"] = "502 5.5.1 Command not implemented"
    status, body = send(service, request())
    assert status == 502
    assert body["error"]["code"] == "UPSTREAM_ERROR"
    assert body["error"]["upstream"]["stage"] == "ehlo"
    assert not any(command.upper().startswith("HELO") for command in fake.last.commands)


def test_case_13_starttls_refused(service: ServiceFactory, fake: FakeSMTP) -> None:
    fake.starttls = True
    fake.responses["STARTTLS"] = "454 4.7.0 TLS not available"
    status, body = send(service, request(), smtp_tls="starttls")
    assert status == 502
    assert body["error"]["code"] == "UPSTREAM_TLS"
    assert body["error"]["upstream"] == {
        "stage": "starttls",
        "code": 454,
        "message": "4.7.0 TLS not available",
    }
    assert not any(c.startswith("MAIL") for c in fake.last.commands)


@pytest.mark.parametrize(
    ("verb", "reply", "stage"),
    [
        ("MAIL", "354 what", "mail_from"),
        ("RCPT", "354 what", "rcpt_to"),
        ("DATA", "250 already", "data"),
        ("DATA", "100 hm", "data"),
        ("MAIL", "650 off the scale", "mail_from"),
    ],
)
def test_replies_out_of_course(
    service: ServiceFactory, fake: FakeSMTP, verb: str, reply: str, stage: str
) -> None:
    """[D39]: out of course is UPSTREAM_ERROR at the current stage; after a 2xx to DATA the
    content is not sent."""
    fake.responses[verb] = reply
    status, body = send(service, request())
    assert status == 502
    error = body["error"]
    assert error["code"] == "UPSTREAM_ERROR"
    assert error["upstream"]["stage"] == stage
    assert fake.last.data is None
    assert fake.messages == []


def test_a_3xx_to_rcpt_ends_the_send(service: ServiceFactory, fake: FakeSMTP) -> None:
    fake.responses["RCPT"] = lambda session, line: "354 odd" if "b@" in line else "250 OK"
    document = request(
        to=[
            {"address": "a@example.org"},
            {"address": "b@example.org"},
            {"address": "c@example.org"},
        ]
    )
    _, body = send(service, document)
    error = body["error"]
    assert error["upstream"] == {"stage": "rcpt_to", "code": 354, "message": "odd"}
    assert [r["address"] for r in error["recipients"]] == ["a@example.org"]
    assert len(fake.last.rcpt_to) == 2


# Case 19 -----------------------------------------------------------------------------------


def test_case_19_determinism(service: ServiceFactory, fake: FakeSMTP) -> None:
    inline = [{"cid": "logo", "content_type": "image/png", "content_base64": b64(PNG)}]
    attachments = [{"filename": "a.pdf", "content_base64": b64(b"%PDF")}]
    document = request(
        html='<img src="cid:logo">', inline=inline, attachments=attachments, subject="Zażółć"
    )
    first = send(service, document)[1]
    second = send(service, document)[1]
    assert first["message_id"] != second["message_id"]
    one, two = fake.messages

    def normalise(raw: bytes) -> bytes:
        raw = re.sub(rb"=_[0-9a-f]{32}", b"BOUNDARY", raw)
        raw = raw.replace(first["message_id"].encode(), b"ID").replace(
            second["message_id"].encode(), b"ID"
        )
        return re.sub(rb"\r\nDate: [^\r]*", b"\r\nDate: DATE", raw)

    assert normalise(one) == normalise(two)
    assert one != two


# Case 20 -----------------------------------------------------------------------------------


def test_case_20_only_the_fake_is_reached(
    service: ServiceFactory, fake: FakeSMTP, connections: list[tuple[str, int]]
) -> None:
    status, _ = send(service, request())
    assert status == 200
    assert connections == [("127.0.0.1", fake.port)]


def test_logs_carry_no_text_and_no_content(
    service: ServiceFactory, fake: FakeSMTP, caplog: pytest.LogCaptureFixture
) -> None:
    """[D30]: the codes and counters, never the subject, content, addresses list or reply text."""
    fake.responses["RCPT"] = lambda session, line: (
        "550 5.1.1 <gone@example.org>: no such user" if "gone@" in line else "250 2.1.5 fine"
    )
    document = request(
        subject="Secret subject",
        text="Secret content",
        to=[{"address": "gone@example.org"}, {"address": "here@example.net"}],
    )
    with caplog.at_level(logging.INFO):
        status, body = send(service, document)
    assert status == 200
    events = [r for r in caplog.records if r.name == "mail_dispatch"]
    (event,) = events
    fields = event.fields  # type: ignore[attr-defined]
    rendered = repr(fields)
    for secret in ("Secret", "no such user", "gone@example.org", "here@example.net", "fine"):
        assert secret not in rendered
    assert fields["dispatch_id"] == body["dispatch_id"]
    assert fields["sender"] == "sender@example.org"
    assert fields["recipients"] == 2
    assert fields["recipient_domains"] == ["example.net", "example.org"]
    assert fields["recipient_codes"] == [
        {"code": 550, "enhanced": "5.1.1"},
        {"code": 250, "enhanced": "2.1.5"},
    ]
    assert (fields["accepted"], fields["rejected"], fields["deferred"]) == (1, 1, 0)
    assert fields["reply"] == {"code": 250, "enhanced": "2.0.0"}
    assert fields["stage"] == "data_end"
    assert fields["size_bytes"] == body["size_bytes"]
    assert isinstance(fields["duration_ms"], int)
