"""Steps 2-5 of SPEC §4.7: from a parsed document to a request that can be composed.

Every step runs over the fields in one fixed schema order, and the first error ends the
handling, so the same invalid request always gets the same error, with the same `field` and
`index` [D36]:

2. the shape (§4.1): for every object its unknown keys in request order, then its known fields
   in schema order — presence, type, valid Unicode — depth first; then the rules that span
   fields;
3. control characters in every field that ends up in a header or a protocol command (§4.6),
   on the raw value [D16];
4. the grammars, category by category: addresses, duplicates, custom headers, bodies and parts;
5. the number of recipients.
"""

from __future__ import annotations

import binascii
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

from .addresses import envelope_key, split_addr_spec
from .bodies import normalise_line_endings
from .errors import ApiError, ErrorCode
from .headers import fold, parameters, split_at_white_space

RecipientField = Literal["to", "cc", "bcc"]

_Kind = Literal["string", "address", "address_list", "inline_list", "attachment_list", "headers"]
_Schema = tuple[tuple[str, _Kind, bool], ...]

_TOP: _Schema = (
    ("from", "address", True),
    ("to", "address_list", False),
    ("cc", "address_list", False),
    ("bcc", "address_list", False),
    ("reply_to", "address_list", False),
    ("subject", "string", True),
    ("text", "string", False),
    ("html", "string", False),
    ("inline", "inline_list", False),
    ("attachments", "attachment_list", False),
    ("headers", "headers", False),
)
_ADDRESS: _Schema = (("address", "string", True), ("name", "string", False))
_INLINE: _Schema = (
    ("cid", "string", True),
    ("content_type", "string", True),
    ("filename", "string", False),
    ("content_base64", "string", True),
)
_ATTACHMENT: _Schema = (
    ("filename", "string", True),
    ("content_type", "string", False),
    ("content_base64", "string", True),
)
_ELEMENTS: dict[_Kind, _Schema] = {
    "address_list": _ADDRESS,
    "inline_list": _INLINE,
    "attachment_list": _ATTACHMENT,
}
_ADDRESS_LISTS = ("to", "cc", "bcc", "reply_to")

# Unicode category Cc: C0, DEL and C1 (§4.6).
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# The Unicode White_Space property, named rather than borrowed from str.isspace [D35].
_WHITE_SPACE = frozenset(
    map(
        chr,
        [
            0x09,
            0x0A,
            0x0B,
            0x0C,
            0x0D,
            0x20,
            0x85,
            0xA0,
            0x1680,
            *range(0x2000, 0x200B),
            0x2028,
            0x2029,
            0x202F,
            0x205F,
            0x3000,
        ],
    )
)
_FIELD_NAME = re.compile(r"[\x21-\x39\x3b-\x7e]+")
_RESERVED = frozenset(
    {
        "from",
        "sender",
        "to",
        "cc",
        "bcc",
        "reply-to",
        "subject",
        "date",
        "message-id",
        "mime-version",
        "return-path",
        "received",
    }
)
_CID = re.compile(r"[A-Za-z0-9!#$%&'*+\-/=?^_`{|}~.@]+")
_TOKEN = r"[!#$%&'*+\-.0-9A-Z^_`a-z{|}~]+"
_QUOTED = r'"(?:[\x20\x21\x23-\x5b\x5d-\x7e]|\\[\x20-\x7e])*"'
_CONTENT_TYPE = re.compile(rf" *({_TOKEN})/({_TOKEN}) *((?:; *{_TOKEN}=(?:{_TOKEN}|{_QUOTED}) *)*)")
_PARAMETER = re.compile(rf"; *({_TOKEN})=({_TOKEN}|{_QUOTED}) *")
_STRUCTURE_PARAMETERS = frozenset({"boundary", "name", "filename"})
_BASE64 = re.compile(r"[A-Za-z0-9+/]*={0,2}")
_BASE64_IGNORED = str.maketrans("", "", "\r\n ")
MAX_FILENAME = 255


@dataclass(frozen=True, slots=True)
class Mailbox:
    address: str
    name: str | None
    domain: str


@dataclass(frozen=True, slots=True)
class Recipient:
    mailbox: Mailbox
    field: RecipientField


@dataclass(frozen=True, slots=True)
class ContentType:
    """`type/subtype; a=b; c="d"` exactly as given; parameters fold between one another."""

    type: str
    subtype: str
    params: tuple[str, ...] = ()

    def pieces(self) -> list[str]:
        return parameters(f"{self.type}/{self.subtype}", self.params)


