"""Composition of the message (SPEC §4.4, §4.5): deterministic for the same request [D13].

The headers come in a fixed order — `From`, `To`, `Cc`, `Reply-To`, `Subject`, `Date`,
`Message-ID`, `MIME-Version`, the custom headers in request order, then the structure's
`Content-*` — and the parts in a fixed order. Only `Date`, `Message-ID` and the boundaries
vary. `bcc` never reaches a header and no `To` is fabricated [D7].

The message is built in memory and always ends in `CRLF`, so what is sent before the final
`.` is exactly what `size_bytes` counts.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime

from . import filetypes
from .bodies import TransferEncoding, encode, normalise_line_endings, text_encoding
from .headers import (
    address_list,
    filename_parameters,
    fold,
    parameters,
    render,
    split_at_white_space,
    unstructured,
)
from .validation import Attachment, Inline, Mailbox, MessageRequest

Header = tuple[str, list[str]]


@dataclass(frozen=True, slots=True)
class Composed:
    data: bytes
    message_id: str


@dataclass(frozen=True, slots=True)
class _Leaf:
    headers: list[Header]
    content: bytes
    encoding: TransferEncoding


@dataclass(frozen=True, slots=True)
class _Multipart:
    subtype: str
    children: Sequence[_Leaf | _Multipart]
    extra: tuple[str, ...] = ()


_Entity = _Leaf | _Multipart


def _new_boundary() -> str:
    # '=_' cannot occur in quoted-printable or base64 output, so no part can contain it.
    return "=_" + secrets.token_hex(16)


def _new_token() -> str:
    return secrets.token_hex(16)


def _header_bytes(headers: Sequence[Header]) -> bytes:
    out = []
    for name, pieces in headers:
        lines = fold(name, pieces)
        # Validation refused every value that cannot be folded; the rest fold by construction.
        assert lines is not None, name
        out.append(render(lines))
    return b"".join(out)


def _text_part(content: str, subtype: str) -> _Leaf:
    data = normalise_line_endings(content)
    encoding = text_encoding(data)
    return _Leaf(
        [
            ("Content-Type", parameters(f"text/{subtype}", ["charset=utf-8"])),
            ("Content-Transfer-Encoding", [" " + encoding]),
        ],
        data,
        encoding,
    )


def _inline_part(part: Inline) -> _Leaf:
    disposition = filename_parameters(part.filename) if part.filename is not None else []
    return _Leaf(
        [
            ("Content-Type", part.content_type.pieces()),
            ("Content-Transfer-Encoding", [" base64"]),
            ("Content-ID", [f" <{part.cid}>"]),
            ("Content-Disposition", parameters("inline", disposition)),
        ],
        part.data,
        "base64",
    )


def _attachment_part(part: Attachment) -> _Leaf:
    content_type = (
        part.content_type.pieces()
        if part.content_type is not None
        else [" " + filetypes.guess(part.filename)]
    )
    return _Leaf(
        [
            ("Content-Type", content_type),
            ("Content-Transfer-Encoding", [" base64"]),
            ("Content-Disposition", parameters("attachment", filename_parameters(part.filename))),
        ],
        part.data,
        "base64",
    )


def _structure(request: MessageRequest) -> _Entity:
    """The table of §4.4, always the same way for the same fields."""
    html: _Entity | None = None
    if request.html is not None:
        html = _text_part(request.html, "html")
        if request.inline:
            html = _Multipart(
                "related", [html, *map(_inline_part, request.inline)], ('type="text/html"',)
            )
    text = _text_part(request.text, "plain") if request.text is not None else None
    body: _Entity
    if text is not None and html is not None:
        body = _Multipart("alternative", [text, html])
    elif text is not None:
        body = text
    else:
        # Validation guarantees text or html.
        assert html is not None
        body = html
    if request.attachments:
        body = _Multipart("mixed", [body, *map(_attachment_part, request.attachments)])
    return body


def _serialise(
    entity: _Entity, boundary: Callable[[], str], *, top: bool
) -> tuple[list[Header], bytes]:
    """The entity's own `Content-*` headers and its body.

    A body inside a multipart is followed by `CRLF` and a delimiter, which belong to the
    delimiter; the top-level body ends the message, so it is terminated itself.
    """
    if isinstance(entity, _Leaf):
        return entity.headers, encode(entity.content, entity.encoding, terminated=top)
    marker = boundary()
    chunks: list[bytes] = []
    for child in entity.children:
        headers, body = _serialise(child, boundary, top=False)
        chunks.append(b"--" + marker.encode("ascii") + b"\r\n")
        chunks.append(_header_bytes(headers) + b"\r\n" + body + b"\r\n")
    chunks.append(b"--" + marker.encode("ascii") + b"--")
    if top:
        chunks.append(b"\r\n")
    params = (f'boundary="{marker}"', *entity.extra)
    return [("Content-Type", parameters(f"multipart/{entity.subtype}", params))], b"".join(chunks)


def _mailboxes(header: str, boxes: Sequence[Mailbox]) -> Header:
    return header, address_list(header, [(box.address, box.name) for box in boxes])


def compose(
    request: MessageRequest,
    *,
    message_id_domain: str | None,
    now: datetime | None = None,
    token: Callable[[], str] = _new_token,
    boundary: Callable[[], str] = _new_boundary,
) -> Composed:
    domain = message_id_domain if message_id_domain is not None else request.sender.domain
    message_id = f"<{token()}@{domain}>"
    moment = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)

    headers: list[Header] = [_mailboxes("From", [request.sender])]
    if to := request.mailboxes("to"):
        headers.append(_mailboxes("To", to))
    if cc := request.mailboxes("cc"):
        headers.append(_mailboxes("Cc", cc))
    if request.reply_to:
        headers.append(_mailboxes("Reply-To", request.reply_to))
    headers.append(("Subject", unstructured("Subject", request.subject)))
    headers.append(("Date", split_at_white_space(" " + format_datetime(moment))))
    headers.append(("Message-ID", [" " + message_id]))
    headers.append(("MIME-Version", [" 1.0"]))
    headers.extend((name, split_at_white_space(" " + value)) for name, value in request.headers)

    content_headers, body = _serialise(_structure(request), boundary, top=True)
    data = _header_bytes([*headers, *content_headers]) + b"\r\n" + body
    return Composed(data, message_id)
