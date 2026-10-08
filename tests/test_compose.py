"""Composition (SPEC §4.4, §4.5) read back with the standard library: cases 1-6 and 19."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

import pytest
from builders import PNG, b64, request
from mailparse import assert_wire_format, display_name, header_names, parse, structure, text_of

from mail_dispatch.compose import Composed, compose
from mail_dispatch.validation import validate


def build(document: dict[str, Any], message_id_domain: str | None = None) -> Composed:
    composed = compose(validate(document, max_recipients=100), message_id_domain=message_id_domain)
    assert_wire_format(composed.data)
    return composed


def test_text_only() -> None:
    """Case 1."""
    composed = build(request(text="Hello\nworld\r\nagain\rend", subject=""))
    message = parse(composed.data)
    assert message.get_content_type() == "text/plain"
    assert message.get_content_charset() == "utf-8"
    assert message["Content-Transfer-Encoding"] == "quoted-printable"
    assert text_of(message) == "Hello\nworld\nagain\nend"
    assert message["Subject"] == ""
    assert message["MIME-Version"] == "1.0"
    assert re.fullmatch(
        r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d\d (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
        r" \d{4} \d\d:\d\d:\d\d \+0000",
        str(message["Date"]),
    )
    assert re.fullmatch(r"<[0-9a-f]{32}@example\.org>", composed.message_id)
    assert message["Message-ID"] == composed.message_id


def test_text_dominated_by_non_ascii_is_base64() -> None:
    """Case 1."""
    text = "Здравствуйте, мир!\n" * 3
    message = parse(build(request(text=text)).data)
    assert message["Content-Transfer-Encoding"] == "base64"
    assert text_of(message) == text


def test_content_without_a_final_line_break_reads_back_without_one() -> None:
    message = parse(build(request(text="no newline at the end")).data)
    assert text_of(message) == "no newline at the end"
    message = parse(build(request(text="one newline\n")).data)
    assert text_of(message) == "one newline\n"


def test_empty_text_is_an_empty_part() -> None:
    message = parse(build(request(text="")).data)
    assert message["Content-Transfer-Encoding"] == "quoted-printable"
    assert message.get_payload(decode=True) == b""


def test_html_only() -> None:
    """Case 2."""
    message = parse(build(request(text=..., html="<p>Hi</p>")).data)
    assert message.get_content_type() == "text/html"
    assert text_of(message) == "<p>Hi</p>"


def test_html_with_inline() -> None:
    """Case 2: related with type="text/html", the inline part as given."""
    inline = [
        {
            "cid": "logo",
            "content_type": "image/png",
            "content_base64": b64(PNG),
            "filename": "logo.png",
        },
        {"cid": "pixel@example.org", "content_type": "image/gif", "content_base64": b64(b"GIF89a")},
    ]
    message = parse(build(request(text=..., html='<img src="cid:logo">', inline=inline)).data)
    assert structure(message) == ("multipart/related", ["text/html", "image/png", "image/gif"])
    assert message.get_param("type") == "text/html"
    _, logo, pixel = message.iter_parts()
    assert logo["Content-ID"] == "<logo>"
    assert logo.get_content_disposition() == "inline"
    assert logo.get_filename() == "logo.png"
    assert logo.get_payload(decode=True) == PNG
    assert pixel["Content-ID"] == "<pixel@example.org>"
    assert pixel.get_filename() is None
    assert "filename" not in str(pixel["Content-Disposition"])


def test_full_structure() -> None:
    """Case 3: mixed(alternative(text, related(html, inline)), attachments) in this order."""
    attachments = [
        {"filename": "report.pdf", "content_base64": b64(b"%PDF-1.7 " + bytes(range(256)))},
        {"filename": "notes.txt", "content_type": "text/plain", "content_base64": b64(b"plain\n")},
        {"filename": "data.unknownext", "content_base64": b64(b"?")},
        {"filename": "forward.eml", "content_base64": b64(b"From: x\r\n\r\nhi")},
        {"filename": "PHOTO.JPG", "content_base64": b64(b"\xff\xd8")},
        {"filename": "archive.tar.gz", "content_base64": b64(b"\x1f\x8b")},
    ]
    inline = [{"cid": "logo", "content_type": "image/png", "content_base64": b64(PNG)}]
    document = request(html="<p>x</p>", inline=inline, attachments=attachments)
    message = parse(build(document).data)
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
            "image/jpeg",
            "application/gzip",
        ],
    )
    parts = list(message.iter_parts())[1:]
    for part, given in zip(parts, attachments, strict=True):
        assert part.get_content_disposition() == "attachment"
        assert part.get_filename() == given["filename"]
        assert part["Content-Transfer-Encoding"] == "base64"
        data = part.get_payload(decode=True)
        assert isinstance(data, bytes)
        expected = hashlib.sha256(__import__("base64").b64decode(given["content_base64"]))
        assert hashlib.sha256(data).hexdigest() == expected.hexdigest()
    # A guessed type carries no parameter, not even a charset for text/*.
    assert parts[1]["Content-Type"] == "text/plain"


def test_content_type_is_written_as_given() -> None:
    attachments = [
        {
            "filename": "a.txt",
            "content_type": 'Text/Plain ;charset=ISO-8859-2;  format="flowed"',
            "content_base64": "",
        }
    ]
    raw = build(request(attachments=attachments)).data
    assert b'Content-Type: Text/Plain; charset=ISO-8859-2; format="flowed"\r\n' in raw


def test_encoded_subject_and_names() -> None:
    """Case 4."""
    document = request(
        subject="Zażółć gęślą jaźń — raport",
        **{"from": {"address": "s@example.org", "name": "Łukasz Żółw"}},
        to=[
            {"address": "a@example.org", "name": 'Smith, John "Jr" \\ x'},
            {"address": "b@example.org", "name": "   "},
        ],
        cc=[{"address": "c@example.org", "name": " Padded "}],
    )
    raw = build(document).data
    message = parse(raw)
    assert message["Subject"] == "Zażółć gęślą jaźń — raport"
    assert b"=?utf-8?b?" in raw
    assert message["From"].addresses[0].display_name == "Łukasz Żółw"
    to = message["To"].addresses
    assert to[0].display_name == 'Smith, John "Jr" \\ x'
    assert to[1].display_name == ""
    assert b"<b@example.org>" not in raw
    assert message["Cc"].addresses[0].display_name == " Padded "


def test_literal_encoded_word_in_ascii_text_is_encoded() -> None:
    raw = build(request(subject="=?utf-8?q?not-a-word?=")).data
    assert parse(raw)["Subject"] == "=?utf-8?q?not-a-word?="
    assert b"Subject: =?utf-8?q?not-a-word?=" not in raw


def test_filenames_in_rfc_2231() -> None:
    """Case 4: a non-ASCII file name and a 200-character one."""
    attachments = [
        {"filename": "raport końcowy.pdf", "content_base64": ""},
        {"filename": "x" * 196 + ".pdf", "content_base64": ""},
        {"filename": "ż" * 255, "content_base64": ""},
    ]
    raw = build(request(attachments=attachments)).data
    message = parse(raw)
    names = [part.get_filename() for part in list(message.iter_parts())[1:]]
    assert names == [a["filename"] for a in attachments]
    assert b"filename*=utf-8''raport%20ko%C5%84cowy.pdf" in raw
    assert b'filename*0="xxx' in raw
    assert all(len(line) <= 78 for line in raw.split(b"\r\n") if b"filename" in line)


def test_unfoldable_ascii_subject_and_name_are_encoded() -> None:
    """Case 4: 1000 ASCII characters without white space, every line within 998."""
    long = "x" * 1000
    document = request(subject=long, **{"from": {"address": "s@example.org", "name": long}})
    raw = build(document).data
    message = parse(raw)
    assert message["Subject"] == long
    assert display_name(raw, "From") == long
    assert all(len(line) <= 998 for line in raw.split(b"\r\n"))


def test_a_long_ascii_subject_with_spaces_is_folded_not_encoded() -> None:
    subject = " ".join(["word"] * 100)
    raw = build(request(subject=subject)).data
    assert b"=?utf-8" not in raw
    assert parse(raw)["Subject"] == subject


def test_envelope_only_bcc_and_no_fabricated_to() -> None:
    """Case 5."""
    document = request(
        to=...,
        cc=[{"address": "c@example.org"}],
        bcc=[{"address": "hidden@example.org"}],
        reply_to=[{"address": "r1@example.org"}, {"address": "r2@example.org", "name": "Two"}],
    )
    raw = build(document).data
    assert b"hidden@example.org" not in raw
    assert b"Bcc" not in raw
    message = parse(raw)
    assert message["To"] is None
    assert [a.addr_spec for a in message["Cc"].addresses] == ["c@example.org"]
    assert [a.addr_spec for a in message["Reply-To"].addresses] == [
        "r1@example.org",
        "r2@example.org",
    ]
    assert header_names(raw).count("Reply-To") == 1


def test_custom_headers_and_order() -> None:
    """Case 6: verbatim values, folding at the existing spaces, the fixed order."""
    references = " ".join("<" + "r" * 498 + ">" for _ in range(3))
    headers = {
        "In-Reply-To": "<id@example.org>",
        "References": references,
        "X-Encoded": "=?utf-8?q?caf=C3=A9?=",
    }
    document = request(
        to=[{"address": "t@example.org"}],
        cc=[{"address": "c@example.org"}],
        reply_to=[{"address": "r@example.org"}],
        headers=headers,
        attachments=[{"filename": "a.pdf", "content_base64": ""}],
    )
    raw = build(document).data
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
    ]
    head = raw.split(b"\r\n\r\n", 1)[0].decode()
    unfolded = head.replace("\r\n ", " ")
    assert "\r\nReferences: " + references + "\r\n" in "\r\n" + unfolded + "\r\n"
    assert "X-Encoded: =?utf-8?q?caf=C3=A9?=" in head
    assert all(len(line) <= 998 for line in head.split("\r\n"))


def test_message_id_domain() -> None:
    assert build(request(), "mail.example.net").message_id.endswith("@mail.example.net>")
    literal = request(**{"from": {"address": "s@[192.0.2.1]"}})
    assert build(literal).message_id.endswith("@[192.0.2.1]>")


def test_two_messages_differ_only_in_date_id_and_boundaries() -> None:
    """Case 19."""
    attachments = [{"filename": "a.pdf", "content_base64": b64(b"%PDF")}]
    inline = [{"cid": "logo", "content_type": "image/png", "content_base64": b64(PNG)}]
    document = request(html="<p>x</p>", inline=inline, attachments=attachments)
    message = validate(document, max_recipients=100)
    first = compose(message, message_id_domain=None)
    second = compose(message, message_id_domain=None)
    assert first.message_id != second.message_id

    def normalise(raw: bytes) -> bytes:
        raw = re.sub(rb"=_[0-9a-f]{32}", b"BOUNDARY", raw)
        raw = re.sub(rb"<[0-9a-f]{32}@", b"<ID@", raw)
        return re.sub(rb"Date: [^\r]*", b"Date: DATE", raw)

    assert normalise(first.data) == normalise(second.data)
    assert first.data != second.data


def test_fixed_moment_token_and_boundaries_make_the_same_bytes() -> None:
    message = validate(
        request(attachments=[{"filename": "a", "content_base64": ""}]), max_recipients=1
    )
    moment = datetime(2026, 10, 8, 12, 0, 0, 123, tzinfo=UTC)
    first = compose(
        message, message_id_domain=None, now=moment, token=lambda: "t", boundary=lambda: "=_b"
    )
    second = compose(
        message, message_id_domain=None, now=moment, token=lambda: "t", boundary=lambda: "=_b"
    )
    assert first == second
    assert b"Date: Thu, 08 Oct 2026 12:00:00 +0000\r\n" in first.data


@pytest.mark.parametrize("text", ["", "a", ".", "\n", "x" * 10_000, "line\n.\n..\n"])
def test_the_message_always_ends_in_crlf(text: str) -> None:
    for document in (request(text=text), request(text=text, html=text)):
        raw = build(document).data
        assert raw.endswith(b"\r\n")