@dataclass(frozen=True, slots=True)
class Inline:
    cid: str
    content_type: ContentType
    filename: str | None
    data: bytes


@dataclass(frozen=True, slots=True)
class Attachment:
    filename: str
    content_type: ContentType | None
    data: bytes


@dataclass(frozen=True, slots=True)
class MessageRequest:
    sender: Mailbox
    recipients: tuple[Recipient, ...]
    reply_to: tuple[Mailbox, ...]
    subject: str
    # The content as it is sent: UTF-8 with every line ending CRLF (§4.3) [D8].
    text: bytes | None
    html: bytes | None
    inline: tuple[Inline, ...]
    attachments: tuple[Attachment, ...]
    headers: tuple[tuple[str, str], ...]

    def mailboxes(self, field: RecipientField) -> list[Mailbox]:
        return [r.mailbox for r in self.recipients if r.field == field]


def _error(code: ErrorCode, message: str, path: str | None, index: int | None) -> ApiError:
    fields: dict[str, object] = {}
    if path is not None:
        fields["field"] = path
    if index is not None:
        fields["index"] = index
    return ApiError(code, message, **fields)


def _readable(key: str) -> str:
    """A key as it can be written into a response: a lone surrogate becomes U+FFFD."""
    return "".join("\N{REPLACEMENT CHARACTER}" if 0xD800 <= ord(c) <= 0xDFFF else c for c in key)


