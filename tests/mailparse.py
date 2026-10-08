"""Independent reading of a raw message with the standard library (SPEC §10.1)."""

from __future__ import annotations

import re
from email import message_from_bytes, policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import compat32


def parse(raw: bytes) -> EmailMessage:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert isinstance(message, EmailMessage)
    return message


def header_names(raw: bytes) -> list[str]:
    head = raw.split(b"\r\n\r\n", 1)[0].decode("ascii")
    return [line.split(":", 1)[0] for line in head.split("\r\n") if not line.startswith(" ")]


def text_of(part: EmailMessage) -> str:
    """Decoded text with line endings normalised, which is how §10 compares content."""
    payload = part.get_payload(decode=True)
    assert isinstance(payload, bytes)
    return re.sub(r"\r\n|\r|\n", "\n", payload.decode("utf-8"))


def structure(message: EmailMessage) -> object:
    if message.is_multipart():
        return (message.get_content_type(), [structure(p) for p in message.iter_parts()])  # type: ignore[arg-type]
    return message.get_content_type()


def assert_wire_format(raw: bytes) -> None:
    """CRLF only, every line at most 998, ending in CRLF."""
    assert raw.endswith(b"\r\n")
    assert b"\r" not in raw.replace(b"\r\n", b"")
    assert b"\n" not in raw.replace(b"\r\n", b"")
    assert all(len(line) <= 998 for line in raw.split(b"\r\n"))


def display_name(raw: bytes, header: str) -> str:
    """The display name of a single-mailbox header, decoded per RFC 2047.

    RFC 2047 §6.2: white space between adjacent encoded-words is ignored. The standard
    library's modern parser keeps it inside a display name (inside unstructured text it does
    not), so a name long enough to need several encoded-words is read with the legacy API,
    which follows the RFC.
    """
    value = message_from_bytes(raw, policy=compat32)[header]
    assert isinstance(value, str)
    decoded = str(make_header(decode_header(value)))
    return decoded.rsplit(" <", 1)[0]