def _is_unicode(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


# Step 2 -------------------------------------------------------------------------------------


def _walk_object(value: dict[str, Any], schema: _Schema, prefix: str, index: int | None) -> None:
    known = {name for name, _, _ in schema}
    for key in value:
        if key not in known:
            raise _error("INVALID_REQUEST", "unknown field", prefix + _readable(key), index)
    for name, kind, required in schema:
        path = prefix + name
        raw = value.get(name)
        if raw is None or raw == [] or (kind == "headers" and raw == {}):
            if required:
                raise _error("INVALID_REQUEST", "required field is missing", path, index)
            continue
        if kind == "string":
            if not isinstance(raw, str):
                raise _error("INVALID_REQUEST", "must be a string", path, index)
            if not _is_unicode(raw):
                raise _error("INVALID_REQUEST", "is not valid Unicode", path, index)
        elif kind == "address":
            if not isinstance(raw, dict):
                raise _error("INVALID_REQUEST", "must be an object", path, index)
            _walk_object(raw, _ADDRESS, path + ".", index)
        elif kind == "headers":
            if not isinstance(raw, dict):
                raise _error("INVALID_REQUEST", "must be an object", path, index)
            for key, header_value in raw.items():
                header_path = f"{path}.{_readable(key)}"
                if not _is_unicode(key):
                    raise _error("INVALID_REQUEST", "is not valid Unicode", header_path, None)
                if not isinstance(header_value, str):
                    raise _error("INVALID_REQUEST", "must be a string", header_path, None)
                if not _is_unicode(header_value):
                    raise _error("INVALID_REQUEST", "is not valid Unicode", header_path, None)
        else:
            if not isinstance(raw, list):
                raise _error("INVALID_REQUEST", "must be a list", path, index)
            for position, element in enumerate(raw):
                element_path = f"{path}[{position}]"
                if not isinstance(element, dict):
                    raise _error("INVALID_REQUEST", "must be an object", element_path, position)
                _walk_object(element, _ELEMENTS[kind], element_path + ".", position)


def _items(document: dict[str, Any], name: str) -> list[dict[str, Any]]:
    """A list field after the walk: an absent field, null and [] are all "none"."""
    value = document.get(name)
    return value if isinstance(value, list) else []


def _str(document: dict[str, Any], name: str) -> str | None:
    """A string field after the walk: an absent field, null and [] are all "none"."""
    value = document.get(name)
    return value if isinstance(value, str) else None


def _headers(document: dict[str, Any]) -> dict[str, str]:
    value = document.get("headers")
    return value if isinstance(value, dict) else {}


def _check_shape(document: object) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ApiError("INVALID_REQUEST", "the body must be a JSON object")
    _walk_object(document, _TOP, "", None)
    if not any(_items(document, name) for name in ("to", "cc", "bcc")):
        raise ApiError("INVALID_REQUEST", "at least one recipient is required in to, cc or bcc")
    if _str(document, "text") is None and _str(document, "html") is None:
        raise ApiError("INVALID_REQUEST", "text or html is required")
    if _items(document, "inline") and _str(document, "html") is None:
        raise _error("INVALID_REQUEST", "inline parts need html", "inline", None)
    return document


# Step 3 -------------------------------------------------------------------------------------


def _header_bound_fields(document: dict[str, Any]) -> Iterator[tuple[str, int | None, str]]:
    """Every field that ends up in a header or a protocol command, in schema order."""
    sender = document["from"]
    yield "from.address", None, sender["address"]
    if (name := _str(sender, "name")) is not None:
        yield "from.name", None, name
    for list_name in _ADDRESS_LISTS:
        for position, entry in enumerate(_items(document, list_name)):
            yield f"{list_name}[{position}].address", position, entry["address"]
            if (name := _str(entry, "name")) is not None:
                yield f"{list_name}[{position}].name", position, name
    yield "subject", None, document["subject"]
    for position, part in enumerate(_items(document, "inline")):
        for field in ("cid", "content_type", "filename"):
            if (value := _str(part, field)) is not None:
                yield f"inline[{position}].{field}", position, value
    for position, part in enumerate(_items(document, "attachments")):
        for field in ("filename", "content_type"):
            if (value := _str(part, field)) is not None:
                yield f"attachments[{position}].{field}", position, value
    for key, value in _headers(document).items():
        yield f"headers.{key}", None, key
        yield f"headers.{key}", None, value


def _check_control_characters(document: dict[str, Any]) -> None:
    for path, index, value in _header_bound_fields(document):
        if _CONTROL.search(value):
            raise _error("INVALID_HEADER", "contains a control character", path, index)


# Step 4 -------------------------------------------------------------------------------------


def _name(raw: str | None) -> str | None:
    """A name that is empty or White_Space only is absent; any other is kept as given [D35]."""
    if raw is None or all(c in _WHITE_SPACE for c in raw):
        return None
    return raw


def _mailbox(entry: dict[str, Any], path: str, index: int | None) -> Mailbox:
    split = split_addr_spec(entry["address"])
    if split is None:
        raise _error("INVALID_ADDRESS", "is not a valid address", path + ".address", index)
    return Mailbox(entry["address"], _name(_str(entry, "name")), split[1])


def _check_duplicates(lists: dict[str, list[Mailbox]]) -> None:
    seen: set[tuple[str, str]] = set()
    for list_name in ("to", "cc", "bcc", "reply_to"):
        if list_name == "reply_to":
            seen = set()
        for position, box in enumerate(lists[list_name]):
            local = box.address[: -len(box.domain) - 1]
            key = envelope_key(local, box.domain)
            if key in seen:
                raise _error(
                    "INVALID_REQUEST",
                    "the address is repeated",
                    f"{list_name}[{position}].address",
                    position,
                )
            seen.add(key)


def _check_custom_headers(headers: dict[str, str]) -> tuple[tuple[str, str], ...]:
    seen: set[str] = set()
    for name, value in headers.items():
        path = f"headers.{name}"
        if not _FIELD_NAME.fullmatch(name):
            raise _error("INVALID_HEADER", "is not a valid header name", path, None)
        lower = name.lower()
        if lower in _RESERVED or lower.startswith("content-"):
            raise _error("INVALID_HEADER", "is a header the service sets itself", path, None)
        if lower in seen:
            raise _error("INVALID_HEADER", "is given twice in different letter case", path, None)
        seen.add(lower)
        if not value.isascii():
            raise _error("INVALID_HEADER", "must be ASCII", path, None)
        if value.strip(" ") == "":
            raise _error("INVALID_HEADER", "must not be empty or spaces only", path, None)
        if fold(name, split_at_white_space(" " + value)) is None:
            raise _error("INVALID_HEADER", "cannot be folded within 998 characters", path, None)
    return tuple(headers.items())


def _content_type(raw: str, path: str, index: int) -> ContentType:
    shape = _CONTENT_TYPE.fullmatch(raw)
    if shape is None:
        raise _error("INVALID_CONTENT", "is not a valid content type", path, index)
    main, sub, rest = shape.groups()
    if main.lower() in ("multipart", "message"):
        raise _error("INVALID_CONTENT", f"{main.lower()}/* cannot be a part's type", path, index)
    params: list[str] = []
    seen: set[str] = set()
    for attribute, value in _PARAMETER.findall(rest):
        lower = attribute.lower()
        if lower.split("*", 1)[0] in _STRUCTURE_PARAMETERS:
            raise _error("INVALID_CONTENT", "sets a parameter the service sets", path, index)
        if lower in seen:
            raise _error("INVALID_CONTENT", "gives a parameter twice", path, index)
        seen.add(lower)
        params.append(f"{attribute}={value}")
    content_type = ContentType(main, sub, tuple(params))
    if fold("Content-Type", content_type.pieces()) is None:
        raise _error("INVALID_HEADER", "cannot be folded within 998 characters", path, index)
    return content_type


def _filename(raw: str, path: str, index: int) -> str:
    if "/" in raw or "\\" in raw:
        raise _error("INVALID_CONTENT", "a file name must not contain / or \\", path, index)
    if all(c in _WHITE_SPACE for c in raw):
        raise _error("INVALID_CONTENT", "a file name must not be empty", path, index)
    if raw in (".", ".."):
        raise _error("INVALID_CONTENT", "a file name must not be . or ..", path, index)
    if len(raw) > MAX_FILENAME:
        raise _error("INVALID_CONTENT", "a file name is limited to 255 characters", path, index)
    return raw


def _content(raw: str | None) -> bytes | None:
    return None if raw is None else normalise_line_endings(raw)


def _base64(raw: str, path: str, index: int) -> bytes:
    # A copy only when there is something to remove.
    needs_cleaning = "\n" in raw or "\r" in raw or " " in raw
    cleaned = raw.translate(_BASE64_IGNORED) if needs_cleaning else raw
    if len(cleaned) % 4 or not _BASE64.fullmatch(cleaned):
        raise _error("INVALID_CONTENT", "is not valid padded base64", path, index)
    return binascii.a2b_base64(cleaned)


def _parts(document: dict[str, Any]) -> tuple[tuple[Inline, ...], tuple[Attachment, ...]]:
    inline: list[Inline] = []
    cids: set[str] = set()
    for position, part in enumerate(_items(document, "inline")):
        prefix = f"inline[{position}]."
        cid = part["cid"]
        if not _CID.fullmatch(cid):
            raise _error("INVALID_CONTENT", "is not a valid content ID", prefix + "cid", position)
        if cid in cids:
            raise _error("INVALID_CONTENT", "the content ID is repeated", prefix + "cid", position)
        cids.add(cid)
        if fold("Content-ID", [f" <{cid}>"]) is None:
            raise _error(
                "INVALID_HEADER", "cannot be folded within 998 characters", prefix + "cid", position
            )
        content_type = _content_type(part["content_type"], prefix + "content_type", position)
        filename = _str(part, "filename")
        if filename is not None:
            filename = _filename(filename, prefix + "filename", position)
        data = _base64(part["content_base64"], prefix + "content_base64", position)
        inline.append(Inline(cid, content_type, filename, data))
    attachments: list[Attachment] = []
    for position, part in enumerate(_items(document, "attachments")):
        prefix = f"attachments[{position}]."
        filename = _filename(part["filename"], prefix + "filename", position)
        raw_type = _str(part, "content_type")
        given_type = (
            None if raw_type is None else _content_type(raw_type, prefix + "content_type", position)
        )
        data = _base64(part["content_base64"], prefix + "content_base64", position)
        attachments.append(Attachment(filename, given_type, data))
    return tuple(inline), tuple(attachments)


# Steps 2-5 ----------------------------------------------------------------------------------


def validate(document: object, *, max_recipients: int) -> MessageRequest:
    shape = _check_shape(document)
    _check_control_characters(shape)

    sender = _mailbox(shape["from"], "from", None)
    lists: dict[str, list[Mailbox]] = {}
    for list_name in _ADDRESS_LISTS:
        lists[list_name] = [
            _mailbox(entry, f"{list_name}[{position}]", position)
            for position, entry in enumerate(_items(shape, list_name))
        ]
    _check_duplicates(lists)
    headers = _check_custom_headers(_headers(shape))
    inline, attachments = _parts(shape)

    recipients = tuple(
        Recipient(box, field) for field in ("to", "cc", "bcc") for box in lists[field]
    )
    if len(recipients) > max_recipients:
        raise ApiError(
            "TOO_MANY_RECIPIENTS",
            "the number of recipients exceeds the limit",
            limit=max_recipients,
            actual=len(recipients),
            limit_source="max_recipients",
        )
    return MessageRequest(
        sender=sender,
        recipients=recipients,
        reply_to=tuple(lists["reply_to"]),
        subject=shape["subject"],
        text=_content(_str(shape, "text")),
        html=_content(_str(shape, "html")),
        inline=inline,
        attachments=attachments,
        headers=headers,
    )
